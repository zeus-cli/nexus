"""Event Fabric — outbox transacional, retry, dead-letter e consumidor idempotente.

Responsabilidade:
* Transactional Outbox: eventos são persistidos na mesma transação do
  EventStore, antes de qualquer publicação.
* Entrega única por evento ao sink configurado (normalmente o ``EventBus``),
  com retry exponencial e dead-letter após esgotar as tentativas.
* Consumidor idempotente: entregas reexecutadas (crash entre ``publish`` e
  ``mark_delivered``) não repetem efeitos colaterais.

Princípios:
* Nenhum evento inserido no EventStore fica fora do outbox por falha de
  entrega; o pump reprocessa pendências no próximo ciclo ou reinício.
* A entrega é *at-least-once*. Consumidores que produzem efeitos externos
  devem usar :class:`IdempotentConsumer`.
* Dead-letter é inspecionável e pode ser reenfileirada manualmente.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

from .event_fabric_schema import EVENT_FABRIC_STATEMENTS
from .events import Event, Severity, parse_timestamp
from .memory.eventstore import EventStore
from .memory.store import Database
from .sqlite_utils import connect_db

_ENVELOPE_KEYS = {
    "event_id",
    "kind",
    "source",
    "severity",
    "payload",
    "correlation_id",
    "created_at",
    "dedup_key",
}

EventSink = Callable[[Event], Awaitable[int] | int]
ConsumerHandler = Callable[[Event], Awaitable[None] | None]


def _decode_payload(raw: str) -> dict[str, Any]:
    """Retorna somente o payload do evento.

    Versões antigas gravavam ``Event.to_dict()`` inteiro na coluna ``payload``.
    Para esses registros, extrai o campo interno ``payload`` sem perder os
    dados; para os novos, o conteúdo já é o payload canônico.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("payload persistido não é um objeto JSON")
    if set(data) == _ENVELOPE_KEYS and isinstance(data.get("payload"), dict):
        return dict(data["payload"])
    return dict(data)


def _encode_metadata(metadata: Mapping[str, Any] | None) -> str:
    """Serializa metadados do outbox em JSON canônico."""
    return json.dumps(
        dict(metadata or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _decode_metadata(raw: str | None) -> dict[str, Any]:
    """Reconstrói metadados persistidos; ``NULL`` (bancos antigos) vira ``{}``."""
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("metadata persistida não é um objeto JSON")
    return dict(data)


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


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Resultado de um ciclo de entrega do outbox."""

    delivered: int = 0
    failed: int = 0
    dead_letter: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "failed": self.failed,
            "dead_letter": self.dead_letter,
            "skipped": self.skipped,
        }


class OutboxError(Exception):
    """Erro no transactional outbox."""


class TransactionalOutbox:
    """Outbox transacional: eventos persistidos junto com transações de negócio.

    Pode operar em duas modalidades:

    * ``TransactionalOutbox(Database)`` — compartilha a conexão do EventStore e
      permite ``append + enqueue`` na mesma transação SQLite;
    * ``TransactionalOutbox(caminho)`` — banco independente, útil para testes e
      ferramentas de inspeção. Nesse modo não há atomicidade entre store e
      outbox; o Event Fabric usa a modalidade compartilhada.
    """

    def __init__(self, target: str | Path | Database) -> None:
        if isinstance(target, Database):
            self._database: Database | None = target
            self._db_path = Path(target.path)
        else:
            self._database = None
            self._db_path = Path(target)
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_db()

    @property
    def database(self) -> Database | None:
        return self._database

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._database is not None:
            with self._database.lock:
                conn = self._database.connection
                conn.row_factory = sqlite3.Row
                yield conn
        else:
            with connect_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                yield conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            if self._database is None:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            for statement in EVENT_FABRIC_STATEMENTS:
                conn.execute(statement)
            self._migrate_columns(
                conn,
                "outbox",
                {"metadata": "TEXT", "dedup": "INTEGER"},
            )
            self._migrate_columns(
                conn,
                "dead_letter",
                {
                    "severity": "TEXT NOT NULL DEFAULT 'info'",
                    "dedup_key": "TEXT",
                    "correlation_id": "TEXT",
                    "event_created_at": "TEXT",
                    "metadata": "TEXT",
                    "dedup": "INTEGER",
                },
            )

    @staticmethod
    def _migrate_columns(
        conn: sqlite3.Connection,
        table: str,
        columns: Mapping[str, str],
    ) -> None:
        """Adiciona colunas ausentes a bancos criados por versões antigas."""
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    def enqueue(
        self,
        event: Event,
        *,
        max_attempts: int = 5,
        metadata: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """Enfileira um evento para entrega.

        Quando ``connection`` é informado, a inserção participa da transação do
        chamador (usado pelo EventFabric para atomicidade com o EventStore).
        """
        if max_attempts <= 0:
            raise ValueError("max_attempts deve ser positivo")
        entry_id = uuid.uuid4().hex
        now = time.time()
        values = (
            entry_id,
            event.event_id,
            event.kind,
            event.source,
            event.severity.value,
            event.canonical_payload(),
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
            _encode_metadata(metadata),
            1 if event.dedup else 0,
        )
        if connection is not None:
            self._insert(connection, values)
        else:
            with self._connection() as conn:
                self._insert(conn, values)
        return entry_id

    @staticmethod
    def _insert(conn: sqlite3.Connection, values: tuple[Any, ...]) -> None:
        conn.execute(
            """
            INSERT INTO outbox
            (entry_id, event_id, kind, source, severity, payload,
             dedup_key, correlation_id, created_at, status, attempts,
             max_attempts, next_retry_at, last_error, recorded_at, delivered_at,
             metadata, dedup)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    def get_pending(self, limit: int = 100) -> list[OutboxEntry]:
        """Retorna entradas pendentes ou para retry (next_retry_at <= agora)."""
        if limit <= 0:
            raise ValueError("limit deve ser positivo")
        now = time.time()
        with self._connection() as conn:
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

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            return self._row_to_entry(row) if row is not None else None

    def mark_delivered(self, entry_id: str) -> bool:
        """Marca uma entrega como bem-sucedida."""
        now = time.time()
        with self._connection() as conn:
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
            return cur.rowcount > 0

    def mark_failed(
        self,
        entry_id: str,
        error: str,
        *,
        retry_delay_s: float | None = None,
    ) -> bool:
        """Marca uma entrega como falhada, agendando retry ou dead-letter."""
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status, attempts, max_attempts FROM outbox WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None or row["status"] != OutboxStatus.PENDING.name:
                return False

            attempts = row["attempts"] + 1
            max_attempts = row["max_attempts"]

            if attempts >= max_attempts:
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
                event_row = conn.execute(
                    "SELECT event_id, kind, source, severity, payload, dedup_key, "
                    "correlation_id, created_at, metadata, dedup "
                    "FROM outbox WHERE entry_id = ?",
                    (entry_id,),
                ).fetchone()
                if event_row:
                    conn.execute(
                        """
                        INSERT INTO dead_letter
                        (entry_id, original_entry_id, event_id, kind, source, severity,
                         payload, reason, failed_attempts, created_at, dedup_key,
                         correlation_id, event_created_at, metadata, dedup)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            entry_id,
                            event_row["event_id"],
                            event_row["kind"],
                            event_row["source"],
                            event_row["severity"],
                            event_row["payload"],
                            error,
                            attempts,
                            now,
                            event_row["dedup_key"],
                            event_row["correlation_id"],
                            event_row["created_at"],
                            event_row["metadata"],
                            event_row["dedup"],
                        ),
                    )
            else:
                delay = retry_delay_s if retry_delay_s is not None else (2.0 ** attempts)
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
                        now + delay,
                        entry_id,
                    ),
                )
            return True

    def move_to_dead_letter(self, entry_id: str, reason: str) -> bool:
        """Move uma entrada manualmente para dead-letter."""
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None or row["status"] == OutboxStatus.DEAD_LETTER.name:
                return False
            conn.execute(
                """
                INSERT INTO dead_letter
                (entry_id, original_entry_id, event_id, kind, source, severity,
                 payload, reason, failed_attempts, created_at, dedup_key,
                 correlation_id, event_created_at, metadata, dedup)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    entry_id,
                    row["event_id"],
                    row["kind"],
                    row["source"],
                    row["severity"],
                    row["payload"],
                    reason,
                    row["attempts"],
                    now,
                    row["dedup_key"],
                    row["correlation_id"],
                    row["created_at"],
                    row["metadata"],
                    row["dedup"],
                ),
            )
            conn.execute(
                "UPDATE outbox SET status = ?, last_error = ? WHERE entry_id = ?",
                (OutboxStatus.DEAD_LETTER.name, reason, entry_id),
            )
            return True

    def list_dead_letter(self, limit: int = 100) -> list[DeadLetterEntry]:
        """Lista eventos na fila de mortos."""
        if limit <= 0:
            raise ValueError("limit deve ser positivo")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM dead_letter ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dead_letter(row) for row in rows]

    def requeue_from_dead_letter(self, entry_id: str) -> bool:
        """Reenfileira um evento morto preservando ``entry_id`` e ``event_id``.

        O evento não ganha nova identidade: consumidores idempotentes continuam
        reconhecendo uma reentrega do mesmo fato. Se a linha original do outbox
        não existir mais, ela é recriada com a mesma identidade.
        """
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM dead_letter WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return False

            original_id = row["original_entry_id"]
            original = conn.execute(
                "SELECT entry_id, max_attempts FROM outbox WHERE entry_id = ?",
                (original_id,),
            ).fetchone()
            if original is not None:
                conn.execute(
                    """
                    UPDATE outbox
                    SET status = ?, attempts = 0, next_retry_at = ?,
                        last_error = NULL, delivered_at = NULL
                    WHERE entry_id = ?
                    """,
                    (OutboxStatus.PENDING.name, now, original_id),
                )
            else:
                event_created_at = row["event_created_at"]
                if not event_created_at:
                    event_created_at = datetime.fromtimestamp(
                        row["created_at"], tz=UTC
                    ).isoformat()
                payload_json = json.dumps(
                    _decode_payload(row["payload"]),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                outbox_entry_id = original_id
                conn.execute(
                    """
                    INSERT INTO outbox
                    (entry_id, event_id, kind, source, severity, payload,
                     dedup_key, correlation_id, created_at, status, attempts,
                     max_attempts, next_retry_at, last_error, recorded_at,
                     delivered_at, metadata, dedup)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outbox_entry_id,
                        row["event_id"],
                        row["kind"],
                        row["source"],
                        row["severity"] or Severity.INFO.value,
                        payload_json,
                        row["dedup_key"],
                        row["correlation_id"],
                        event_created_at,
                        OutboxStatus.PENDING.name,
                        0,
                        5,
                        now,
                        None,
                        now,
                        None,
                        row["metadata"],
                        row["dedup"],
                    ),
                )
            conn.execute(
                "DELETE FROM dead_letter WHERE entry_id = ?", (entry_id,)
            )
            return True

    def stats(self) -> dict[str, Any]:
        with self._connection() as conn:
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

    @staticmethod
    def _row_dedup(row: sqlite3.Row) -> bool:
        """Reconstrói ``dedup``, tratando linhas migradas de versões antigas.

        Bancos antigos não gravavam a coluna; a migração a adiciona como NULL.
        Nesse caso, a presença de ``dedup_key`` é a única evidência disponível.
        """
        try:
            value = row["dedup"]
        except (IndexError, KeyError):
            return row["dedup_key"] is not None
        if value is None:
            return row["dedup_key"] is not None
        return bool(value)

    def _row_to_entry(self, row: sqlite3.Row) -> OutboxEntry:
        event = Event(
            kind=row["kind"],
            source=row["source"],
            payload=_decode_payload(row["payload"]),
            severity=Severity(row["severity"]),
            event_id=row["event_id"],
            correlation_id=row["correlation_id"],
            created_at=parse_timestamp(row["created_at"], "outbox.created_at"),
            dedup=self._row_dedup(row),
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
            metadata=_decode_metadata(row["metadata"]),
        )

    def _row_to_dead_letter(self, row: sqlite3.Row) -> DeadLetterEntry:
        event_created_at = row["event_created_at"]
        if event_created_at:
            created_at = parse_timestamp(event_created_at, "dead_letter.event_created_at")
        else:
            created_at = datetime.fromtimestamp(row["created_at"], tz=UTC)
        event = Event(
            kind=row["kind"],
            source=row["source"],
            payload=_decode_payload(row["payload"]),
            severity=Severity(row["severity"] or Severity.INFO.value),
            event_id=row["event_id"],
            correlation_id=row["correlation_id"],
            created_at=created_at,
            dedup=self._row_dedup(row),
        )
        return DeadLetterEntry(
            entry_id=row["entry_id"],
            original_entry_id=row["original_entry_id"],
            event=event,
            reason=row["reason"],
            failed_attempts=row["failed_attempts"],
            created_at=row["created_at"],
            metadata=_decode_metadata(row["metadata"]),
        )


class DeadLetterProcessor:
    """Processador de dead-letter: retry automático e inspeção."""

    def __init__(self, outbox: TransactionalOutbox) -> None:
        self._outbox = outbox

    def retry_all_dead_letter(self, max_retries: int = 10) -> int:
        """Tenta reenfileirar todos os eventos do dead-letter."""
        if max_retries <= 0:
            raise ValueError("max_retries deve ser positivo")
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


def _noop_sink(event: Event) -> int:
    del event
    return 0


class EventFabric:
    """Fachada única de emissão: EventStore + outbox + entrega + retry.

    O fluxo de :meth:`emit` é:

    1. ``EventStore.append`` e ``TransactionalOutbox.enqueue`` na mesma
       transação (quando ambos compartilham o ``Database``);
    2. entrega imediata ao sink;
    3. ``mark_delivered`` em caso de sucesso ou ``mark_failed`` com backoff.

    Falha de entrega não levanta exceção: o evento já está durável e o pump
    (ou o próximo processo) tentará novamente. Após ``max_attempts``, a entrada
    vai para dead-letter e pode ser inspecionada/reenfileirada.
    """

    def __init__(
        self,
        store: EventStore,
        outbox: TransactionalOutbox,
        sink: EventSink | None = None,
        *,
        logger: logging.Logger | None = None,
        batch_size: int = 100,
        poll_interval_s: float = 1.0,
        backoff_base_s: float | None = None,
        max_backoff_s: float = 300.0,
        max_attempts: int = 5,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size deve ser positivo")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s deve ser positivo")
        if backoff_base_s is not None and backoff_base_s < 0:
            raise ValueError("backoff_base_s não pode ser negativo")
        if max_backoff_s < 0:
            raise ValueError("max_backoff_s não pode ser negativo")
        if max_attempts <= 0:
            raise ValueError("max_attempts deve ser positivo")
        self._store = store
        self._outbox = outbox
        self._sink: EventSink = sink or _noop_sink
        self._logger = logger or logging.getLogger("nexus.event_fabric")
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._backoff_base_s = backoff_base_s
        self._max_backoff_s = max_backoff_s
        self._max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_report = DeliveryReport()

    @property
    def outbox(self) -> TransactionalOutbox:
        return self._outbox

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stats(self) -> dict[str, Any]:
        stats = self._outbox.stats()
        stats["running"] = self.running
        stats["last_delivery"] = self._last_report.as_dict()
        return stats

    async def emit(
        self,
        event: Event,
        *,
        max_attempts: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Event:
        """Persiste, enfileira e tenta entregar ``event`` imediatamente.

        Eventos duplicados (dedup do EventStore) retornam o registro existente
        e não são republicados.
        """
        stored, entry_id = self._persist_and_enqueue(
            event,
            max_attempts=max_attempts or self._max_attempts,
            metadata=metadata,
        )
        if entry_id is None:
            return stored
        async with self._lock:
            entry = self._outbox.get_entry(entry_id)
            if entry is not None and entry.status is OutboxStatus.PENDING:
                await self._deliver_entry(entry)
        return stored

    async def deliver_pending(self) -> DeliveryReport:
        """Entrega todas as entradas vencidas, respeitando o limite do lote."""
        delivered = failed = dead = skipped = 0
        async with self._lock:
            for entry in self._outbox.get_pending(self._batch_size):
                outcome = await self._deliver_entry(entry)
                if outcome == "delivered":
                    delivered += 1
                elif outcome == "failed":
                    failed += 1
                elif outcome == "dead_letter":
                    dead += 1
                else:
                    skipped += 1
        self._last_report = DeliveryReport(
            delivered=delivered,
            failed=failed,
            dead_letter=dead,
            skipped=skipped,
        )
        return self._last_report

    def start(self) -> asyncio.Task[None]:
        """Inicia (idempotentemente) o pump de retry em background."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="nexus-event-fabric")
        return self._task

    async def stop(self) -> None:
        """Sinaliza e aguarda o encerramento do pump."""
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception(
                "pump do Event Fabric encerrou com erro",
                extra={"component": "event_fabric"},
            )
        finally:
            self._task = None
            self._stop_event = None

    async def _run_loop(self) -> None:
        stop_event = self._stop_event
        assert stop_event is not None
        while not stop_event.is_set():
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "falha no ciclo de entrega do outbox",
                    extra={"component": "event_fabric"},
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._poll_interval_s
                )
            except TimeoutError:
                pass

    def _persist_and_enqueue(
        self,
        event: Event,
        *,
        max_attempts: int,
        metadata: Mapping[str, Any] | None,
    ) -> tuple[Event, str | None]:
        if (
            self._outbox.database is not None
            and self._outbox.database is self._store.database
        ):
            database = self._store.database
            with database.lock:
                conn = database.connection
                conn.execute("BEGIN IMMEDIATE")
                try:
                    stored, inserted = self._store.append(event)
                    entry_id = (
                        self._outbox.enqueue(
                            stored,
                            max_attempts=max_attempts,
                            metadata=metadata,
                            connection=conn,
                        )
                        if inserted
                        else None
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                return stored, entry_id

        # Modo legado/independente: sem transação única entre store e outbox.
        stored, inserted = self._store.append(event)
        if not inserted:
            return stored, None
        return stored, self._outbox.enqueue(
            stored, max_attempts=max_attempts, metadata=metadata
        )

    def _retry_delay(self, attempts: int) -> float | None:
        if self._backoff_base_s is None:
            return None
        delay = self._backoff_base_s * (2 ** max(attempts - 1, 0))
        return min(delay, self._max_backoff_s)

    async def _deliver_entry(self, entry: OutboxEntry) -> str:
        try:
            result = self._sink(entry.event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 — qualquer falha do sink vira retry
            error = f"{type(exc).__name__}: {exc}"
            self._outbox.mark_failed(
                entry.entry_id,
                error,
                retry_delay_s=self._retry_delay(entry.attempts + 1),
            )
            exhausted = entry.attempts + 1 >= entry.max_attempts
            self._logger.warning(
                "entrega de evento falhou; %s",
                "movido para dead-letter" if exhausted else "retry agendado",
                extra={
                    "component": "event_fabric",
                    "entry_id": entry.entry_id,
                    "event_id": entry.event.event_id,
                    "kind": entry.event.kind,
                    "attempts": entry.attempts + 1,
                    "max_attempts": entry.max_attempts,
                    "error": error,
                },
            )
            return "dead_letter" if exhausted else "failed"
        else:
            self._outbox.mark_delivered(entry.entry_id)
            self._logger.debug(
                "evento entregue",
                extra={
                    "component": "event_fabric",
                    "entry_id": entry.entry_id,
                    "event_id": entry.event.event_id,
                    "kind": entry.event.kind,
                },
            )
            return "delivered"


class IdempotentConsumer:
    """Consumidor com deduplicação durável por ``event_id``/``dedup_key``.

    A chave primária é ``event_id``, que é reutilizado em reentregas do outbox.
    ``dedup_key`` também é consultado dentro de uma janela para absorver o mesmo
    fato emitido por outro processo com outro ``event_id``. O marcador só é
    gravado depois que o handler termina; falha permite nova tentativa.

    O lock é de processo. A garantia é ``at-least-once`` com efeito
    ``at-most-once`` por consumidor enquanto houver um único processo emissor
    (topologia atual). Consumidores concorrentes entre processos exigem claim
    transacional, previsto para o Control Plane.
    """

    def __init__(
        self,
        outbox: TransactionalOutbox,
        consumer_id: str,
        handler: ConsumerHandler,
        *,
        dedup_window_s: float = 86_400.0,
        logger: logging.Logger | None = None,
    ) -> None:
        normalized = (consumer_id or "").strip()
        if not normalized:
            raise ValueError("consumer_id não pode ser vazio")
        if not callable(handler):
            raise TypeError("handler deve ser chamável")
        if dedup_window_s < 0:
            raise ValueError("dedup_window_s não pode ser negativo")
        self._outbox = outbox
        self._consumer_id = normalized
        self._handler = handler
        self._dedup_window_s = dedup_window_s
        self._logger = logger or logging.getLogger("nexus.idempotent_consumer")
        self._lock = asyncio.Lock()
        self._processed = 0
        self._skipped = 0

    @property
    def consumer_id(self) -> str:
        return self._consumer_id

    def stats(self) -> dict[str, int | str]:
        return {
            "consumer_id": self._consumer_id,
            "processed": self._processed,
            "skipped": self._skipped,
        }

    def is_processed(self, event: Event) -> bool:
        dedup_key = event.dedup_key()
        with self._outbox._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM consumer_processed "
                "WHERE consumer_id = ? AND event_id = ? LIMIT 1",
                (self._consumer_id, event.event_id),
            ).fetchone()
            if row is not None:
                return True
            if dedup_key and self._dedup_window_s > 0:
                cutoff = time.time() - self._dedup_window_s
                row = conn.execute(
                    "SELECT 1 FROM consumer_processed "
                    "WHERE consumer_id = ? AND dedup_key = ? AND processed_at >= ? "
                    "LIMIT 1",
                    (self._consumer_id, dedup_key, cutoff),
                ).fetchone()
                return row is not None
            return False

    def _mark_processed(self, event: Event) -> None:
        with self._outbox._connection() as conn:
            conn.execute(
                """
                INSERT INTO consumer_processed
                    (consumer_id, event_id, dedup_key, processed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(consumer_id, event_id) DO UPDATE SET
                    dedup_key = excluded.dedup_key,
                    processed_at = excluded.processed_at
                """,
                (
                    self._consumer_id,
                    event.event_id,
                    event.dedup_key(),
                    time.time(),
                ),
            )

    async def handle(self, event: Event) -> bool:
        """Executa o handler se o evento ainda não foi processado.

        Retorna ``True`` quando o handler executou e ``False`` quando a entrega
        era duplicada. Exceções do handler propagam sem marcar o evento.
        """
        async with self._lock:
            if self.is_processed(event):
                self._skipped += 1
                self._logger.debug(
                    "evento duplicado ignorado pelo consumidor",
                    extra={
                        "component": "idempotent_consumer",
                        "consumer_id": self._consumer_id,
                        "event_id": event.event_id,
                    },
                )
                return False
            result = self._handler(event)
            if inspect.isawaitable(result):
                await result
            self._mark_processed(event)
            self._processed += 1
            return True

    def subscribe(self, bus: Any, pattern: str = "*") -> Any:
        """Registra :meth:`handle` no ``EventBus`` e retorna a assinatura."""
        return bus.subscribe(pattern, self.handle)


__all__ = [
    "DeadLetterEntry",
    "DeadLetterProcessor",
    "DeliveryReport",
    "EventFabric",
    "IdempotentConsumer",
    "OutboxEntry",
    "OutboxError",
    "OutboxStatus",
    "TransactionalOutbox",
]
