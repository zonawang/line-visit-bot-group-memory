"""LINE group visit assistant with per-conversation Firestore memory."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from event_store import EventStoreError, FirestoreEventStore


FAQ = json.loads(Path(__file__).with_name("faq.json").read_text(encoding="utf-8"))
MAX_BODY_BYTES = 1_000_000
MAX_FIELD_LENGTH = 300
MAX_LABEL_LENGTH = 30
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
GENERIC_FIELD_WORDS = {
    "地點",
    "位置",
    "時間",
    "日期",
    "方式",
    "資訊",
    "資料",
    "內容",
    "事項",
    "說明",
}
QUESTION_FILLERS = (
    "可以",
    "可不可以",
    "能不能",
    "請問",
    "哪裡",
    "哪邊",
    "什麼",
    "怎麼",
    "如何",
    "是否",
)

FIELD_COMMANDS = {
    "活動名稱": "eventName",
    "日期": "eventDate",
    "集合時間": "meetingTime",
    "集合地點": "meetingPlace",
    "交通": "transportation",
    "聯絡人": "contact",
}
FIELD_LABELS = {
    "eventName": "活動名稱",
    "eventDate": "日期",
    "meetingTime": "集合時間",
    "meetingPlace": "集合地點",
    "transportation": "交通方式",
    "contact": "聯絡人",
}
SET_COMMAND = re.compile(
    r"設定\s*(?:(?P<label_builtin>活動名稱|日期|集合時間|集合地點|交通|聯絡人)"
    r"(?:\s*[:：]\s*|\s+)(?P<value_builtin>.+)|"
    r"(?P<label_colon>[^:：\n]+?)\s*[:：]\s*(?P<value_colon>.+)|"
    r"(?P<label_space>[^:：\s]+)\s+(?P<value_space>.+))",
    re.DOTALL,
)

_event_store = None


def get_event_store():
    global _event_store
    if _event_store is None:
        collection = os.environ.get("VISIT_GROUP_COLLECTION", "visit_group_events")
        _event_store = FirestoreEventStore(collection)
    return _event_store


def normalize(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", text).lower() if not char.isspace()
    )


def match_text(text: str) -> str:
    """Keep searchable letters and numbers while dropping mentions and punctuation."""
    return "".join(char for char in normalize(text) if char.isalnum())


def longest_common_text(left: str, right: str) -> str:
    """Return the longest contiguous fragment shared by two short strings."""
    if not left or not right:
        return ""
    previous = [0] * (len(right) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_char in enumerate(left, start=1):
        current = [0] * (len(right) + 1)
        for right_index, right_char in enumerate(right, start=1):
            if left_char == right_char:
                current[right_index] = previous[right_index - 1] + 1
                if current[right_index] > best_length:
                    best_length = current[right_index]
                    best_end = left_index
        previous = current
    return left[best_end - best_length : best_end]


def custom_field_answer(question: str, event: dict[str, Any]) -> str | None:
    """Find the custom field whose label best matches a natural-language question."""
    fields = event.get("customFields") or {}
    question_text = match_text(question)
    question_core = question_text
    for filler in QUESTION_FILLERS:
        question_core = question_core.replace(match_text(filler), "")

    best_match = None
    best_score = None
    for label, value in fields.items():
        label_text = match_text(label)
        if not label_text or not value:
            continue
        if label_text in question_text:
            score = (2, len(label_text), 1.0, len(set(label_text)))
        else:
            label_core = label_text
            for generic_word in GENERIC_FIELD_WORDS:
                if label_core.endswith(generic_word) and len(label_core) > len(generic_word):
                    label_core = label_core[: -len(generic_word)]
                    break
            common = longest_common_text(label_core, question_core)
            meaningful_common = len(common) >= 2 and common not in GENERIC_FIELD_WORDS
            shared_characters = len(set(label_core) & set(question_core))
            coverage = shared_characters / max(1, len(set(label_core)))
            if not meaningful_common and not (shared_characters >= 3 and coverage >= 0.5):
                continue
            score = (1 if meaningful_common else 0, len(common), coverage, shared_characters)
        if best_score is None or score > best_score:
            best_score = score
            best_match = (label, value)

    if best_match:
        return (
            "沒問題，我查到的資訊如下：\n"
            f"{best_match[0]}：{best_match[1]}\n"
            "如果還想確認其他細節，也可以繼續問我。"
        )
    return None


def static_answer(question: str) -> str:
    normalized = normalize(question)
    for item in FAQ["answers"]:
        if any(normalize(keyword) in normalized for keyword in item["keywords"]):
            return item["answer"]
    return FAQ["fallback"]


def is_for_bot(event: dict[str, Any]) -> bool:
    if event.get("type") != "message":
        return False
    message = event.get("message") or {}
    if message.get("type") != "text":
        return False
    source_type = (event.get("source") or {}).get("type")
    if source_type == "user":
        return True
    if source_type not in {"group", "room"}:
        return False
    mentionees = (message.get("mention") or {}).get("mentionees") or []
    return any(person.get("isSelf") is True for person in mentionees)


def conversation_id(source: dict[str, Any]) -> str | None:
    if source.get("type") == "group":
        return source.get("groupId")
    if source.get("type") == "room":
        return source.get("roomId")
    return None


def manager_help() -> str:
    return (
        "好的，以下是參訪資料的設定方式：\n"
        "1. @我 建立參訪\n"
        "2. @我 設定活動名稱 LINE 企業參訪\n"
        "3. @我 設定日期 2026/10/20\n"
        "4. @我 設定集合時間 09:30\n"
        "5. @我 設定集合地點 公司一樓\n"
        "也可以自由新增欄位，例如「@我 設定注意事項：請攜帶訪客證」。\n"
        "建立參訪的人會成為這個群組的資料管理者。完成設定後，群組成員就可以直接向我查詢。"
    )


def format_event(event: dict[str, Any]) -> str:
    lines = ["沒問題，以下是這個群組目前的參訪資訊："]
    for field in FIELD_LABELS:
        value = event.get(field)
        if value:
            lines.append(f"{FIELD_LABELS[field]}：{value}")
    for label, value in (event.get("customFields") or {}).items():
        if value:
            lines.append(f"{label}：{value}")
    if len(lines) == 1:
        lines.append("目前還沒有已設定的內容，可以稍等 Zona 協助補充。")
    else:
        lines.append("若現場安排有調整，請以 Zona 的最新公告為準。")
    return "\n".join(lines)


def format_schedule(event: dict[str, Any] | None) -> str:
    if not event:
        return (
            "不好意思，這個群組目前還沒有參訪資料。"
            "可以先稍等 Zona 協助確認，或請主辦人輸入「管理說明」開始設定。"
        )
    date = event.get("eventDate")
    time = event.get("meetingTime")
    if date and time:
        return (
            f"可以的，目前查到這場參訪的日期是 {date}，集合時間是 {time}。"
            "若時間有臨時調整，請以 Zona 的最新公告為準。"
        )
    if date:
        return (
            f"目前查到這場參訪的日期是 {date}，但集合時間還沒有設定。"
            "集合時間可以稍等 Zona 協助補充。"
        )
    if time:
        return (
            f"目前查到這場參訪的集合時間是 {time}，但日期還沒有設定。"
            "日期可以稍等 Zona 協助補充。"
        )
    return (
        "不好意思，目前還查不到這場參訪的日期與集合時間。"
        "可以先稍等 Zona 協助確認，有最新資訊時請以 Zona 的公告為準。"
    )


def format_location(event: dict[str, Any] | None) -> str:
    if not event:
        return (
            "不好意思，這個群組目前還沒有參訪資料。"
            "可以先稍等 Zona 協助確認，或請主辦人輸入「管理說明」開始設定。"
        )
    place = event.get("meetingPlace")
    transportation = event.get("transportation")
    if place and transportation:
        return (
            "沒問題，我查到的資訊如下：\n"
            f"集合地點：{place}\n交通方式：{transportation}\n"
            "若現場安排有調整，請以 Zona 的最新公告為準。"
        )
    if place:
        return (
            f"目前查到的集合地點是「{place}」，但交通方式還沒有設定。"
            "交通資訊可以稍等 Zona 協助補充。"
        )
    if transportation:
        return (
            f"目前查到的交通方式是「{transportation}」，但集合地點還沒有設定。"
            "集合地點可以稍等 Zona 協助補充。"
        )
    return (
        "不好意思，目前還查不到這場參訪的集合地點與交通資訊。"
        "可以先稍等 Zona 協助確認，有最新資訊時請以 Zona 的公告為準。"
    )


def group_answer(event: dict[str, Any], store) -> str:
    source = event.get("source") or {}
    group_key = conversation_id(source)
    user_id = source.get("userId")
    text = (event.get("message") or {}).get("text", "")
    normalized = normalize(text)

    if not group_key:
        return (
            "不好意思，我目前無法辨識這個群組，因此暫時不能讀寫參訪資料。"
            "可以稍等 Zona 協助確認。"
        )

    if "管理說明" in normalized or "設定說明" in normalized:
        return manager_help()

    if "建立參訪" in normalized:
        if not user_id:
            return (
                "不好意思，LINE 目前沒有提供你的使用者識別資訊，因此暫時無法將你設為管理者。"
                "可以稍等 Zona 協助確認。"
            )
        status = store.create(group_key, user_id)
        if status == "created":
            return (
                "好的，已經幫你建立這個群組的參訪資料，你是目前的資料管理者。\n"
                "接著可以輸入「@我 管理說明」查看設定方式。"
            )
        if status == "owned":
            return (
                "這個群組已經建立參訪資料，而且你目前是資料管理者。"
                "需要查看設定方式時，可以輸入「管理說明」。"
            )
        return (
            "不好意思，這個群組已經有資料管理者。"
            "如果需要更換管理者，請稍等 Zona 協助確認。"
        )

    command = SET_COMMAND.search(text)
    if command:
        if not user_id:
            return (
                "不好意思，LINE 目前沒有提供你的使用者識別資訊，因此暫時無法修改資料。"
                "可以稍等 Zona 協助確認。"
            )
        parts = command.groupdict()
        label = parts["label_builtin"] or parts["label_colon"] or parts["label_space"]
        raw_value = (
            parts["value_builtin"] or parts["value_colon"] or parts["value_space"]
        )
        label = label.strip()
        value = raw_value.strip()
        if len(label) > MAX_LABEL_LENGTH:
            return f"不好意思，欄位名稱有點長，請控制在 {MAX_LABEL_LENGTH} 個字以內再試一次。"
        if not value:
            return f"請在「設定{label}」後面加上內容，我才能幫你保存這筆資訊。"
        if len(value) > MAX_FIELD_LENGTH:
            return f"不好意思，{label}的內容有點長，請控制在 {MAX_FIELD_LENGTH} 個字以內再試一次。"
        if label in FIELD_COMMANDS:
            status = store.update(group_key, user_id, FIELD_COMMANDS[label], value)
        else:
            status = store.update_custom(group_key, user_id, label, value)
        if status == "updated":
            return f"好的，已經幫你更新完成：\n{label}：{value}\n群組成員現在可以直接向我查詢這筆資訊。"
        if status == "missing":
            return (
                "不好意思，這個群組目前還沒有參訪資料。"
                "請先輸入「@我 建立參訪」，再進行設定。"
            )
        return (
            "不好意思，只有這個群組的資料管理者可以修改參訪資訊。"
            "如果內容需要調整，可以請資料管理者協助，或稍等 Zona 確認。"
        )

    event_data = store.get(group_key)
    if any(keyword in normalized for keyword in ("活動資訊", "參訪資訊", "全部資訊")):
        return format_event(event_data) if event_data else (
            "不好意思，這個群組目前還沒有參訪資料。"
            "可以先稍等 Zona 協助確認，或請主辦人輸入「管理說明」開始設定。"
        )
    if event_data:
        custom_answer = custom_field_answer(text, event_data)
        if custom_answer:
            return custom_answer
    if any(keyword in normalized for keyword in ("時間", "日期", "幾點", "何時")):
        return format_schedule(event_data)
    if any(keyword in normalized for keyword in ("地點", "哪裡", "地址", "交通", "集合")):
        return format_location(event_data)
    return static_answer(text)


def answer_for_event(event: dict[str, Any], store=None) -> str | None:
    if not is_for_bot(event):
        return None
    source_type = (event.get("source") or {}).get("type")
    text = (event.get("message") or {}).get("text", "")
    normalized = normalize(text)
    group_only = SET_COMMAND.search(text) is not None or any(
        keyword in normalized
        for keyword in (
            "建立參訪",
            "管理說明",
            "活動資訊",
            "參訪資訊",
            "全部資訊",
            "集合時間",
            "集合地點",
            "幾點",
            "何時",
            "哪裡",
            "地址",
            "交通",
        )
    )
    if source_type == "user":
        if group_only:
            return (
                "不好意思，群組專屬資料需要在參訪群組中設定或查詢。"
                "請先把我加入群組，再使用 LINE 的提及功能 @我。"
            )
        return static_answer(text)
    try:
        return group_answer(event, store or get_event_store())
    except EventStoreError:
        return (
            "不好意思，參訪資料庫目前暫時無法連線，請稍後再試。"
            "如果問題持續發生，可以稍等 Zona 協助確認；重要資訊請以 Zona 的最新公告為準。"
        )


def valid_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(signature, expected)


def send_reply(reply_token: str, message: str, access_token: str) -> None:
    payload = json.dumps(
        {"replyToken": reply_token, "messages": [{"type": "text", "text": message}]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        REPLY_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


class Handler(BaseHTTPRequestHandler):
    def respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/ready":
            self.respond(200, {"status": "ok"})
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/webhook":
            self.respond(404, {"error": "not_found"})
            return
        secret = os.environ.get("LINE_CHANNEL_SECRET", "")
        access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        if not secret or not access_token:
            self.respond(503, {"error": "not_configured"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.respond(400, {"error": "invalid_length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self.respond(413, {"error": "invalid_length"})
            return
        body = self.rfile.read(length)
        if not valid_signature(body, self.headers.get("X-Line-Signature", ""), secret):
            self.respond(401, {"error": "invalid_signature"})
            return
        try:
            events = json.loads(body).get("events", [])
            for event in events:
                response = answer_for_event(event)
                if response is not None and event.get("replyToken"):
                    send_reply(event["replyToken"], response, access_token)
        except (ValueError, TypeError, KeyError):
            self.respond(400, {"error": "invalid_payload"})
            return
        except urllib.error.HTTPError as error:
            self.log_error("LINE reply failed with HTTP %s", error.code)
            self.respond(502, {"error": "reply_failed"})
            return
        except urllib.error.URLError:
            self.log_error("LINE reply connection failed")
            self.respond(502, {"error": "reply_failed"})
            return
        self.respond(200, {"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
