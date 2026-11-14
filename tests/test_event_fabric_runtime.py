"""Testes de integração do Event Fabric com EventStore, outbox e consumidores."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC
from pathlib import Path

import pytest

from nexus_core.event_fabric import (
    DeliveryReport,
    EventFabric,
    IdempotentConsumer,
    OutboxStatus,
    TransactionalOutbox,
)
from nexus_core.eventbus import EventBus
from nexus_core.events import Event, Severity
from nexus_core.memory.eventstore import EventStore
from nexus_core.memory.store import Database
from nexus_core.recorder import EventRecorder


def build_stack(tmp_path: Path, sink=None):
    database = Database(tmp_path / "events.db")
    database.connect()
    database.migrate()
    store = EventStore(database)
    outbox = TransactionalOutbox(database)
    fabric = EventFabric(
        store,
        outbox,
        sink=sink,
        backoff_base_s=0.0,
        poll_interval_s=0.01,
    )
    return database, store, outbox, fabric


def run(coro):
    return asyncio.run(coro)


def test_emit_persists_and_marks_delivered_in_shared_database(tmp_path):
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("test.*", received.append)
    database, store, outbox, fabric = build_stack(tmp_path, sink=bus.publish)
    try:
        event = Event(kind="test.event", source="test", payload={"x": 1})
        stored = run(fabric.emit(event))

        assert stored.event_id == event.event_id
        assert store.get(event.event_id) is not None
        assert [item.event_id for item in received] == [event.event_id]
        assert outbox.get_pending() == []

        entry = outbox.stats()
        assert entry["by_status"][OutboxStatus.DELIVERED.name] == 1

        # O outbox vive no mesmo banco do EventStore.
        with sqlite3.connect(database.path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert {"events", "outbox", "dead_letter", "consumer_processed"} <= tables
    finally:
        database.close()


def test_duplicate_event_is_not_enqueued_or_republished(tmp_path):
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("*", received.append)
    database, store, outbox, fabric = build_stack(tmp_path, sink=bus.publish)
    try:
        first = Event(kind="test.duplicate", source="test", payload={"x": 1})
        second = Event(kind="test.duplicate", source="test", payload={"x": 1})
        run(fabric.emit(first))
        run(fabric.emit(second))

        assert store.count() == 1
        assert len(received) == 1
        assert outbox.stats()["total"] == 1
        assert outbox.stats()["by_status"][OutboxStatus.DELIVERED.name] == 1
    finally:
        database.close()


def test_enqueue_failure_rolls_back_event_store(tmp_path, monkeypatch):
    database, store, outbox, fabric = build_stack(tmp_path)
    try:
        def boom(*args, **kwargs):
            raise RuntimeError("enqueue indisponível")

        monkeypatch.setattr(outbox, "enqueue", boom)
        event = Event(kind="test.rollback", source="test", payload={"x": 1})

        with pytest.raises(RuntimeError, match="enqueue indisponível"):
            run(fabric.emit(event))

        # Sem o rollback, o evento ficaria no store e nunca seria entregue.
        assert store.count() == 0
        assert outbox.stats()["total"] == 0
    finally:
        database.close()


def test_retry_with_backoff_until_success(tmp_path):
    attempts: list[str] = []
    delivered: list[Event] = []

    def flaky(event: Event) -> int:
        attempts.append(event.event_id)
        if len(attempts) < 3:
            raise ConnectionError("bridge offline")
        delivered.append(event)
        return 1

    database, _store, outbox, fabric = build_stack(tmp_path, sink=flaky)
    try:
        event = Event(kind="test.retry", source="test", payload={"x": 1})
        run(fabric.emit(event))
        assert outbox.get_pending()[0].attempts == 1

        report = run(fabric.deliver_pending())
        assert report == DeliveryReport(failed=1)
        assert outbox.get_pending()[0].attempts == 2

        report = run(fabric.deliver_pending())
        assert report == DeliveryReport(delivered=1)
        assert delivered == [event]
        assert outbox.get_pending() == []
        assert outbox.stats()["by_status"][OutboxStatus.DELIVERED.name] == 1
    finally:
        database.close()


def test_exhausted_retries_move_to_dead_letter_and_requeue(tmp_path):
    calls: list[str] = []

    def always_fails(event: Event) -> int:
        calls.append(event.event_id)
        raise TimeoutError("sem resposta")

    database, _store, outbox, fabric = build_stack(tmp_path, sink=always_fails)
    try:
        event = Event(kind="test.dead", source="test", payload={"x": 1})
        run(fabric.emit(event, max_attempts=2))

        report = run(fabric.deliver_pending())
        assert report == DeliveryReport(dead_letter=1)
        dead = outbox.list_dead_letter()
        assert len(dead) == 1
        assert dead[0].event.event_id == event.event_id
        assert dead[0].failed_attempts == 2
        assert "TimeoutError" in dead[0].reason

        dead_entry_id = dead[0].entry_id
        assert outbox.requeue_from_dead_letter(dead_entry_id) is True
        pending = outbox.get_pending()
        assert len(pending) == 1
        assert pending[0].event.event_id == event.event_id
        assert pending[0].attempts == 0
        assert outbox.list_dead_letter() == []
    finally:
        database.close()


def test_requeue_recreates_missing_outbox_row_with_same_identity(tmp_path):
    def always_fails(event: Event) -> int:
        raise RuntimeError("offline")

    database, _store, outbox, fabric = build_stack(tmp_path, sink=always_fails)
    try:
        event = Event(kind="test.requeue", source="test", payload={"x": 1})
        run(fabric.emit(event, max_attempts=1))
        dead = outbox.list_dead_letter()[0]
        original_entry_id = dead.original_entry_id

        with sqlite3.connect(database.path) as conn:
            conn.execute("DELETE FROM outbox WHERE entry_id = ?", (original_entry_id,))

        assert outbox.requeue_from_dead_letter(dead.entry_id) is True
        pending = outbox.get_pending()
        assert len(pending) == 1
        assert pending[0].entry_id == original_entry_id
        assert pending[0].event.event_id == event.event_id
        assert pending[0].attempts == 0
    finally:
        database.close()


def test_pending_entry_recovers_after_fabric_restart(tmp_path):
    database, store, outbox, _fabric = build_stack(
        tmp_path, sink=lambda event: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    try:
        event = Event(kind="test.restart", source="test", payload={"x": 1})
        run(_fabric.emit(event))
        assert outbox.get_pending()[0].event.event_id == event.event_id

        received: list[Event] = []
        restarted = EventFabric(
            store,
            outbox,
            sink=received.append,
            backoff_base_s=0.0,
        )
        report = run(restarted.deliver_pending())

        assert report == DeliveryReport(delivered=1)
        assert [item.event_id for item in received] == [event.event_id]
        assert outbox.get_pending() == []
    finally:
        database.close()


def test_dedup_flag_survives_outbox_and_dead_letter(tmp_path):
    def always_fails(event: Event) -> int:
        raise RuntimeError("offline")

    database, _store, outbox, fabric = build_stack(tmp_path, sink=always_fails)
    try:
        event = Event(
            kind="test.unsafe",
            source="test",
            payload={"n": 9_007_199_254_740_992},
        )
        assert event.dedup is True
        assert event.dedup_key() is None

        run(fabric.emit(event, max_attempts=1))
        dead = outbox.list_dead_letter()[0]
        assert dead.event.dedup is True
        assert dead.event.dedup_key() is None

        assert outbox.requeue_from_dead_letter(dead.entry_id) is True
        pending = outbox.get_pending()[0]
        assert pending.event.event_id == event.event_id
        assert pending.event.dedup is True
        assert pending.event.dedup_key() is None
    finally:
        database.close()


def test_consumer_deduplicates_by_event_and_dedup_key(tmp_path):
    database, _store, outbox, _fabric = build_stack(tmp_path)
    try:
        seen: list[str] = []

        consumer = IdempotentConsumer(
            outbox, "memory-writer", lambda event: seen.append(event.event_id)
        )
        first = Event(kind="test.consume", source="test", payload={"x": 1})
        duplicate = Event(kind="test.consume", source="test", payload={"x": 1})

        assert run(consumer.handle(first)) is True
        assert run(consumer.handle(first)) is False
        assert run(consumer.handle(duplicate)) is False
        assert seen == [first.event_id]
        assert consumer.stats()["processed"] == 1
        assert consumer.stats()["skipped"] == 2

        # Outra instância do consumidor mantém a memória durável.
        restarted = IdempotentConsumer(
            outbox, "memory-writer", lambda event: seen.append(event.event_id)
        )
        assert run(restarted.handle(first)) is False
        assert seen == [first.event_id]
    finally:
        database.close()


def test_consumer_failure_does_not_mark_event_processed(tmp_path):
    database, _store, outbox, _fabric = build_stack(tmp_path)
    try:
        attempts: list[str] = []

        def handler(event: Event) -> None:
            attempts.append(event.event_id)
            if len(attempts) == 1:
                raise RuntimeError("falha transitória")

        consumer = IdempotentConsumer(outbox, "flaky", handler)
        event = Event(kind="test.failure", source="test", payload={"x": 1})

        with pytest.raises(RuntimeError, match="falha transitória"):
            run(consumer.handle(event))
        assert run(consumer.handle(event)) is True
        assert attempts == [event.event_id, event.event_id]
        assert consumer.stats()["processed"] == 1
    finally:
        database.close()


def test_consumer_subscribes_to_bus_and_skips_duplicates(tmp_path):
    bus = EventBus()
    database, _store, outbox, fabric = build_stack(tmp_path, sink=bus.publish)
    try:
        seen: list[str] = []
        consumer = IdempotentConsumer(
            outbox, "auditor", lambda event: seen.append(event.event_id)
        )
        consumer.subscribe(bus, "test.*")

        event = Event(kind="test.bus", source="test", payload={"x": 1})
        run(fabric.emit(event))
        # Reentrega explícita do outbox não deve repetir o efeito.
        run(bus.publish(event))

        assert seen == [event.event_id]
        assert consumer.stats() == {
            "consumer_id": "auditor",
            "processed": 1,
            "skipped": 1,
        }
    finally:
        database.close()


def test_recorder_uses_outbox_when_fabric_is_injected(tmp_path):
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("*", received.append)
    database, store, outbox, fabric = build_stack(tmp_path, sink=bus.publish)
    try:
        recorder = EventRecorder(store, bus, fabric=fabric)
        event = Event(kind="test.recorder", source="test", payload={"x": 1})
        stored = run(recorder.record(event))
        run(recorder.record(event))

        assert stored.event_id == event.event_id
        assert received == [event]
        assert outbox.stats()["total"] == 1
        assert outbox.stats()["by_status"][OutboxStatus.DELIVERED.name] == 1
    finally:
        database.close()


def test_fabric_pump_processes_pending_entries_and_stops(tmp_path):
    delivered: list[Event] = []

    def sink(event: Event) -> int:
        delivered.append(event)
        return 1

    database, _store, outbox, fabric = build_stack(tmp_path, sink=sink)
    try:
        # Enfileira sem entregar para exercitar o pump de verdade.
        event = Event(kind="test.pump", source="test", payload={"x": 1})
        entry_id = outbox.enqueue(event)
        assert outbox.get_entry(entry_id).status is OutboxStatus.PENDING

        async def exercise() -> None:
            fabric.start()
            for _ in range(100):
                if not outbox.get_pending():
                    break
                await asyncio.sleep(0.01)
            await fabric.stop()

        run(exercise())
        assert delivered == [event]
        assert fabric.running is False
        assert outbox.get_pending() == []
    finally:
        database.close()


def test_legacy_outbox_without_dedup_column_is_migrated(tmp_path):
    """Bancos antigos não têm a coluna dedup; a presença de dedup_key decide."""
    db_path = tmp_path / "legacy-outbox.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE outbox (
                entry_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload TEXT NOT NULL,
                dedup_key TEXT,
                correlation_id TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_retry_at REAL,
                last_error TEXT,
                recorded_at REAL NOT NULL,
                delivered_at REAL,
                metadata TEXT
            )
            """
        )
        payload = '{"x":1}'
        created_at = "2026-11-14T12:00:00+00:00"
        rows = [
            ("entry-legacy-sem-dedup", "event-legacy-1", None),
            ("entry-legacy-com-dedup", "event-legacy-2", "chave-derivada"),
        ]
        for entry_id, event_id, dedup_key in rows:
            conn.execute(
                """
                INSERT INTO outbox
                (entry_id, event_id, kind, source, severity, payload, dedup_key,
                 correlation_id, created_at, status, attempts, max_attempts,
                 next_retry_at, last_error, recorded_at, delivered_at, metadata)
                VALUES (?, ?, 'test.legacy', 'test', 'info', ?, ?, NULL, ?,
                        'PENDING', 0, 5, 0.0, NULL, 1.0, NULL, '{}')
                """,
                (entry_id, event_id, payload, dedup_key, created_at),
            )
        conn.commit()

    outbox = TransactionalOutbox(db_path)
    entries = {entry.entry_id: entry for entry in outbox.get_pending()}
    assert entries["entry-legacy-sem-dedup"].event.dedup is False
    assert entries["entry-legacy-sem-dedup"].event.dedup_key() is None
    assert entries["entry-legacy-com-dedup"].event.dedup is True

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    assert "dedup" in columns


def test_transactional_outbox_supports_standalone_database(tmp_path):
    outbox = TransactionalOutbox(tmp_path / "standalone-outbox.db")
    event = Event(kind="test.standalone", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(event)

    entry = outbox.get_pending()[0]
    assert entry.entry_id == entry_id
    assert entry.event.severity == Severity.INFO
    assert entry.event.created_at.tzinfo == UTC
    assert entry.event.dedup_key() == event.dedup_key()

    with sqlite3.connect(outbox.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    assert "dedup" in columns
    assert "metadata" in columns
