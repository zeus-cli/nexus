"""Testes do Event Fabric — Transactional Outbox e Dead-Letter Queue."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nexus_core.event_fabric import (
    DeadLetterProcessor,
    OutboxEntry,
    OutboxStatus,
    TransactionalOutbox,
)
from nexus_core.events import Event, Severity


# --------------------------------------------------------------------------- #
# Transactional Outbox
# --------------------------------------------------------------------------- #

def test_outbox_enqueue_and_get_pending(tmp_path):
    """Eventos enfileirados devem aparecer como pendentes."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev)
    assert entry_id is not None

    pending = outbox.get_pending()
    assert len(pending) == 1
    assert pending[0].entry_id == entry_id
    assert pending[0].status == OutboxStatus.PENDING
    assert pending[0].attempts == 0


def test_outbox_mark_delivered(tmp_path):
    """Evento entregue deve ser marcado como DELIVERED."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev)

    assert outbox.mark_delivered(entry_id) is True
    pending = outbox.get_pending()
    assert len(pending) == 0  # não está mais pendente


def test_outbox_mark_nonexistent_delivered(tmp_path):
    """Marcar entrega de entry inexistente deve retornar False."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    assert outbox.mark_delivered("nonexistent") is False


def test_outbox_mark_failed_retries(tmp_path):
    """Falha deve agendar retry com backoff."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev, max_attempts=3)

    # Primeira falha com retry imediato.
    assert outbox.mark_failed(entry_id, "connection error", retry_delay_s=0.0) is True
    pending = outbox.get_pending()
    assert len(pending) == 1
    entry = pending[0]
    assert entry.attempts == 1
    assert entry.last_error == "connection error"
    assert entry.next_retry_at is not None  # agendado para retry


def test_outbox_max_attempts_moves_to_dead_letter(tmp_path):
    """Após max_attempts falhas, evento deve ir para dead-letter."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev, max_attempts=2)

    # Primeira falha -> retry.
    assert outbox.mark_failed(entry_id, "error 1", retry_delay_s=0.0) is True
    # Segunda falha -> dead-letter.
    assert outbox.mark_failed(entry_id, "error 2", retry_delay_s=0.0) is True

    # Não deve mais aparecer como pendente.
    pending = outbox.get_pending()
    assert len(pending) == 0

    # Deve estar no dead-letter.
    dead = outbox.list_dead_letter()
    assert len(dead) == 1
    assert dead[0].reason == "error 2"


def test_outbox_persists_epoch_and_preserves_event_metadata(tmp_path):
    """O outbox deve usar tempo de parede e reconstruir o evento fielmente."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    ev = Event(
        kind="test.event",
        source="test",
        payload={"x": 1},
        severity=Severity.WARNING,
        correlation_id="corr-1",
        created_at=created_at,
    )
    entry_id = outbox.enqueue(ev)

    entry = outbox.get_pending()[0]
    assert entry.entry_id == entry_id
    assert entry.created_at > 1_000_000_000  # epoch, não time.monotonic()
    assert entry.event.created_at == created_at
    assert entry.event.severity == Severity.WARNING
    assert entry.event.correlation_id == "corr-1"
    assert entry.event.dedup_key() == ev.dedup_key()


def test_outbox_enqueue_persists_metadata(tmp_path):
    """Metadados informados no enqueue devem sobreviver à leitura."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    entry_id = outbox.enqueue(
        Event(kind="test.event", source="test", payload={"x": 1}),
        metadata={"trace": "abc", "attempt": 1},
    )

    entry = outbox.get_pending()[0]
    assert entry.entry_id == entry_id
    assert entry.metadata == {"trace": "abc", "attempt": 1}
    assert entry.as_dict()["metadata"] == {"trace": "abc", "attempt": 1}


def test_outbox_metadata_survives_dead_letter_and_requeue(tmp_path):
    """Metadados devem acompanhar o evento até o dead-letter e a volta."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    entry_id = outbox.enqueue(
        Event(kind="test.event", source="test", payload={"x": 1}),
        max_attempts=1,
        metadata={"trace": "abc"},
    )
    assert outbox.mark_failed(entry_id, "permanent error") is True

    dead = outbox.list_dead_letter()[0]
    assert dead.metadata == {"trace": "abc"}

    assert outbox.requeue_from_dead_letter(dead.entry_id) is True
    pending = outbox.get_pending()[0]
    assert pending.metadata == {"trace": "abc"}


def test_outbox_manual_dead_letter_preserves_metadata(tmp_path):
    """O caminho manual de dead-letter também deve preservar metadados."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    entry_id = outbox.enqueue(
        Event(kind="test.event", source="test", payload={"x": 1}),
        metadata={"trace": "manual"},
    )
    assert outbox.move_to_dead_letter(entry_id, "manual move") is True

    dead = outbox.list_dead_letter()[0]
    assert dead.metadata == {"trace": "manual"}


def test_outbox_migrates_legacy_schema_for_metadata(tmp_path):
    """Bancos criados sem a coluna metadata devem ser migrados na abertura."""
    db_path = tmp_path / "legacy.db"
    now = datetime.now(timezone.utc)
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
                delivered_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE dead_letter (
                entry_id TEXT PRIMARY KEY,
                original_entry_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                payload TEXT NOT NULL,
                reason TEXT NOT NULL,
                failed_attempts INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO outbox
            (entry_id, event_id, kind, source, severity, payload, dedup_key,
             correlation_id, created_at, status, attempts, max_attempts,
             next_retry_at, last_error, recorded_at, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-entry",
                "legacy-event",
                "test.event",
                "test",
                "info",
                json.dumps({"x": 1}, separators=(",", ":")),
                None,
                None,
                now.isoformat(),
                OutboxStatus.PENDING.name,
                0,
                5,
                now.timestamp(),
                None,
                now.timestamp(),
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO dead_letter
            (entry_id, original_entry_id, event_id, kind, source, payload,
             reason, failed_attempts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-dead",
                "legacy-entry",
                "legacy-dead-event",
                "test.event",
                "test",
                json.dumps({"x": 1}, separators=(",", ":")),
                "legacy failure",
                1,
                now.timestamp(),
            ),
        )
        conn.commit()

    outbox = TransactionalOutbox(db_path)
    assert outbox.get_pending()[0].metadata == {}
    assert outbox.list_dead_letter()[0].metadata == {}

    entry_id = outbox.enqueue(
        Event(kind="test.event", source="test", payload={"x": 2}),
        metadata={"trace": "new"},
    )
    entry = next(e for e in outbox.get_pending() if e.entry_id == entry_id)
    assert entry.metadata == {"trace": "new"}

    with sqlite3.connect(db_path) as conn:
        outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        dead_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(dead_letter)")
        }
    assert "metadata" in outbox_columns
    assert "metadata" in dead_columns


def test_outbox_reads_legacy_envelope_payload(tmp_path):
    """Bancos antigos guardavam Event.to_dict(); o payload deve ser extraído."""
    db_path = tmp_path / "outbox.db"
    outbox = TransactionalOutbox(db_path)
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE outbox SET payload = ? WHERE entry_id = ?",
            (json.dumps(ev.to_dict(), sort_keys=True, separators=(",", ":")), entry_id),
        )
        conn.commit()

    entry = outbox.get_pending()[0]
    assert entry.event.payload == {"x": 1}


def test_outbox_stats(tmp_path):
    """Estatísticas do outbox devem refletir o estado."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    outbox.enqueue(Event(kind="e1", source="s", payload={}))
    outbox.enqueue(Event(kind="e2", source="s", payload={}))
    stats = outbox.stats()
    assert stats["total"] == 2
    assert stats["by_status"][OutboxStatus.PENDING.name] == 2


# --------------------------------------------------------------------------- #
# Dead-Letter Queue
# --------------------------------------------------------------------------- #

def test_dead_letter_list_and_requeue(tmp_path):
    """Eventos em dead-letter devem poder ser reenfileirados."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    ev = Event(kind="test.event", source="test", payload={"x": 1})
    entry_id = outbox.enqueue(ev, max_attempts=1)

    # Falha única com max_attempts=1 vai direto para dead-letter.
    outbox.mark_failed(entry_id, "permanent error")

    dead = outbox.list_dead_letter()
    assert len(dead) == 1
    assert dead[0].reason == "permanent error"

    # Reenfileira.
    assert outbox.requeue_from_dead_letter(dead[0].entry_id) is True

    # Não deve mais estar no dead-letter.
    dead_after = outbox.list_dead_letter()
    assert len(dead_after) == 0

    # Deve aparecer como pendente no outbox.
    pending = outbox.get_pending()
    assert len(pending) == 1


def test_dead_letter_preserves_metadata_across_requeue(tmp_path):
    """Dead-letter e reenfileiramento não devem perder severidade/correlação."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    created_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    ev = Event(
        kind="test.event",
        source="test",
        payload={"x": 1},
        severity=Severity.ERROR,
        correlation_id="corr-2",
        created_at=created_at,
    )
    entry_id = outbox.enqueue(ev, max_attempts=1)
    assert outbox.mark_failed(entry_id, "permanent error") is True

    dead = outbox.list_dead_letter()[0]
    assert dead.event.severity == Severity.ERROR
    assert dead.event.correlation_id == "corr-2"
    assert dead.event.created_at == created_at

    assert outbox.requeue_from_dead_letter(dead.entry_id) is True
    pending = outbox.get_pending()[0]
    assert pending.event.severity == Severity.ERROR
    assert pending.event.correlation_id == "corr-2"
    assert pending.event.created_at == created_at


def test_dead_letter_processor_stats(tmp_path):
    """DeadLetterProcessor deve retornar estatísticas de motivos."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    processor = DeadLetterProcessor(outbox)

    # Cria alguns eventos no dead-letter.
    ev1 = Event(kind="e1", source="s", payload={})
    eid1 = outbox.enqueue(ev1, max_attempts=1)
    outbox.mark_failed(eid1, "timeout")

    ev2 = Event(kind="e2", source="s", payload={})
    eid2 = outbox.enqueue(ev2, max_attempts=1)
    outbox.mark_failed(eid2, "timeout")

    ev3 = Event(kind="e3", source="s", payload={})
    eid3 = outbox.enqueue(ev3, max_attempts=1)
    outbox.mark_failed(eid3, "connection refused")

    stats = processor.stats()
    assert stats["dead_letter_count"] == 3
    assert stats["reasons"]["timeout"] == 2
    assert stats["reasons"]["connection refused"] == 1


def test_dead_letter_processor_retry_all(tmp_path):
    """retry_all_dead_letter deve reenfileirar todos os eventos mortos."""
    outbox = TransactionalOutbox(tmp_path / "outbox.db")
    processor = DeadLetterProcessor(outbox)

    for i in range(3):
        ev = Event(kind=f"e{i}", source="s", payload={})
        eid = outbox.enqueue(ev, max_attempts=1)
        outbox.mark_failed(eid, "error")

    assert len(outbox.list_dead_letter()) == 3
    requeued = processor.retry_all_dead_letter()
    assert requeued == 3
    assert len(outbox.list_dead_letter()) == 0


__all__ = []
