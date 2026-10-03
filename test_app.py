import base64
import hashlib
import hmac
import unittest

from app import answer_for_event, valid_signature
from event_store import InMemoryEventStore


def text_event(
    source_type,
    text,
    *,
    user_id="U-manager",
    conversation_id="G-school-a",
    mentioned=True,
):
    source = {"type": source_type, "userId": user_id}
    if source_type == "group":
        source["groupId"] = conversation_id
    elif source_type == "room":
        source["roomId"] = conversation_id
    message = {"type": "text", "text": text}
    if source_type in {"group", "room"} and mentioned:
        message["mention"] = {"mentionees": [{"type": "user", "isSelf": True}]}
    return {"type": "message", "source": source, "message": message}


class GroupMemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryEventStore()

    def ask(self, text, **kwargs):
        return answer_for_event(text_event("group", text, **kwargs), self.store)

    def test_signed_body(self):
        body = b'{"events":[]}'
        signature = base64.b64encode(
            hmac.new(b"secret", body, hashlib.sha256).digest()
        ).decode()
        self.assertTrue(valid_signature(body, signature, "secret"))
        self.assertFalse(valid_signature(body + b" ", signature, "secret"))

    def test_group_ignores_messages_without_self_mention(self):
        event = text_event("group", "活動資訊", mentioned=False)
        self.assertIsNone(answer_for_event(event, self.store))

    def test_creator_becomes_manager_and_can_update(self):
        self.assertIn("你是目前的資料管理者", self.ask("@Bot 建立參訪"))
        self.assertIn(
            "活動名稱：XX 大學企業參訪",
            self.ask("@Bot 設定活動名稱 XX 大學企業參訪"),
        )
        self.assertIn("日期：2026/10/20", self.ask("@Bot 設定日期 2026/10/20"))
        self.assertIn("集合時間：09:30", self.ask("@Bot 設定集合時間 09:30"))
        reply = self.ask("@Bot 活動資訊")
        self.assertIn("活動名稱：XX 大學企業參訪", reply)
        self.assertIn("日期：2026/10/20", reply)
        self.assertIn("集合時間：09:30", reply)

    def test_non_manager_cannot_change_event(self):
        self.ask("@Bot 建立參訪")
        reply = self.ask("@Bot 設定日期 2026/12/01", user_id="U-student")
        self.assertIn("只有這個群組的資料管理者", reply)
        self.assertNotIn("eventDate", self.store.get("G-school-a"))

    def test_manager_can_create_and_query_any_field(self):
        self.ask("@Bot 建立參訪")
        reply = self.ask("@Bot 設定注意事項：參訪需全程攜帶訪客證")
        self.assertIn("注意事項：參訪需全程攜帶訪客證", reply)
        self.assertIn(
            "注意事項：參訪需全程攜帶訪客證",
            self.ask("@Bot 注意事項"),
        )
        self.assertIn("注意事項：參訪需全程攜帶訪客證", self.ask("@Bot 活動資訊"))

        self.assertIn(
            "Dress Code：Business Casual",
            self.ask("@Bot 設定 Dress Code：Business Casual"),
        )
        self.assertIn("Dress Code：Business Casual", self.ask("@Bot Dress Code"))

    def test_natural_question_matches_custom_field(self):
        self.ask("@Bot 建立參訪")
        self.ask("@Bot 設定遊覽車停車地點：大港墘公園")
        self.assertIn(
            "遊覽車停車地點：大港墘公園",
            self.ask("@Bot 遊覽車可以停哪裡"),
        )

    def test_generic_location_question_does_not_select_unrelated_custom_field(self):
        self.ask("@Bot 建立參訪")
        self.ask("@Bot 設定遊覽車停車地點：大港墘公園")
        reply = self.ask("@Bot 集合地點在哪裡")
        self.assertIn("還查不到這場參訪的集合地點與交通資訊", reply)
        self.assertIn("Zona", reply)

    def test_non_manager_cannot_change_custom_field(self):
        self.ask("@Bot 建立參訪")
        reply = self.ask(
            "@Bot 設定報到方式：一樓櫃台報到",
            user_id="U-student",
        )
        self.assertIn("只有這個群組的資料管理者", reply)
        self.assertNotIn("customFields", self.store.get("G-school-a"))

    def test_groups_keep_separate_event_data(self):
        self.ask("@Bot 建立參訪", conversation_id="G-school-a")
        self.ask("@Bot 設定集合地點 公司一樓", conversation_id="G-school-a")
        self.ask("@Bot 建立參訪", conversation_id="G-school-b")
        self.ask("@Bot 設定集合地點 捷運站二號出口", conversation_id="G-school-b")
        self.assertIn("公司一樓", self.ask("@Bot 集合地點", conversation_id="G-school-a"))
        self.assertIn(
            "捷運站二號出口",
            self.ask("@Bot 集合地點", conversation_id="G-school-b"),
        )

    def test_unknown_group_does_not_invent_activity_data(self):
        reply = self.ask("@Bot 幾點集合？", conversation_id="G-new")
        self.assertIn("目前還沒有參訪資料", reply)
        self.assertIn("Zona", reply)

    def test_direct_message_cannot_modify_group_data(self):
        for command in ("設定日期 2026/10/20", "設定注意事項：請攜帶訪客證"):
            with self.subTest(command=command):
                event = text_event("user", command)
                reply = answer_for_event(event, self.store)
                self.assertIn("需要在參訪群組中", reply)

    def test_direct_message_redirects_group_specific_queries(self):
        for question in ("幾點集合？", "集合地點在哪裡？", "交通方式是什麼？"):
            with self.subTest(question=question):
                event = text_event("user", question)
                reply = answer_for_event(event, self.store)
                self.assertIn("需要在參訪群組中", reply)

    def test_direct_message_can_ask_general_question(self):
        event = text_event("user", "參訪要準備什麼？")
        reply = answer_for_event(event, self.store)
        self.assertIn("出發前建議先確認", reply)
        self.assertIn("Zona", reply)

    def test_unknown_question_politely_waits_for_zona(self):
        reply = self.ask("@Bot 公司附近有便利商店嗎？")
        self.assertIn("不好意思", reply)
        self.assertIn("稍等 Zona 協助確認", reply)

    def test_non_text_is_ignored(self):
        event = text_event("user", "功能")
        event["message"]["type"] = "image"
        self.assertIsNone(answer_for_event(event, self.store))


if __name__ == "__main__":
    unittest.main()
