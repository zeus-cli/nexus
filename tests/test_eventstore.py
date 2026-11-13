from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nexus_core.events import Event, Severity
from nexus_core.eventbus import EventBus
from nexus_core.memory.eventstore import CorruptEventError, EventStore, EventStoreError
from nexus_core.memory.store import Database
from nexus_core.recorder import EventRecorder


def build_store(root: Path, **kwargs) -> tuple[Database, EventStore]:
    database = Database(root / "data" / "databases" / "zeus_events.db")
    database.connect()
    database.migrate()
    return database, EventStore(database, **kwargs)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.database, self.store = build_store(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.database.close)


class SchemaTest(StoreTestCase):
    def test_wal_is_enabled(self) -> None:
        mode = self.database.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_migrations_are_idempotent(self) -> None:
        self.assertEqual(self.database.schema_version(), 3)
        self.assertEqual(self.database.migrate(), [])
        self.assertEqual(self.database.pending_migrations(), [])

    def test_integrity_check_ok(self) -> None:
        self.assertEqual(self.database.integrity_check(), "ok")


class AppendQueryTest(StoreTestCase):
    def test_roundtrip_preserves_fields(self) -> None:
        event = Event(
            kind="perception.fs.created",
            source="watcher_rs",
            payload={"path": "/tmp/a.txt", "size": 12},
            severity=Severity.WARNING,
            correlation_id="corr-1",
        )
        stored, inserted = self.store.append(event)
        self.assertTrue(inserted)
        self.assertEqual(stored.event_id, event.event_id)

        loaded = self.store.get(event.event_id)
        assert loaded is not None
        self.assertEqual(loaded.kind, event.kind)
        self.assertEqual(loaded.source, "watcher_rs")
        self.assertEqual(dict(loaded.payload), {"path": "/tmp/a.txt", "size": 12})
        self.assertEqual(loaded.severity, Severity.WARNING)
        self.assertEqual(loaded.correlation_id, "corr-1")
        self.assertEqual(loaded.created_at, event.created_at)

    def test_dedup_within_window(self) -> None:
        event = Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
        first, inserted_first = self.store.append(event)
        second, inserted_second = self.store.append(
            Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
        )
        self.assertTrue(inserted_first)
        self.assertFalse(inserted_second)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(self.store.count(), 1)

    def test_different_payload_is_not_deduplicated(self) -> None:
        self.store.append(Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1}))
        self.store.append(Event(kind="system.heartbeat", source="app", payload={"uptime_s": 2}))
        self.assertEqual(self.store.count(), 2)

    def test_dedup_disabled_when_window_is_zero(self) -> None:
        database, store = build_store(self.root / "dedup-off", dedup_window_hours=0)
        self.addCleanup(database.close)
        for _ in range(2):
            self.assertTrue(
                store.append(
                    Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
                )[1]
            )
        self.assertEqual(store.count(), 2)

    def test_event_id_cannot_be_reused_with_different_content(self) -> None:
        event = Event(kind="a.b", source="s", payload={"n": 1})
        self.assertTrue(self.store.append(event)[1])
        conflicting = Event(kind="a.b", source="s", payload={"n": 2}, event_id=event.event_id)
        with self.assertRaises(EventStoreError):
            self.store.append(conflicting)
        self.assertEqual(self.store.count(), 1)

    def test_dedup_expires_outside_window(self) -> None:
        event = Event(
            kind="system.heartbeat",
            source="app",
            payload={"uptime_s": 1},
            created_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        self.assertTrue(self.store.append(event)[1])
        self.assertTrue(
            self.store.append(
                Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
            )[1]
        )
        self.assertEqual(self.store.count(), 2)

    def test_dedup_opt_out_allows_repeated_lifecycle_events(self) -> None:
        for _ in range(3):
            _, inserted = self.store.append(
                Event(
                    kind="system.api.stopped",
                    source="api",
                    payload={"sessions_closed": 0},
                    dedup=False,
                )
            )
            self.assertTrue(inserted)
        self.assertEqual(self.store.count(), 3)

    def test_roundtrip_preserves_dedup_opt_out(self) -> None:
        event = Event(
            kind="system.api.stopped",
            source="api",
            payload={"sessions_closed": 0},
            dedup=False,
        )
        self.assertTrue(self.store.append(event)[1])

        loaded = self.store.get(event.event_id)
        assert loaded is not None
        self.assertFalse(loaded.dedup)
        self.assertIsNone(loaded.to_dict()["dedup_key"])

    def test_roundtrip_preserves_deduplicable_events(self) -> None:
        event = Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
        self.assertTrue(self.store.append(event)[1])

        loaded = self.store.get(event.event_id)
        assert loaded is not None
        self.assertTrue(loaded.dedup)
        self.assertEqual(loaded.to_dict()["dedup_key"], event.dedup_key())

    def test_filters_and_ordering(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        for index in range(5):
            self.store.append(
                Event(
                    kind="agent.step" if index % 2 == 0 else "agent.plan",
                    source="agent_manager",
                    payload={"step": index},
                    severity=Severity.INFO if index < 4 else Severity.ERROR,
                    created_at=base + timedelta(minutes=index),
                    correlation_id="run-1" if index < 3 else None,
                )
            )

        newest = self.store.query(limit=2)
        self.assertEqual([dict(e.payload)["step"] for e in newest], [4, 3])

        oldest = self.store.query(limit=2, oldest_first=True)
        self.assertEqual([dict(e.payload)["step"] for e in oldest], [0, 1])

        page = self.store.query(limit=2, offset=2)
        self.assertEqual([dict(e.payload)["step"] for e in page], [2, 1])

        self.assertEqual(len(self.store.query(kind="agent.plan")), 2)
        self.assertEqual(len(self.store.query(severity="error")), 1)
        self.assertEqual(len(self.store.query(severity=Severity.INFO)), 4)
        self.assertEqual(len(self.store.query(correlation_id="run-1")), 3)
        self.assertEqual(len(self.store.query(since=base + timedelta(minutes=3))), 2)
        self.assertEqual(len(self.store.query(source="agent_manager")), 5)

    def test_invalid_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.query(limit=0)
        with self.assertRaises(ValueError):
            self.store.query(severity="ruidoso")
        with self.assertRaises(ValueError):
            self.store.count(since="não é data")

    def test_kinds_aggregation(self) -> None:
        self.store.append(Event(kind="a.b", source="s", payload={"n": 1}))
        self.store.append(Event(kind="a.b", source="s", payload={"n": 2}))
        self.store.append(Event(kind="a.c", source="s", payload={"n": 3}))
        self.assertEqual(
            self.store.list_kinds(), [{"kind": "a.b", "count": 2}, {"kind": "a.c", "count": 1}]
        )

    def test_payload_limit_enforced(self) -> None:
        store = EventStore(self.database, max_payload_bytes=64)
        with self.assertRaises(ValueError):
            store.append(Event(kind="a.b", source="s", payload={"blob": "x" * 200}))


class RecorderDedupTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_event_is_not_republished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database, store = build_store(Path(tmp))
            self.addCleanup(database.close)
            bus = EventBus()
            delivered: list[str] = []
            bus.subscribe("*", lambda event: delivered.append(event.event_id))
            recorder = EventRecorder(store, bus)

            first = await recorder.record(
                Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
            )
            second = await recorder.record(
                Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
            )

            self.assertEqual(first.event_id, second.event_id)
            self.assertEqual(delivered, [first.event_id])

    async def test_distinct_events_are_both_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database, store = build_store(Path(tmp))
            self.addCleanup(database.close)
            bus = EventBus()
            delivered: list[str] = []
            bus.subscribe("*", lambda event: delivered.append(event.event_id))
            recorder = EventRecorder(store, bus)

            first = await recorder.record(
                Event(kind="system.heartbeat", source="app", payload={"uptime_s": 1})
            )
            second = await recorder.record(
                Event(kind="system.heartbeat", source="app", payload={"uptime_s": 2})
            )

            self.assertNotEqual(first.event_id, second.event_id)
            self.assertEqual(delivered, [first.event_id, second.event_id])


class MaintenanceTest(StoreTestCase):
    def test_retention_removes_old_events_only(self) -> None:
        store = EventStore(self.database, retention_days=90)
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        store.append(
            Event(kind="a.old", source="s", payload={"n": 1},
                  created_at=now - timedelta(days=120))
        )
        store.append(
            Event(kind="a.new", source="s", payload={"n": 2}, created_at=now - timedelta(days=1))
        )
        self.assertEqual(store.apply_retention(now=now), 1)
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.query()[0].kind, "a.new")

    def test_retention_disabled(self) -> None:
        database, store = build_store(self.root / "no-retention", retention_days=0)
        self.addCleanup(database.close)
        store.append(Event(kind="a.old", source="s", payload={},
                           created_at=datetime.now(timezone.utc) - timedelta(days=500)))
        self.assertEqual(store.apply_retention(), 0)
        self.assertEqual(store.count(), 1)

    def test_default_retention_does_not_delete_history(self) -> None:
        self.store.append(
            Event(
                kind="a.old",
                source="s",
                payload={"n": 1},
                created_at=datetime.now(timezone.utc) - timedelta(days=500),
            )
        )
        self.assertEqual(self.store.apply_retention(), 0)
        self.assertEqual(self.store.count(), 1)

    def test_persistence_across_reconnect(self) -> None:
        path = self.root / "persist" / "data" / "databases" / "zeus_events.db"
        first = Database(path)
        first.connect()
        first.migrate()
        EventStore(first).append(Event(kind="a.b", source="s", payload={"n": 7}))
        first.close()

        second = Database(path)
        second.connect()
        self.addCleanup(second.close)
        self.assertEqual(second.migrate(), [])
        loaded = EventStore(second).query()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(dict(loaded[0].payload), {"n": 7})

    def test_verify_detects_corrupt_payload(self) -> None:
        self.store.append(Event(kind="a.b", source="s", payload={"n": 1}))
        self.database.execute(
            "INSERT INTO events (event_id, kind, source, severity, payload, dedup_key,"
            " correlation_id, created_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("broken", "a.b", "s", "info", "{não-json", None, None,
             datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        report = self.store.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["invalid_payloads"], 1)
        with self.assertRaises(CorruptEventError):
            self.store.get("broken")

    def test_backup_is_readable_and_complete(self) -> None:
        self.store.append(Event(kind="a.b", source="s", payload={"n": 1}))
        target = self.database.backup_to(self.root / "data" / "backups" / "copy.db")
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        copy = sqlite3.connect(str(target))
        try:
            count = copy.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
        self.assertEqual(count, 1)
        self.assertEqual(integrity, "ok")

    def test_stats_shape(self) -> None:
        self.store.append(Event(kind="a.b", source="s", payload={"n": 1}))
        stats = self.store.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["schema_version"], 3)
        self.assertIn("info", stats["by_severity"])
        json.dumps(stats)  # precisa ser serializável para o HUD/API


class EventModelTest(unittest.TestCase):
    def test_kind_and_source_validation(self) -> None:
        for kind in ("Agent.plan", "agent plan", "", "agent..plan"):
            with self.assertRaises(ValueError):
                Event(kind=kind, source="s")
        with self.assertRaises(ValueError):
            Event(kind="a.b", source="")
        with self.assertRaises(ValueError):
            Event(kind="a.b", source="com espaço")

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Event(kind="a.b", source="s", created_at=datetime(2026, 1, 1))

    def test_dedup_opt_out_has_no_key(self) -> None:
        self.assertIsNone(Event(kind="a.b", source="s", dedup=False).dedup_key())
        self.assertEqual(len(Event(kind="a.b", source="s").dedup_key()), 64)

    def test_serialization_is_canonical_and_tz_aware(self) -> None:
        event = Event(kind="a.b", source="s", payload={"b": 1, "a": 2})
        self.assertEqual(event.canonical_payload(), '{"a":2,"b":1}')
        self.assertEqual(event.created_at.tzinfo, timezone.utc)
        self.assertEqual(len(event.dedup_key() or ""), 64)


if __name__ == "__main__":
    unittest.main()
