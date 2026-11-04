"""EventStore: registro durável e consultável de tudo o que o NEXUS faz.

Responsabilidades: append idempotente (dedup por janela), consulta filtrada,
retenção, verificação de integridade e estatísticas para o HUD.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from ..events import Event, Severity, parse_timestamp
from .store import Database


class EventStoreError(Exception):
    """Falha de persistência ou leitura no event store."""


class CorruptEventError(EventStoreError):
    """Registro persistido não pôde ser reconstruído como evento."""


class EventStore:
    def __init__(
        self,
        database: Database,
        *,
        dedup_window_hours: int = 24,
        max_payload_bytes: int = 65_536,
        retention_days: int = 0,
        query_limit: int = 500,
    ) -> None:
        if dedup_window_hours < 0:
            raise ValueError("dedup_window_hours não pode ser negativo")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes deve ser positivo")
        if query_limit <= 0:
            raise ValueError("query_limit deve ser positivo")
        self._db = database
        self._dedup_window = timedelta(hours=dedup_window_hours)
        self._max_payload_bytes = max_payload_bytes
        self._retention = timedelta(days=retention_days) if retention_days > 0 else None
        self._query_limit = query_limit

    @property
    def database(self) -> Database:
        return self._db

    # -- escrita -------------------------------------------------------------
    def append(self, event: Event) -> tuple[Event, bool]:
        """Persiste ``event``; retorna ``(evento_armazenado, inserido)``.

        Eventos equivalentes dentro da janela de deduplicação retornam o
        registro já existente com ``inserido=False``.
        """
        payload_json = event.canonical_payload()
        size = len(payload_json.encode("utf-8"))
        if size > self._max_payload_bytes:
            raise ValueError(
                f"payload de {size} bytes excede o limite de {self._max_payload_bytes} bytes"
            )

        dedup_key = event.dedup_key() if self._dedup_window else None
        conn = self._db.connection
        with self._db.lock:
            if dedup_key is not None:
                cutoff = (event.created_at - self._dedup_window).isoformat()
                row = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE dedup_key = ? AND created_at >= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (dedup_key, cutoff),
                ).fetchone()
                if row is not None:
                    return self._row_to_event(row), False
            try:
                conn.execute(
                    """
                    INSERT INTO events (event_id, kind, source, severity, payload,
                                        dedup_key, correlation_id, created_at, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.kind,
                        event.source,
                        event.severity.value,
                        payload_json,
                        dedup_key,
                        event.correlation_id,
                        event.created_at.isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventStoreError(f"evento rejeitado pelo banco: {exc}") from exc
        return event, True

    def append_many(self, events: Sequence[Event]) -> list[tuple[Event, bool]]:
        return [self.append(event) for event in events]

    # -- leitura -------------------------------------------------------------
    def get(self, event_id: str) -> Event | None:
        rows = self._db.query("SELECT * FROM events WHERE event_id = ? LIMIT 1", (event_id,))
        return self._row_to_event(rows[0]) if rows else None

    def query(
        self,
        *,
        kind: str | None = None,
        source: str | None = None,
        severity: Severity | str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> list[Event]:
        if limit <= 0:
            raise ValueError("limit deve ser positivo")
        if offset < 0:
            raise ValueError("offset não pode ser negativo")
        where, params = self._filters(
            kind=kind, source=source, severity=severity, since=since, until=until,
            correlation_id=correlation_id,
        )
        direction = "ASC" if oldest_first else "DESC"
        sql = (
            f"SELECT * FROM events{where} "
            f"ORDER BY created_at {direction}, id {direction} LIMIT ? OFFSET ?"
        )
        rows = self._db.query(sql, (*params, min(limit, self._query_limit), offset))
        return [self._row_to_event(row) for row in rows]

    def iter_all(self, *, batch_size: int = 500) -> Iterator[Event]:
        last_id = 0
        while True:
            rows = self._db.query(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (last_id, batch_size)
            )
            if not rows:
                return
            for row in rows:
                last_id = int(row["id"])
                yield self._row_to_event(row)

    def count(
        self,
        *,
        kind: str | None = None,
        source: str | None = None,
        severity: Severity | str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        where, params = self._filters(
            kind=kind, source=source, severity=severity, since=since, until=until,
            correlation_id=correlation_id,
        )
        rows = self._db.query(f"SELECT COUNT(*) AS total FROM events{where}", params)
        return int(rows[0]["total"])

    def list_kinds(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT kind, COUNT(*) AS total FROM events GROUP BY kind ORDER BY total DESC LIMIT ?",
            (max(1, limit),),
        )
        return [{"kind": row["kind"], "count": int(row["total"])} for row in rows]

    # -- manutenção ----------------------------------------------------------
    def purge(self, *, older_than: datetime | str) -> int:
        cutoff = parse_timestamp(older_than, "older_than").isoformat()
        return self._db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))

    def apply_retention(self, *, now: datetime | None = None) -> int:
        """Remove eventos além da retenção configurada. Retorna quantos saíram."""
        if self._retention is None:
            return 0
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return self.purge(older_than=reference - self._retention)

    def verify(self) -> dict[str, Any]:
        integrity = self._db.integrity_check()
        total = 0
        invalid = 0
        for row in self._db.query("SELECT payload FROM events"):
            total += 1
            try:
                json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                invalid += 1
        return {
            "ok": integrity == "ok" and invalid == 0,
            "integrity": integrity,
            "schema_version": self._db.schema_version(),
            "events": total,
            "invalid_payloads": invalid,
            "size_bytes": self._db.size_bytes(),
        }

    def stats(self) -> dict[str, Any]:
        conn = self._db.connection
        with self._db.lock:
            total = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            oldest = conn.execute("SELECT MIN(created_at) FROM events").fetchone()[0]
            newest = conn.execute("SELECT MAX(created_at) FROM events").fetchone()[0]
            by_severity = {
                str(row["severity"]): int(row["total"])
                for row in conn.execute(
                    "SELECT severity, COUNT(*) AS total FROM events GROUP BY severity"
                )
            }
        return {
            "total": total,
            "oldest": oldest,
            "newest": newest,
            "by_severity": by_severity,
            "size_bytes": self._db.size_bytes(),
            "path": str(self._db.path),
            "schema_version": self._db.schema_version(),
        }

    # -- internos ------------------------------------------------------------
    def _filters(
        self,
        *,
        kind: str | None,
        source: str | None,
        severity: Severity | str | None,
        since: datetime | str | None,
        until: datetime | str | None,
        correlation_id: str | None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if severity is not None:
            value = severity.value if isinstance(severity, Severity) else str(severity).lower()
            if value not in {item.value for item in Severity}:
                raise ValueError(f"severity inválida: {severity!r}")
            clauses.append("severity = ?")
            params.append(value)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(parse_timestamp(since, "since").isoformat())
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(parse_timestamp(until, "until").isoformat())
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, tuple(params)

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        try:
            return Event(
                kind=row["kind"],
                source=row["source"],
                payload=json.loads(row["payload"]),
                severity=Severity(row["severity"]),
                event_id=row["event_id"],
                correlation_id=row["correlation_id"],
                created_at=parse_timestamp(row["created_at"], "created_at"),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CorruptEventError(
                f"evento corrompido (id={row['event_id'] if 'event_id' in row.keys() else '?'}): {exc}"
            ) from exc
