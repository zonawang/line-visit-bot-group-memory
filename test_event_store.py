import unittest

from event_store import InMemoryEventStore


class InMemoryEventStoreTests(unittest.TestCase):
    def test_create_is_idempotent_for_owner(self):
        store = InMemoryEventStore()
        self.assertEqual("created", store.create("G1", "U1"))
        self.assertEqual("owned", store.create("G1", "U1"))
        self.assertEqual("exists", store.create("G1", "U2"))

    def test_update_requires_owner(self):
        store = InMemoryEventStore()
        self.assertEqual("missing", store.update("G1", "U1", "eventDate", "2026/10/20"))
        store.create("G1", "U1")
        self.assertEqual("forbidden", store.update("G1", "U2", "eventDate", "2026/10/20"))
        self.assertEqual("updated", store.update("G1", "U1", "eventDate", "2026/10/20"))
        self.assertEqual("2026/10/20", store.get("G1")["eventDate"])

    def test_custom_field_update_requires_owner(self):
        store = InMemoryEventStore()
        self.assertEqual(
            "missing",
            store.update_custom("G1", "U1", "注意事項", "請攜帶訪客證"),
        )
        store.create("G1", "U1")
        self.assertEqual(
            "forbidden",
            store.update_custom("G1", "U2", "注意事項", "請攜帶訪客證"),
        )
        self.assertEqual(
            "updated",
            store.update_custom("G1", "U1", "注意事項", "請攜帶訪客證"),
        )
        self.assertEqual(
            "請攜帶訪客證",
            store.get("G1")["customFields"]["注意事項"],
        )


if __name__ == "__main__":
    unittest.main()
