
"""Secure Human-in-the-Loop Approval Manager for NEXUS."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ApprovalStatus(Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    CLAIMED = "CLAIMED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"

class ExecutionStatus(Enum):
    PREPARED = "PREPARED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_id: str
    params: Mapping[str, Any]
    task_id: str
    plan_id: str
    execution_mode: str
    access_level: str
    fs_scope: str
    net_scope: str
    resource_limits: str
    policy_version: str
    justification: str

@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    decision: str # "APPROVED" or "DENIED"
    approver: str
    timestamp: float = field(default_factory=time.time)

DEFAULT_APPROVALS_DB = Path("data") / "databases" / "nexus_approvals.db"


class ApprovalManager:
    def __init__(self, db_path: str | Path = DEFAULT_APPROVALS_DB) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    risk_level TEXT,
                    access_level TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    fs_scope TEXT,
                    net_scope TEXT,
                    resource_limits TEXT,
                    policy_version TEXT NOT NULL,
                    justification TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    consumed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS executions (
                    approval_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    result_summary TEXT,
                    FOREIGN KEY(approval_id) REFERENCES approvals(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON approvals(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task ON approvals(task_id)")
            conn.commit()

    def _canonicalize_and_hash(self, req: ApprovalRequest) -> str:
        params_json = json.dumps(req.params, sort_keys=True, separators=(",", ":"))
        payload = {
            "tool_id": req.tool_id,
            "params": params_json,
            "task_id": req.task_id,
            "plan_id": req.plan_id,
            "execution_mode": req.execution_mode,
            "access_level": req.access_level,
            "fs_scope": req.fs_scope,
            "net_scope": req.net_scope,
            "resource_limits": req.resource_limits,
            "policy_version": req.policy_version,
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload_json.encode()).hexdigest()

    def request_approval(self, req: ApprovalRequest, ttl_s: float = 3600.0) -> str:
        req_id = uuid.uuid4().hex
        params_hash = self._canonicalize_and_hash(req)
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO approvals (
                    id, task_id, plan_id, tool_id, params_hash, access_level, 
                    execution_mode, fs_scope, net_scope, resource_limits, 
                    policy_version, justification, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                req_id, req.task_id, req.plan_id, req.tool_id, params_hash, 
                req.access_level, req.execution_mode, req.fs_scope, req.net_scope, 
                req.resource_limits, req.policy_version, req.justification, 
                ApprovalStatus.PENDING.value, now, now + ttl_s
            ))
            conn.commit()
        return req_id

    def decide(self, request_id: str, decision: ApprovalDecision) -> None:
        status = ApprovalStatus.APPROVED.value if decision.decision == "APPROVED" else ApprovalStatus.DENIED.value
        with sqlite3.connect(self._db_path) as conn:
            res = conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ? AND status = ?",
                (status, decision.timestamp, decision.approver, request_id, ApprovalStatus.PENDING.value)
            )
            if res.rowcount == 0:
                raise ValueError(f"Request {request_id} not found or not in PENDING state")
            conn.commit()

    def claim_approval(self, request_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM approvals WHERE id = ?", (request_id,)
                ).fetchone()
                if not row or row[0] != ApprovalStatus.APPROVED.value:
                    return False
                exp = conn.execute("SELECT expires_at FROM approvals WHERE id = ?", (request_id,)).fetchone()
                if exp and time.time() > exp[0]:
                    conn.execute("UPDATE approvals SET status = ? WHERE id = ?", (ApprovalStatus.EXPIRED.value, request_id))
                    conn.commit()
                    return False
                conn.execute("UPDATE approvals SET status = ? WHERE id = ?", (ApprovalStatus.CLAIMED.value, request_id))
                conn.execute(
                    "INSERT INTO executions (approval_id, status, started_at) VALUES (?, ?, ?)",
                    (request_id, ExecutionStatus.PREPARED.value, time.time())
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def mark_starting(self, request_id: str) -> None:
        """Transitions approval to CONSUMED and execution to STARTING before subprocess launch."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                res = conn.execute(
                    "UPDATE approvals SET status = ?, consumed_at = ? WHERE id = ? AND status = ?",
                    (ApprovalStatus.CONSUMED.value, time.time(), request_id, ApprovalStatus.CLAIMED.value)
                )
                if res.rowcount == 0:
                    raise ValueError(f"Approval {request_id} cannot be started (not CLAIMED)")
                
                conn.execute(
                    "UPDATE executions SET status = ?, started_at = ? WHERE approval_id = ?",
                    (ExecutionStatus.STARTING.value, time.time(), request_id)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def update_execution_status(self, request_id: str, status: ExecutionStatus, summary: str | None = None) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE executions SET status = ?, completed_at = ?, result_summary = ? WHERE approval_id = ?",
                (status.value, time.time() if status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED) else None, summary, request_id)
            )
            conn.commit()

    def revoke(self, request_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            res = conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status IN (?, ?)",
                (ApprovalStatus.REVOKED.value, request_id, ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
            )
            if res.rowcount == 0:
                raise ValueError(f"Request {request_id} cannot be revoked (already consumed or not found)")
            conn.commit()

    def expire_stale(self) -> int:
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            res = conn.execute(
                "UPDATE approvals SET status = ? WHERE expires_at < ? AND status IN (?, ?)",
                (ApprovalStatus.EXPIRED.value, now, ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
            )
            count = res.rowcount
            conn.commit()
            return count

    def verify_action(self, request_id: str, req: ApprovalRequest) -> bool:
        current_hash = self._canonicalize_and_hash(req)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT params_hash FROM approvals WHERE id = ?", (request_id,)
            ).fetchone()
            if not row:
                return False
            return row[0] == current_hash

    def recover_orphans(self, timeout_s: float = 300.0) -> int:
        """Finds CLAIMED approvals with old STARTING/PREPARED executions and marks them UNKNOWN."""
        now = time.time()
        with sqlite3.connect(self._db_path) as conn:
            orphans = conn.execute(
                "SELECT approval_id FROM executions WHERE status IN (?, ?, ?) AND started_at < ?",
                (ExecutionStatus.STARTING.value, ExecutionStatus.RUNNING.value, ExecutionStatus.PREPARED.value, now - timeout_s)
            ).fetchall()
            
            count = 0
            for (aid,) in orphans:
                conn.execute("UPDATE executions SET status = ? WHERE approval_id = ?", (ExecutionStatus.UNKNOWN.value, aid))
                count += 1
            conn.commit()
            return count

    def list_pending(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM approvals WHERE status = ?", (ApprovalStatus.PENDING.value,)).fetchall()
            return [dict(r) for r in rows]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (request_id,)).fetchone()
            return dict(row) if row else None

    def get_history(
        self, limit: int = 100, status: str | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Returns a paginated history of all approval requests and their final states."""
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]
    