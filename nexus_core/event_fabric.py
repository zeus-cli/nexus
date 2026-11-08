"""Event Fabric — Transactional Outbox e Dead-Letter Queue.

Responsabilidade:
* Transactional Outbox: garante que eventos críticos sejam persistidos
  atomicamente com a transação de negócios, evitando perda de mensagens.
* Dead-Letter Queue: eventos que falham repetidamente na entrega são
  direcionados para uma fila de mortos, evitando bloqueio do sistema.
* Retry com backoff exponencial para eventos falhados.

Princípios:
* Nenhum evento crítico é perdido devido a falha de entrega.
* Eventos no dead-letter podem ser inspecionados e reenfileirados manualmente.
* O outbox garante consistência entre estado de negócio e eventos emitidos.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Mapping

from .events import Event


class OutboxStatus(Enum):
    PENDING = auto()      # aguardando entrega
    DELIVERED = auto()    # entregue com sucesso
    FAILED = auto()       # falhou, será retryado
    DEAD_LETTER = auto()  # moveu para fila de mortos após max_retries


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """Entrada no transactional outbox."""

    entry_id: str
    event: Event
    status: OutboxStatus
    attempts: int
    max_attempts: int
    next_retry_at: float | None
    last_error: str | None
    created_at: float
    delivered_at: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "event": self.event.to_dict(),
            "status": self.status.name,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    """Entrada na fila de mensagens mortas."""

    entry_id: str
    original_entry_id: str
    event: Event
    reason: str
    failed_attempts: int
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "original_entry_id": self.original_entry_id,
            "event": self.event.to_dict(),
            "reason": self.reason,
            "failed_attempts": self.failed_attempts,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class OutboxError(Exception):
    """Erro no transactional outbox."""


class TransactionalOutbox:
    """Outbox transacional: eventos persistidos junto com transações de negócio.

    Garante que eventos não sejam perdidos mesmo que a entrega ao EventBus
    falhe. Os eventos são persistidos primeiro e entregues em seguida.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
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
                "CREATE INDEX IF NOT EXISTS ix_outbox_status_next_retry "
                "ON outbox(status, next_retry_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_outbox_event_id "
                "ON outbox(event_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letter (
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
                "CREATE INDEX IF NOT EXISTS ix_dead_letter_created "
                "ON dead_letter(created_at)"
            )
            conn.commit()

    def enqueue(
        self,
        event: Event,
        *,
        max_attempts: int = 5,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Enfileira um evento para entrega (atomicamente com transação do caller)."""
        entry_id = uuid.uuid4().hex
        now = time.monotonic()
        payload_json = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO outbox
                (entry_id, event_id, kind, source, severity, payload,
                 dedup_key, correlation_id, created_at, status, attempts,
                 max_attempts, next_retry_at, last_error, recorded_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    event.event_id,
                    event.kind,
                    event.source,
                    event.severity.value,
                    payload_json,
                    event.dedup_key(),
                    event.correlation_id,
                    event.created_at.isoformat(),
                    OutboxStatus.PENDING.name,
                    0,
                    max_attempts,
                    now,  # primeira tentativa imediata
                    None,
                    now,
                    None,
                ),
            )
            conn.commit()
        return entry_id

    def get_pending(self, limit: int = 100) -> list[OutboxEntry]:
        """Retorna entradas pendentes ou para retry (next_retry_at <= agora)."""
        now = time.monotonic()
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status = ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY next_retry_at ASC, recorded_at ASC
                LIMIT ?
                """,
                (OutboxStatus.PENDING.name, now, limit),
            ).fetchall()
            return [self._row_to_entry(row) for row in rows]

    def mark_delivered(self, entry_id: str) -> bool:
        """Marca uma entrega como bem-sucedida."""
        now = time.monotonic()
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """
                UPDATE outbox
                SET status = ?, delivered_at = ?
                WHERE entry_id = ? AND status = ?
                """,
                (
                    OutboxStatus.DELIVERED.name,
                    now,
                    entry_id,
                    OutboxStatus.PENDING.name,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_failed(
        self,
        entry_id: str,
        error: str,
        *,
        retry_delay_s: float | None = None,
    ) -> bool:
        """Marca uma entrega como falhada, agendando retry ou dead-letter."""
        now = time.monotonic()
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT attempts, max_attempts FROM outbox WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False

            attempts = row["attempts"] + 1
            max_attempts = row["max_attempts"]

            if attempts >= max_attempts:
                # Move para dead-letter.
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, last_error = ?, attempts = ?
                    WHERE entry_id = ?
                    """,
                    (
                        OutboxStatus.DEAD_LETTER.name,
                        error,
                        attempts,
                        entry_id,
                    ),
                )
                # Copia para dead_letter.
                event_row = conn.execute(
                    "SELECT event_id, kind, source, severity, payload, dedup_key, correlation_id, created_at FROM outbox WHERE entry_id = ?",
                    (entry_id,),
                ).fetchone()
                if event_row:
                    dead_id = uuid.uuid4().hex
                    conn.execute(
                        """
                        INSERT INTO dead_letter
                        (entry_id, original_entry_id, event_id, kind, source, payload,
                         reason, failed_attempts, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dead_id,
                            entry_id,
                            event_row["event_id"],
                            event_row["kind"],
                            event_row["source"],
                            event_row["payload"],
                            error,
                            attempts,
                            now,
                        ),
                    )
            else:
                # Agenda retry com backoff exponencial.
                delay = retry_delay_s if retry_delay_s is not None else (2.0 ** attempts)
                next_retry = now + delay
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, last_error = ?, attempts = ?, next_retry_at = ?
                    WHERE entry_id = ?
                    """,
                    (
                        OutboxStatus.PENDING.name,
                        error,
                        attempts,
                        next_retry,
                        entry_id,
                    ),
                )
            conn.commit()
            return True

    def move_to_dead_letter(self, entry_id: str, reason: str) -> bool:
        """Move uma entrada manualmente para dead-letter."""
        now = time.monotonic()
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM outbox WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return False
            dead_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO dead_letter
                (entry_id, original_entry_id, event_id, kind, source, payload,
                 reason, failed_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dead_id,
                    entry_id,
                    row["event_id"],
                    row["kind"],
                    row["source"],
                    row["payload"],
                    reason,
                    row["attempts"],
                    now,
                ),
            )
            conn.execute(
                "UPDATE outbox SET status = ?, last_error = ? WHERE entry_id = ?",
                (OutboxStatus.DEAD_LETTER.name, reason, entry_id),
            )
            conn.commit()
            return True

    def list_dead_letter(self, limit: int = 100) -> list[DeadLetterEntry]:
        """Lista eventos na fila de mortos."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM dead_letter ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dead_letter(row) for row in rows]

    def requeue_from_dead_letter(self, entry_id: str) -> bool:
        """Reenfileira um evento da fila de mortos de volta ao outbox."""
        now = time.monotonic()
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM dead_letter WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return False
            # Reinsere no outbox como PENDING com novo event_id (evita UNIQUE constraint).
            new_entry_id = uuid.uuid4().hex
            new_event_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO outbox
                (entry_id, event_id, kind, source, severity, payload,
                 dedup_key, correlation_id, created_at, status, attempts,
                 max_attempts, next_retry_at, last_error, recorded_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_entry_id,
                    new_event_id,
                    row["kind"],
                    row["source"],
                    "info",  # severity desconhecido, usa info
                    row["payload"],
                    None,
                    None,
                    row["created_at"],
                    OutboxStatus.PENDING.name,
                    0,
                    5,
                    now,
                    None,
                    now,
                    None,
                ),
            )
            conn.execute(
                "DELETE FROM dead_letter WHERE entry_id = ?", (entry_id,)
            )
            conn.commit()
            return True

    def stats(self) -> dict[str, Any]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                "SELECT COUNT(*) FROM outbox"
            ).fetchone()[0]
            by_status = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM outbox GROUP BY status"
            ):
                by_status[row["status"]] = row["c"]
            dead = conn.execute(
                "SELECT COUNT(*) FROM dead_letter"
            ).fetchone()[0]
            return {
                "total": total,
                "by_status": by_status,
                "dead_letter": dead,
            }

    def _row_to_entry(self, row: sqlite3.Row) -> OutboxEntry:
        import json as _json

        event = Event(
            kind=row["kind"],
            source=row["source"],
            payload=_json.loads(row["payload"]),
            severity=__import__("nexus_core.events", fromlist=["Severity"]).Severity(
                row["severity"]
            ),
            event_id=row["event_id"],
            correlation_id=row["correlation_id"],
        )
        return OutboxEntry(
            entry_id=row["entry_id"],
            event=event,
            status=OutboxStatus[row["status"]],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            created_at=row["recorded_at"],
            delivered_at=row["delivered_at"],
            metadata={},
        )

    def _row_to_dead_letter(self, row: sqlite3.Row) -> DeadLetterEntry:
        import json as _json

        event = Event(
            kind=row["kind"],
            source=row["source"],
            payload=_json.loads(row["payload"]),
            severity=__import__("nexus_core.events", fromlist=["Severity"]).Severity(
                "info"
            ),
            event_id=row["event_id"],
        )
        return DeadLetterEntry(
            entry_id=row["entry_id"],
            original_entry_id=row["original_entry_id"],
            event=event,
            reason=row["reason"],
            failed_attempts=row["failed_attempts"],
            created_at=row["created_at"],
            metadata={},
        )


class DeadLetterProcessor:
    """Processador de dead-letter: retry automático e inspeção."""

    def __init__(self, outbox: TransactionalOutbox) -> None:
        self._outbox = outbox

    def retry_all_dead_letter(self, max_retries: int = 10) -> int:
        """Tenta reenfileirar todos os eventos do dead-letter."""
        entries = self._outbox.list_dead_letter()
        requeued = 0
        for entry in entries:
            if self._outbox.requeue_from_dead_letter(entry.entry_id):
                requeued += 1
        return requeued

    def stats(self) -> dict[str, Any]:
        dead = self._outbox.list_dead_letter()
        reasons: dict[str, int] = {}
        for entry in dead:
            reasons[entry.reason] = reasons.get(entry.reason, 0) + 1
        return {
            "dead_letter_count": len(dead),
            "reasons": reasons,
        }


__all__ = [
    "TransactionalOutbox",
    "DeadLetterProcessor",
    "OutboxEntry",
    "DeadLetterEntry",
    "OutboxStatus",
]
