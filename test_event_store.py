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


if __name__ == "__main__":
    unittest.main()
