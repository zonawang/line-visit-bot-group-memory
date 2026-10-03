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
REPLY_URL = "https://api.line.me/v2/bot/message/reply"

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
    r"設定\s*(活動名稱|日期|集合時間|集合地點|交通|聯絡人)\s*(?:[:：]\s*)?(.+)",
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
        "主辦人設定方式：\n"
        "1. @我 建立參訪\n"
        "2. @我 設定活動名稱 LINE 企業參訪\n"
        "3. @我 設定日期 2026/10/20\n"
        "4. @我 設定集合時間 09:30\n"
        "5. @我 設定集合地點 公司一樓\n"
        "也可以設定交通或聯絡人。建立參訪的人會成為這個群組的資料管理者。"
    )


def format_event(event: dict[str, Any]) -> str:
    lines = ["這個群組的參訪資訊："]
    for field in FIELD_LABELS:
        value = event.get(field)
        if value:
            lines.append(f"{FIELD_LABELS[field]}：{value}")
    if len(lines) == 1:
        lines.append("資料尚未設定。請主辦人輸入「管理說明」。")
    return "\n".join(lines)


def format_schedule(event: dict[str, Any] | None) -> str:
    if not event:
        return "這個群組尚未建立參訪資料。請主辦人輸入「管理說明」。"
    date = event.get("eventDate")
    time = event.get("meetingTime")
    if date and time:
        return f"這場參訪的日期是 {date}，集合時間是 {time}。"
    if date:
        return f"這場參訪的日期是 {date}；集合時間尚未設定。"
    if time:
        return f"這場參訪的集合時間是 {time}；日期尚未設定。"
    return "這場參訪的日期與集合時間尚未設定，請以主辦人最新公告為準。"


def format_location(event: dict[str, Any] | None) -> str:
    if not event:
        return "這個群組尚未建立參訪資料。請主辦人輸入「管理說明」。"
    place = event.get("meetingPlace")
    transportation = event.get("transportation")
    if place and transportation:
        return f"集合地點：{place}\n交通方式：{transportation}"
    if place:
        return f"集合地點：{place}\n交通方式尚未設定。"
    if transportation:
        return f"集合地點尚未設定。\n交通方式：{transportation}"
    return "這場參訪的集合地點與交通資訊尚未設定，請以主辦人最新公告為準。"


def group_answer(event: dict[str, Any], store) -> str:
    source = event.get("source") or {}
    group_key = conversation_id(source)
    user_id = source.get("userId")
    text = (event.get("message") or {}).get("text", "")
    normalized = normalize(text)

    if not group_key:
        return "我找不到這個群組的識別資訊，暫時無法讀寫參訪資料。"

    if "管理說明" in normalized or "設定說明" in normalized:
        return manager_help()

    if "建立參訪" in normalized:
        if not user_id:
            return "LINE 沒有提供你的使用者識別資訊，暫時無法把你設為管理者。"
        status = store.create(group_key, user_id)
        if status == "created":
            return "已建立這個群組的參訪資料，你是目前的資料管理者。\n接著可輸入「@我 管理說明」查看設定方式。"
        if status == "owned":
            return "這個群組已建立參訪資料，而且你是管理者。輸入「管理說明」可查看設定方式。"
        return "這個群組已經有資料管理者。如需更換管理者，請由主辦團隊確認後處理。"

    command = SET_COMMAND.search(text)
    if command:
        if not user_id:
            return "LINE 沒有提供你的使用者識別資訊，暫時無法修改資料。"
        label, raw_value = command.groups()
        value = raw_value.strip()
        if not value:
            return f"請在「設定{label}」後面加上內容。"
        if len(value) > MAX_FIELD_LENGTH:
            return f"{label}太長了，請控制在 {MAX_FIELD_LENGTH} 個字以內。"
        status = store.update(group_key, user_id, FIELD_COMMANDS[label], value)
        if status == "updated":
            return f"已更新{label}：{value}"
        if status == "missing":
            return "這個群組還沒建立參訪資料。請先輸入「@我 建立參訪」。"
        return "只有這個群組的資料管理者可以修改參訪資訊。"

    event_data = store.get(group_key)
    if any(keyword in normalized for keyword in ("活動資訊", "參訪資訊", "全部資訊")):
        return format_event(event_data) if event_data else (
            "這個群組尚未建立參訪資料。請主辦人輸入「管理說明」。"
        )
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
    group_only = any(
        keyword in normalized
        for keyword in (
            "建立參訪",
            "管理說明",
            "設定活動名稱",
            "設定日期",
            "設定集合時間",
            "設定集合地點",
            "設定交通",
            "設定聯絡人",
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
            return "群組專屬資料需要在參訪群組中設定或查詢。請把我加入群組並 @我。"
        return static_answer(text)
    try:
        return group_answer(event, store or get_event_store())
    except EventStoreError:
        return "參訪資料庫目前無法連線，請稍後再試；重要資訊請先以主辦人公告為準。"


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
