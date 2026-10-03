"""Persistent event data for each LINE group or room."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any


class EventStoreError(RuntimeError):
    """Raised when the event database cannot be used."""


class InMemoryEventStore:
    """Small test double with the same behavior as the Firestore store."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            event = self._events.get(conversation_id)
            return deepcopy(event) if event else None

    def create(self, conversation_id: str, manager_user_id: str) -> str:
        with self._lock:
            existing = self._events.get(conversation_id)
            if existing:
                return "owned" if existing["managerUserId"] == manager_user_id else "exists"
            self._events[conversation_id] = {
                "managerUserId": manager_user_id,
                "createdAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc),
            }
            return "created"

    def update(
        self, conversation_id: str, manager_user_id: str, field: str, value: str
    ) -> str:
        with self._lock:
            event = self._events.get(conversation_id)
            if not event:
                return "missing"
            if event["managerUserId"] != manager_user_id:
                return "forbidden"
            event[field] = value
            event["updatedAt"] = datetime.now(timezone.utc)
            return "updated"


class FirestoreEventStore:
    """Firestore implementation using one document per LINE conversation."""

    def __init__(self, collection: str = "visit_group_events") -> None:
        try:
            from google.cloud import firestore
        except ImportError as error:  # pragma: no cover - only possible in a bad image
            raise EventStoreError("google-cloud-firestore is not installed") from error
        self._firestore = firestore
        self._client = firestore.Client()
        self._collection = self._client.collection(collection)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            snapshot = self._collection.document(conversation_id).get()
            return snapshot.to_dict() if snapshot.exists else None
        except Exception as error:  # Google SDK exposes several transport errors
            raise EventStoreError("Firestore read failed") from error

    def create(self, conversation_id: str, manager_user_id: str) -> str:
        document = self._collection.document(conversation_id)
        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def create_in_transaction(current_transaction):
            snapshot = document.get(transaction=current_transaction)
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                return "owned" if data.get("managerUserId") == manager_user_id else "exists"
            current_transaction.set(
                document,
                {
                    "managerUserId": manager_user_id,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
            )
            return "created"

        try:
            return create_in_transaction(transaction)
        except Exception as error:
            raise EventStoreError("Firestore create failed") from error

    def update(
        self, conversation_id: str, manager_user_id: str, field: str, value: str
    ) -> str:
        document = self._collection.document(conversation_id)
        transaction = self._client.transaction()
        firestore = self._firestore

        @firestore.transactional
        def update_in_transaction(current_transaction):
            snapshot = document.get(transaction=current_transaction)
            if not snapshot.exists:
                return "missing"
            data = snapshot.to_dict() or {}
            if data.get("managerUserId") != manager_user_id:
                return "forbidden"
            current_transaction.update(
                document,
                {field: value, "updatedAt": firestore.SERVER_TIMESTAMP},
            )
            return "updated"

        try:
            return update_in_transaction(transaction)
        except Exception as error:
            raise EventStoreError("Firestore update failed") from error
