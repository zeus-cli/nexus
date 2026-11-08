"""Execution Journal — registro dedicado de execuções governadas.

Responsabilidade: manter um journal imutável e auditável de todas as execuções
realizadas através do Execution Gate, separado do EventStore de eventos gerais.

Cada registro inclui: request_id, component_id, tool_id, parâmetros (hash),
resultado, status, timestamps, assinatura de integridade e correlação com
aprovação HITL quando aplicável.

Regra de projeto: o journal é append-only e imutável; registros não são
editados após gravação, apenas marcados com status final.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Mapping


class ExecutionStatusJournal(Enum):
    AUTHORIZED = auto()
    EXECUTING = auto()
    COMPLETED = auto()
    FAILED = auto()
    INTERRUPTED = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class ExecutionJournalEntry:
    """Entrada do journal de execução."""

    entry_id: str
    request_id: str
    component_id: str
    tool_id: str
    params_hash: str
    params: Mapping[str, Any]
    result: Mapping[str, Any] | None
    status: ExecutionStatusJournal
    approval_id: str | None
    task_id: str
    plan_id: str
    started_at: float
    completed_at: float | None
    duration_ms: float | None
    error: str | None
    integrity_hash: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "request_id": self.request_id,
            "component_id": self.component_id,
            "tool_id": self.tool_id,
            "params_hash": self.params_hash,
            "params": dict(self.params),
            "result": dict(self.result) if self.result else None,
            "status": self.status.name,
            "approval_id": self.approval_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "integrity_hash": self.integrity_hash,
            "created_at": self.created_at,
        }

    def verify_integrity(self) -> bool:
        """Verifica a integridade da entrada recalculando o hash."""
        return self.integrity_hash == self._compute_hash()

    def _compute_hash(self) -> str:
        payload = {
            "request_id": self.request_id,
            "component_id": self.component_id,
            "tool_id": self.tool_id,
            "params_hash": self.params_hash,
            "result": json.dumps(dict(self.result), sort_keys=True) if self.result else None,
            "status": self.status.name,
            "approval_id": self.approval_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ExecutionJournal:
    """Journal de execução persistente em SQLite."""

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
                CREATE TABLE IF NOT EXISTS execution_journal (
                    entry_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    component_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    params TEXT NOT NULL,
                    result TEXT,
                    status TEXT NOT NULL,
                    approval_id TEXT,
                    task_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    duration_ms REAL,
                    error TEXT,
                    integrity_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_request ON execution_journal(request_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_tool ON execution_journal(tool_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_status ON execution_journal(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_task ON execution_journal(task_id)"
            )
            conn.commit()

    def record_entry(
        self,
        *,
        request_id: str,
        component_id: str,
        tool_id: str,
        params: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        status: ExecutionStatusJournal = ExecutionStatusJournal.AUTHORIZED,
        approval_id: str | None = None,
        task_id: str = "unknown",
        plan_id: str = "default",
        started_at: float | None = None,
        completed_at: float | None = None,
        error: str | None = None,
    ) -> str:
        """Insere uma nova entrada no journal (append-only)."""
        entry_id = uuid.uuid4().hex
        params_json = json.dumps(dict(params), sort_keys=True, separators=(",", ":"))
        params_hash = hashlib.sha256(params_json.encode()).hexdigest()
        result_json = json.dumps(dict(result), sort_keys=True, separators=(",", ":")) if result else None
        now = time.time()
        started = started_at if started_at is not None else now
        duration_ms = None
        if completed_at is not None and started_at is not None:
            duration_ms = (completed_at - started_at) * 1000.0

        entry = ExecutionJournalEntry(
            entry_id=entry_id,
            request_id=request_id,
            component_id=component_id,
            tool_id=tool_id,
            params_hash=params_hash,
            params=dict(params),
            result=dict(result) if result else None,
            status=status,
            approval_id=approval_id,
            task_id=task_id,
            plan_id=plan_id,
            started_at=started,
            completed_at=completed_at,
            duration_ms=duration_ms,
            error=error,
            integrity_hash="",  # preenchido abaixo
            created_at=now,
        )
        integrity_hash = entry._compute_hash()
        entry = ExecutionJournalEntry(
            entry_id=entry.entry_id,
            request_id=entry.request_id,
            component_id=entry.component_id,
            tool_id=entry.tool_id,
            params_hash=entry.params_hash,
            params=entry.params,
            result=entry.result,
            status=entry.status,
            approval_id=entry.approval_id,
            task_id=entry.task_id,
            plan_id=entry.plan_id,
            started_at=entry.started_at,
            completed_at=entry.completed_at,
            duration_ms=entry.duration_ms,
            error=entry.error,
            integrity_hash=integrity_hash,
            created_at=entry.created_at,
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_journal
                (entry_id, request_id, component_id, tool_id, params_hash, params,
                 result, status, approval_id, task_id, plan_id, started_at,
                 completed_at, duration_ms, error, integrity_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.entry_id,
                    entry.request_id,
                    entry.component_id,
                    entry.tool_id,
                    entry.params_hash,
                    params_json,
                    result_json,
                    entry.status.name,
                    entry.approval_id,
                    entry.task_id,
                    entry.plan_id,
                    entry.started_at,
                    entry.completed_at,
                    entry.duration_ms,
                    entry.error,
                    entry.integrity_hash,
                    entry.created_at,
                ),
            )
            conn.commit()
        return entry_id

    def update_status(
        self,
        request_id: str,
        status: ExecutionStatusJournal,
        *,
        result: Mapping[str, Any] | None = None,
        completed_at: float | None = None,
        error: str | None = None,
    ) -> bool:
        """Atualiza o status de uma entrada existente (apenas uma vez por campo final)."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT entry_id, status FROM execution_journal WHERE request_id = ? ORDER BY created_at DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            if row is None:
                return False
            entry_id, current_status = row
            # Não permite regressão de status final.
            final_statuses = {
                ExecutionStatusJournal.COMPLETED,
                ExecutionStatusJournal.FAILED,
                ExecutionStatusJournal.INTERRUPTED,
                ExecutionStatusJournal.UNKNOWN,
            }
            if ExecutionStatusJournal[current_status] in final_statuses and status not in final_statuses:
                return False

            result_json = json.dumps(dict(result), sort_keys=True, separators=(",", ":")) if result else None
            duration_ms = None
            if completed_at is not None:
                started_row = conn.execute(
                    "SELECT started_at FROM execution_journal WHERE entry_id = ?", (entry_id,)
                ).fetchone()
                if started_row:
                    duration_ms = (completed_at - started_row[0]) * 1000.0

            conn.execute(
                """
                UPDATE execution_journal
                SET status = ?, result = ?, completed_at = ?, duration_ms = ?, error = ?
                WHERE entry_id = ?
                """,
                (
                    status.name,
                    result_json,
                    completed_at,
                    duration_ms,
                    error,
                    entry_id,
                ),
            )
            conn.commit()
        return True

    def get_entry(self, entry_id: str) -> ExecutionJournalEntry | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM execution_journal WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_entry(row)

    def get_by_request(self, request_id: str) -> list[ExecutionJournalEntry]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM execution_journal WHERE request_id = ? ORDER BY created_at DESC",
                (request_id,),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def list_entries(
        self,
        *,
        status: ExecutionStatusJournal | None = None,
        tool_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionJournalEntry]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM execution_journal"
            params: list[Any] = []
            clauses = []
            if status is not None:
                clauses.append("status = ?")
                params.append(status.name)
            if tool_id is not None:
                clauses.append("tool_id = ?")
                params.append(tool_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_entry(r) for r in rows]

    def verify_all_integrity(self) -> dict[str, Any]:
        """Verifica a integridade de todas as entradas do journal."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM execution_journal").fetchall()
            conn.row_factory = sqlite3.Row
            total = 0
            invalid = 0
            for row in rows:
                total += 1
                entry = self._row_to_entry(row)
                if not entry.verify_integrity():
                    invalid += 1
            return {"total": total, "invalid": invalid, "ok": invalid == 0}

    def _row_to_entry(self, row: sqlite3.Row) -> ExecutionJournalEntry:
        return ExecutionJournalEntry(
            entry_id=row["entry_id"],
            request_id=row["request_id"],
            component_id=row["component_id"],
            tool_id=row["tool_id"],
            params_hash=row["params_hash"],
            params=json.loads(row["params"]),
            result=json.loads(row["result"]) if row["result"] else None,
            status=ExecutionStatusJournal[row["status"]],
            approval_id=row["approval_id"],
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
            error=row["error"],
            integrity_hash=row["integrity_hash"],
            created_at=row["created_at"],
        )


__all__ = [
    "ExecutionJournal",
    "ExecutionJournalEntry",
    "ExecutionStatusJournal",
]
