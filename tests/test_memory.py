from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus_core.memory.records import MemoryStore
from nexus_core.memory.store import Database


class MemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "data" / "memory.db")
        self.database.connect()
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.memory = MemoryStore(self.database, vector_dir='/tmp/vectors')

    def test_create_list_and_get_memory(self) -> None:
        created = self.memory.create(
            kind="long_term",
            title="Preferred language",
            content="The user prefers Portuguese for product updates.",
            metadata={"source": "explicit_user_input", "confidence": 1.0},
        )
        self.assertEqual(self.memory.get(created.memory_id), created)
        self.assertEqual(self.memory .list_all(kind="long_term")[0], created)
        self.assertEqual(self.memory.stats(), {"long_term": 1, "total": 1})

    def test_full_text_search_and_kind_filter(self) -> None:
        matching = self.memory.create(
            kind="episodic", title="Project decision", content="Use SQLite for local persistence."
        )
        self.memory.create(kind="semantic", content="The sky appears blue in daylight.")
        results = self.memory.search("SQLite local")
        self.assertEqual([item.memory_id for item in results], [matching.memory_id])
        self.assertEqual(self.memory.search("SQLite", kind="semantic"), [])

    def test_hybrid_search_filters_semantic_results_by_kind(self) -> None:
        episodic = self.memory.create(
            kind="episodic", content="Deploy release notes for the service."
        )
        semantic = self.memory.create(
            kind="semantic", content="Deploy release notes for the service."
        )
        fake_vectors = MagicMock()
        fake_vectors.search.return_value = [
            SimpleNamespace(memory_id=episodic.memory_id),
            SimpleNamespace(memory_id=semantic.memory_id),
        ]
        self.memory._vector_store = fake_vectors

        results = self.memory.search("deploy", kind="semantic", hybrid=True)

        self.assertEqual([item.memory_id for item in results], [semantic.memory_id])

    def test_delete_removes_record_from_search_index(self) -> None:
        record = self.memory.create(kind="semantic", content="Use an emerald accent color.")
        self.assertTrue(self.memory.delete(record.memory_id))
        self.assertFalse(self.memory.delete(record.memory_id))
        self.assertIsNone(self.memory.get(record.memory_id))
        self.assertEqual(self.memory.search("emerald"), [])

    def test_expiration_is_opt_in_and_purge_is_explicit(self) -> None:
        record = self.memory.create(
            kind="short_term",
            content="Temporary task context",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.assertEqual(len(self.memory .list_all()), 1)
        self.assertEqual(
            self.memory.purge_expired(now=datetime.now(timezone.utc) + timedelta(days=2)), 1
        )
        self.assertIsNone(self.memory.get(record.memory_id))

    def test_expired_records_are_not_counted_as_active_memory(self) -> None:
        record = self.memory.create(
            kind="short_term",
            content="Expired context",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.database.execute(
            "UPDATE memories SET expires_at = ? WHERE memory_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), record.memory_id),
        )
        self.assertEqual(self.memory.stats(), {"total": 0})
        self.assertEqual(self.memory .list_all(), [])

    def test_invalid_records_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.create(kind="unknown", content="text")
        with self.assertRaises(ValueError):
            self.memory.create(kind="long_term", content=" ")
        with self.assertRaises(ValueError):
            self.memory.create(kind="long_term", content="text", metadata={"bad": object()})
        with self.assertRaises(ValueError):
            self.memory.create(
                kind="long_term",
                content="text",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
