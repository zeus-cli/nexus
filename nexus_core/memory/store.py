"""Conexao SQLite governada: WAL, migrações versionadas, integridade e backup."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..privatefs import ensure_private_file

Migration = tuple[int, str, tuple[str, ...]]

MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "initial_event_store",
        (
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, source TEXT NOT NULL, severity TEXT NOT NULL, payload TEXT NOT NULL, dedup_key TEXT, correlation_id TEXT, created_at TEXT NOT NULL, recorded_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_events_created_at ON events (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_events_kind_created ON events (kind, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_events_severity_created ON events (severity, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_events_dedup ON events (dedup_key, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_events_correlation ON events (correlation_id)",
        ),
    ),
    (
        2,
        "structured_memory",
        (
            "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN (\'short_term\', \'long_term\', \'episodic\', \'semantic\')), title TEXT NOT NULL DEFAULT \'\', content TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT \'{}\', source_event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT)",
            "CREATE INDEX IF NOT EXISTS ix_memories_kind_updated ON memories (kind, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_memories_expires ON memories (expires_at)",
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, title, content, tokenize = 'unicode61')",
            "CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN INSERT INTO memory_fts(memory_id, title, content) VALUES (new.memory_id, new.title, new.content); END",
            "CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN DELETE FROM memory_fts WHERE memory_id = old.memory_id; END",
            "CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN DELETE FROM memory_fts WHERE memory_id = old.memory_id; INSERT INTO memory_fts(memory_id, title, content) VALUES (new.memory_id, new.title, new.content); END",
        ),
    ),
    (
        3,
        "add_privacy_level",
        ("ALTER TABLE memories ADD COLUMN privacy_level INTEGER NOT NULL DEFAULT 0",),
    ),
)

class MigrationError(Exception):
    """Migração falhou; nenhuma alteração parcial é preservada."""

class Database:
    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self._timeout = timeout
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def is_memory(self) -> bool:
        return str(self.path) == ":memory:"

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if not self.is_memory:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                ensure_private_file(self.path)
            conn = sqlite3.connect(
                str(self.path),
                timeout=self._timeout,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            if not self.is_memory:
                conn.execute("PRAGMA journal_mode = WAL")
                ensure_private_file(self.path)
            conn.execute("PRAGMA synchronous = NORMAL")
            self._conn = conn
            return conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def schema_version(self) -> int:
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if row is None:
                return 0
            current = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            return int(current or 0)

    def migrate(self) -> list[int]:
        applied: list[int] = []
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            done = {
                int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
            }
            for version, name, statements in MIGRATIONS:
                if version in done:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, datetime.now(timezone.utc).isoformat()),
                    )
                except Exception as exc:
                    conn.execute("ROLLBACK")
                    raise MigrationError(
                        f"falha na migração {version} ({name}): {exc}") from exc
                conn.execute("COMMIT")
                applied.append(version)
        return applied

    def pending_migrations(self) -> list[int]:
        current = self.schema_version()
        return [version for version, _, _ in MIGRATIONS if version > current]

    def integrity_check(self, *, quick: bool = False) -> str:
        pragma = "PRAGMA quick_check" if quick else "PRAGMA integrity_check"
        with self._lock:
            rows = self.connect().execute(pragma).fetchall()
        return "; ".join(str(row[0]) for row in rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connect().execute(sql, tuple(params)).fetchall()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            conn = self.connect()
            cursor = conn.execute(sql, tuple(params))
            return cursor.rowcount

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_private_file(target)
        with self._lock:
            source = self.connect()
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
            ensure_private_file(target)
        return target

    def vacuum(self) -> None:
        with self._lock:
            self.connect().execute("VACUUM")

    def size_bytes(self) -> int:
        if self.is_memory:
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                total += candidate.stat().st_size
        return total
