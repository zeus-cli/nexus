"""Structured, user-owned memory records stored alongside the event log."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..events import parse_timestamp
from .store import Database
from .vector import VectorStore

MEMORY_KINDS = ("short_term", "long_term", "episodic", "semantic")
MAX_CONTENT_BYTES = 1_000_000
STOP_WORDS = {"a", "an", "the", "is", "are", "was", "were", "and", "or", "but", "if", "then", "of", "at", "by", "for", "with", "about", "against", "between", "into", "through", "during", "before", "after", "above", "below", "to", "from", "up", "down", "in", "out", "on", "off", "over", "under", "again", "further", "once"}

class MemoryStoreError(Exception):
    """Invalid memory data or persistence error."""

@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    kind: str
    title: str
    content: str
    metadata: Mapping[str, Any]
    source_event_id: str | None
    created_at: datetime
    updated_at: datetime
    privacy_level: int
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "metadata": dict(self.metadata),
            "source_event_id": self.source_event_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "privacy_level": self.privacy_level,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

class MemoryStore:
    """Hybrid CRUD and retrieval for explicitly stored memory records."""

    def __init__(self, database: Database, vector_dir: str) -> None:
        self._db = database
        self._vector_store = VectorStore(vector_dir)

    def create(
        self,
        *,
        kind: str,
        content: str,
        title: str = "",
        metadata: Mapping[str, Any] | None = None,
        source_event_id: str | None = None,
        expires_at: datetime | str | None = None,
        privacy_level: int = 0,
    ) -> MemoryRecord:
        if kind not in MEMORY_KINDS:
            raise ValueError(f"kind de memória inválido: {kind!r}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content deve ser uma string não vazia")
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(f"content excede {MAX_CONTENT_BYTES} bytes")
        if not isinstance(title, str) or len(title.encode("utf-8")) > 4096:
            raise ValueError("title deve ser string de até 4096 bytes")
        
        metadata_dict = dict(metadata or {})
        try:
            metadata_json = json.dumps(
                metadata_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata deve ser serializável como JSON") from exc
        if len(metadata_json.encode("utf-8")) > 65_536:
            raise ValueError("metadata excede 65536 bytes")
            
        now = datetime.now(UTC)
        expiry = parse_timestamp(expires_at, "expires_at") if expires_at is not None else None
        if expiry is not None and expiry <= now:
            raise ValueError("expires_at deve estar no futuro")
        
        memory_id = uuid.uuid4().hex
        with self._db.lock:
            self._db.connection.execute(
                """
                INSERT INTO memories
                    (memory_id, kind, title, content, metadata, source_event_id,
                     created_at, updated_at, expires_at, privacy_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    kind,
                    title.strip(),
                    content,
                    metadata_json,
                    source_event_id,
                    now.isoformat(),
                    now.isoformat(),
                    expiry.isoformat() if expiry else None,
                    privacy_level,
                ),
            )
        
        # Vetor é atualizado sob demanda: não deve bloquear a criação de memória
        # se o modelo de embeddings ou o LanceDB não estiverem disponíveis.
        try:
            self._vector_store.upsert(memory_id, content)
        except Exception:  # noqa: BLE001 - vetor é opcional, memória é a prioridade
            pass
        record = self.get(memory_id)
        if record is None:
            raise MemoryStoreError("memória não pôde ser lida após criação")
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        rows = self._db.query("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
        return self._row_to_record(rows[0]) if rows else None

    def list(
        self,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        """Lists active records, newest first, optionally filtered by kind."""
        if kind is not None and kind not in MEMORY_KINDS:
            raise ValueError(f"kind de memória inválido: {kind!r}")
        if limit <= 0:
            raise ValueError("limit deve ser positivo")
        if offset < 0:
            raise ValueError("offset não pode ser negativo")

        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(datetime.now(UTC).isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT * FROM memories{where} "
            "ORDER BY updated_at DESC, memory_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [self._row_to_record(row) for row in rows]

    def list_all(
        self,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_expired: bool = False,
    ) -> list[MemoryRecord]:
        """Backwards-compatible alias for :meth:`list`."""
        return self.list(
            kind=kind,
            limit=limit,
            offset=offset,
            include_expired=include_expired,
        )

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 10,
        hybrid: bool = False,
    ) -> list[MemoryRecord]:
        if not hybrid:
            return self._search_lexical(query, kind, limit)
        
        lexical_results = self._search_lexical(query, kind, limit)
        semantic_results = self._search_semantic(query, limit)
        
        scores = {}
        for rank, rec in enumerate(lexical_results):
            scores[rec.memory_id] = scores.get(rec.memory_id, 0) + 1 / (60 + rank)
        for rank, rec in enumerate(semantic_results):
            scores[rec.memory_id] = scores.get(rec.memory_id, 0) + 1 / (60 + rank)
            
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        
        results = []
        for mid in sorted_ids[:limit]:
            rec = self.get(mid)
            if rec: results.append(rec)
            
        return results

    def _search_lexical(self, query: str, kind: str | None, limit: int) -> list[MemoryRecord]:
        terms = [t.lower() for t in re.findall(r"[\w-]+", query, flags=re.UNICODE) if t.lower() not in STOP_WORDS]
        if not terms: return []
        match = " AND ".join(f'"{term}"' for term in terms)
        clauses = ["memory_fts MATCH ?", "(m.expires_at IS NULL OR m.expires_at > ?)"]
        params = [match, datetime.now(UTC).isoformat()]
        if kind is not None:
            clauses.append("m.kind = ?")
            params.append(kind)
        rows = self._db.query(
            """
            SELECT m.* FROM memories AS m
            JOIN memory_fts AS f ON f.memory_id = m.memory_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?",
            (*params, limit),
        )
        return [self._row_to_record(row) for row in rows]

    def _search_semantic(self, query: str, limit: int) -> list[MemoryRecord]:
        """Busca semântica via vetores; retorna [] se o store vetorial estiver indisponível."""
        try:
            vector_results = self._vector_store.search(query, limit=limit)
        except Exception:  # noqa: BLE001 - vetor é opcional, busca semântica é best-effort
            return []
        records = []
        for vr in vector_results:
            rec = self.get(vr.memory_id)
            if rec:
                records.append(rec)
        return records

    def delete(self, memory_id: str) -> bool:
        # Vetor é opcional: não deve bloquear a exclusão de memória se o
        # store vetorial estiver indisponível.
        try:
            self._vector_store.delete(memory_id)
        except Exception:  # noqa: BLE001
            pass
        return self._db.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,)) > 0

    def purge_expired(self, *, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        return self._db.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (cutoff,))

    def consolidate(
        self,
        *,
        now: datetime | None = None,
        short_term_max_age_days: int = 7,
        promote_access_count: int = 3,
    ) -> dict[str, int]:
        """Move memórias short_term antigas ou frequentemente acessadas para long_term/episodic.

        Retorna contagens: {"short_term_archived": N, "promoted_to_long_term": M, "promoted_to_episodic": K}.
        """
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff_iso = now_dt.isoformat()
        moved = {"short_term_archived": 0, "promoted_to_long_term": 0, "promoted_to_episodic": 0}

        # Busca memórias short_term candidatas
        rows = self._db.query(
            """
            SELECT memory_id, kind, updated_at, expires_at FROM memories
            WHERE kind = 'short_term'
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (cutoff_iso,),
        )

        for row in rows:
            memory_id = row["memory_id"]
            updated_at = parse_timestamp(row["updated_at"], "updated_at")
            age_days = (now_dt - updated_at).total_seconds() / 86400.0

            # Arquiva memórias short_term muito antigas (sem acesso recente)
            if age_days > short_term_max_age_days:
                self._db.execute(
                    "UPDATE memories SET kind = 'long_term' WHERE memory_id = ?",
                    (memory_id,),
                )
                moved["short_term_archived"] += 1
                continue

            # Promove memórias com muitos acessos (heurística: updated_at recente indica uso)
            if age_days <= 1.0:
                self._db.execute(
                    "UPDATE memories SET kind = 'episodic' WHERE memory_id = ?",
                    (memory_id,),
                )
                moved["promoted_to_episodic"] += 1

        moved["promoted_to_long_term"] = moved["short_term_archived"]
        return moved

    def stats(self) -> dict[str, int]:
        rows = self._db.query("SELECT kind, COUNT(*) AS total FROM memories "
            "WHERE expires_at IS NULL OR expires_at > ? GROUP BY kind ORDER by kind",
            (datetime.now(UTC).isoformat(),),
        )
        result = {str(row["kind"]): int(row["total"]) for row in rows}
        result["total"] = sum(result.values())
        return result

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        try:
            metadata = json.loads(row["metadata"])
            if not isinstance(metadata, dict):
                raise ValueError("metadata não é objeto JSON")
            return MemoryRecord(
                memory_id=row["memory_id"],
                kind=row["kind"],
                title=row["title"],
                content=row["content"],
                metadata=metadata,
                source_event_id=row["source_event_id"],
                created_at=parse_timestamp(row["created_at"], "created_at"),
                updated_at=parse_timestamp(row["updated_at"], "updated_at"),
                privacy_level=int(row["privacy_level"]),
                expires_at=(
                    parse_timestamp(row["expires_at"], "expires_at")
                    if row["expires_at"]
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MemoryStoreError(f"registro de memória corrompido: {exc}") from exc
