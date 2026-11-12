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
