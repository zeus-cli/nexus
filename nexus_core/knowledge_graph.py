"""Knowledge Graph persistente em SQLite.

O grafo compartilha o `Database` governado do EventStore. Cada nó e aresta
carrega proveniência (`kind`, `source`, `source_ref`), confiança, validade
temporal e evidência de validação. Regras centrais:

* `KnowledgeKind.SIMULATION` nunca é promovido a `FACT` automaticamente;
* toda escrita valida tipos, limites e JSON antes de tocar o banco;
* consultas são determinísticas e limitadas;
* apagar um nó remove suas arestas por `ON DELETE CASCADE`.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .knowledge_engine import KnowledgeKind, KnowledgeSource

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)

_MAX_LABEL = 4096
_MAX_TEXT = 256
_MAX_LIMIT = 10_000
_MAX_DEPTH = 32
_MAX_SEARCH_CANDIDATES = 5_000
_MAX_EVIDENCE_ITEMS = 1_000
_DIRECTIONS = frozenset({"out", "in", "both"})


class KnowledgeGraphError(RuntimeError):
    """Registro corrompido ou operação inválida no grafo."""


@dataclass(frozen=True, slots=True)
class GraphNode:
    """Nó de conhecimento com proveniência e confiança."""

    node_id: str
    node_type: str
    label: str
    properties: dict[str, Any]
    kind: KnowledgeKind
    source: KnowledgeSource
    source_ref: str | None
    confidence: float
    created_at: str
    updated_at: str
    expires_at: str | None
    validated_by: str | None
    validation_evidence: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "properties": dict(self.properties),
            "kind": self.kind.name,
            "source": self.source.name,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "validated_by": self.validated_by,
            "validation_evidence": list(self.validation_evidence),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """Aresta dirigida entre dois nós, também com proveniência e confiança."""

    edge_id: str
    source_id: str
    target_id: str
    relation: str
    properties: dict[str, Any]
    kind: KnowledgeKind
    source: KnowledgeSource
    source_ref: str | None
    confidence: float
    created_at: str
    updated_at: str
    expires_at: str | None
    validated_by: str | None
    validation_evidence: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "properties": dict(self.properties),
            "kind": self.kind.name,
            "source": self.source.name,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "validated_by": self.validated_by,
            "validation_evidence": list(self.validation_evidence),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class GraphPath:
    """Resultado de uma travessia: nó, profundidade e aresta de entrada."""

    node: GraphNode
    depth: int
    via_edge: GraphEdge | None
    parent_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.as_dict(),
            "depth": self.depth,
            "via_edge": self.via_edge.as_dict() if self.via_edge else None,
            "parent_id": self.parent_id,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, field_name: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} deve ser string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} não pode ser vazio")
    if len(text) > max_len:
        raise ValueError(f"{field_name} excede {max_len} caracteres")
    if "\x00" in text:
        raise ValueError(f"{field_name} não pode conter NUL")
    return text


def _optional_text(value: Any, field_name: str, *, max_len: int) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, max_len=max_len)


def _identifier(value: Any, field_name: str) -> str:
    text = _text(value, field_name, max_len=64)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise ValueError(
            f"{field_name} deve começar com letra minúscula e usar apenas "
            "letras minúsculas, dígitos, ponto, hífen ou underscore"
        )
    return text


def _node_id(value: Any) -> str:
    text = _text(value, "node_id", max_len=128)
    if not _NODE_ID_RE.fullmatch(text):
        raise ValueError("node_id contém caracteres inválidos")
    return text


def _enum(
    value: Any,
    enum_cls: type[KnowledgeKind | KnowledgeSource],
    field_name: str,
) -> KnowledgeKind | KnowledgeSource:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls[value.strip().upper()]
        except KeyError:
            pass
    options = ", ".join(enum_cls.__members__)
    raise ValueError(f"{field_name} inválido; use um de: {options}")


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("confidence deve ser numérico")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("confidence deve estar entre 0.0 e 1.0")
    return number


def _expires_at(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expires_at deve ser string ISO-8601 ou None")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expires_at deve ser ISO-8601 válido") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at deve incluir timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _is_expired(value: Any) -> bool:
    """Validade de um timestamp persistido; ausência de prazo nunca expira."""
    if value is None:
        return False
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise KnowledgeGraphError(f"expires_at persistido inválido: {exc}") from exc
    if parsed.tzinfo is None:
        raise KnowledgeGraphError("expires_at persistido sem timezone")
    return parsed <= datetime.now(timezone.utc)


def _encode_object(value: Any, field_name: str) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} deve ser um objeto JSON")
    data = dict(value)
    for key in data:
        if not isinstance(key, str):
            raise TypeError(f"{field_name} deve ter chaves string")
        if "\x00" in key:
            raise ValueError(f"{field_name} não pode conter NUL nas chaves")
    try:
        return json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} não é JSON serializável: {exc}") from exc


def _decode_object(raw: Any, field_name: str) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise KnowledgeGraphError(f"{field_name} persistido inválido: {exc}") from exc
    if not isinstance(data, dict):
        raise KnowledgeGraphError(f"{field_name} persistido não é um objeto JSON")
    return data


def _encode_evidence(evidence: Any) -> str:
    if evidence is None:
        return "[]"
    if isinstance(evidence, str) or not isinstance(evidence, Sequence):
        raise TypeError("validation_evidence deve ser uma sequência de strings")
    items: list[str] = []
    for item in evidence:
        text = _text(item, "validation_evidence", max_len=_MAX_LABEL)
        if text not in items:
            items.append(text)
        if len(items) > _MAX_EVIDENCE_ITEMS:
            raise ValueError("validation_evidence excede o limite de itens")
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _decode_evidence(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise KnowledgeGraphError(
            f"validation_evidence persistida inválida: {exc}"
        ) from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise KnowledgeGraphError("validation_evidence persistida não é uma lista de strings")
    return tuple(data)


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit deve ser inteiro entre 1 e {_MAX_LIMIT}")
    return value


def _max_depth(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_DEPTH:
        raise ValueError(f"max_depth deve ser inteiro entre 0 e {_MAX_DEPTH}")
    return value


def _direction(value: Any) -> str:
    if not isinstance(value, str) or value not in _DIRECTIONS:
        raise ValueError("direction deve ser 'out', 'in' ou 'both'")
    return value


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} deve ser booleano")
    return value


def _prepare_node(
    *,
    node_type: Any,
    label: Any,
    properties: Any,
    kind: Any,
    source: Any,
    source_ref: Any,
    confidence: Any,
    expires_at: Any,
    metadata: Any,
    validated_by: Any,
    validation_evidence: Any,
) -> tuple[Any, ...]:
    return (
        _identifier(node_type, "node_type"),
        _text(label, "label", max_len=_MAX_LABEL),
        _encode_object(properties, "properties"),
        _enum(kind, KnowledgeKind, "kind").name,
        _enum(source, KnowledgeSource, "source").name,
        _optional_text(source_ref, "source_ref", max_len=_MAX_TEXT),
        _confidence(confidence),
        _expires_at(expires_at),
        _optional_text(validated_by, "validated_by", max_len=_MAX_TEXT),
        _encode_evidence(validation_evidence),
        _encode_object(metadata, "metadata"),
    )


def _prepare_edge(
    *,
    source_id: Any,
    target_id: Any,
    relation: Any,
    properties: Any,
    kind: Any,
    source: Any,
    source_ref: Any,
    confidence: Any,
    expires_at: Any,
    metadata: Any,
    validated_by: Any,
    validation_evidence: Any,
) -> tuple[Any, ...]:
    return (
        _node_id(source_id),
        _node_id(target_id),
        _identifier(relation, "relation"),
        _encode_object(properties, "properties"),
        _enum(kind, KnowledgeKind, "kind").name,
        _enum(source, KnowledgeSource, "source").name,
        _optional_text(source_ref, "source_ref", max_len=_MAX_TEXT),
        _confidence(confidence),
        _expires_at(expires_at),
        _optional_text(validated_by, "validated_by", max_len=_MAX_TEXT),
        _encode_evidence(validation_evidence),
        _encode_object(metadata, "metadata"),
    )


class KnowledgeGraph:
    """Grafo persistente de nós e arestas sobre um `Database` SQLite."""

    def __init__(self, database: Any) -> None:
        self._db = database

    @property
    def database(self) -> Any:
        return self._db

    # -- infraestrutura ------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._db.lock:
            conn = self._db.connection
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> GraphNode:
        try:
            kind = KnowledgeKind[row["kind"]]
            source = KnowledgeSource[row["source"]]
        except KeyError as exc:
            raise KnowledgeGraphError(
                f"proveniência persistida inválida no nó {row['node_id']}: {exc}"
            ) from exc
        return GraphNode(
            node_id=str(row["node_id"]),
            node_type=str(row["node_type"]),
            label=str(row["label"]),
            properties=_decode_object(row["properties"], "properties"),
            kind=kind,
            source=source,
            source_ref=row["source_ref"],
            confidence=float(row["confidence"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            expires_at=row["expires_at"],
            validated_by=row["validated_by"],
            validation_evidence=_decode_evidence(row["validation_evidence"]),
            metadata=_decode_object(row["metadata"], "metadata"),
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
        try:
            kind = KnowledgeKind[row["kind"]]
            source = KnowledgeSource[row["source"]]
        except KeyError as exc:
            raise KnowledgeGraphError(
                f"proveniência persistida inválida na aresta {row['edge_id']}: {exc}"
            ) from exc
        return GraphEdge(
            edge_id=str(row["edge_id"]),
            source_id=str(row["source_id"]),
            target_id=str(row["target_id"]),
            relation=str(row["relation"]),
            properties=_decode_object(row["properties"], "properties"),
            kind=kind,
            source=source,
            source_ref=row["source_ref"],
            confidence=float(row["confidence"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            expires_at=row["expires_at"],
            validated_by=row["validated_by"],
            validation_evidence=_decode_evidence(row["validation_evidence"]),
            metadata=_decode_object(row["metadata"], "metadata"),
        )

    # -- nós -----------------------------------------------------------------

    def add_node(
        self,
        node_type: Any,
        label: Any,
        *,
        node_id: Any = None,
        properties: Mapping[str, Any] | None = None,
        kind: Any = KnowledgeKind.ASSUMPTION,
        source: Any = KnowledgeSource.MODEL_INFERENCE,
        source_ref: str | None = None,
        confidence: float = 0.5,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validated_by: str | None = None,
        validation_evidence: Sequence[str] = (),
    ) -> GraphNode:
        identifier = _node_id(node_id) if node_id is not None else uuid.uuid4().hex
        values = _prepare_node(
            node_type=node_type,
            label=label,
            properties=properties,
            kind=kind,
            source=source,
            source_ref=source_ref,
            confidence=confidence,
            expires_at=expires_at,
            metadata=metadata,
            validated_by=validated_by,
            validation_evidence=validation_evidence,
        )
        (
            node_type_value,
            label_value,
            properties_json,
            kind_value,
            source_value,
            source_ref_value,
            confidence_value,
            expires_value,
            validated_by_value,
            evidence_json,
            metadata_json,
        ) = values
        now = _now()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_nodes WHERE node_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                raise KnowledgeGraphError(f"nó já existe: {identifier}")
            conn.execute(
                """
                INSERT INTO knowledge_nodes
                (node_id, node_type, label, properties, kind, source, source_ref,
                 confidence, created_at, updated_at, expires_at, validated_by,
                 validation_evidence, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    node_type_value,
                    label_value,
                    properties_json,
                    kind_value,
                    source_value,
                    source_ref_value,
                    confidence_value,
                    now,
                    now,
                    expires_value,
                    validated_by_value,
                    evidence_json,
                    metadata_json,
                ),
            )
        node = self.get_node(identifier)
        if node is None:
            raise KnowledgeGraphError("nó não pôde ser lido após inserção")
        return node

    def update_node(
        self,
        node_id: str,
        *,
        node_type: Any,
        label: Any,
        properties: Mapping[str, Any] | None = None,
        kind: Any = KnowledgeKind.ASSUMPTION,
        source: Any = KnowledgeSource.MODEL_INFERENCE,
        source_ref: str | None = None,
        confidence: float = 0.5,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validated_by: str | None = None,
        validation_evidence: Sequence[str] = (),
    ) -> GraphNode:
        identifier = _node_id(node_id)
        (
            node_type_value,
            label_value,
            properties_json,
            kind_value,
            source_value,
            source_ref_value,
            confidence_value,
            expires_value,
            validated_by_value,
            evidence_json,
            metadata_json,
        ) = _prepare_node(
            node_type=node_type,
            label=label,
            properties=properties,
            kind=kind,
            source=source,
            source_ref=source_ref,
            confidence=confidence,
            expires_at=expires_at,
            metadata=metadata,
            validated_by=validated_by,
            validation_evidence=validation_evidence,
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_nodes WHERE node_id = ?", (identifier,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"nó ausente: {identifier}")
            conn.execute(
                """
                UPDATE knowledge_nodes
                SET node_type = ?, label = ?, properties = ?, kind = ?, source = ?,
                    source_ref = ?, confidence = ?, updated_at = ?, expires_at = ?,
                    validated_by = ?, validation_evidence = ?, metadata = ?
                WHERE node_id = ?
                """,
                (
                    node_type_value,
                    label_value,
                    properties_json,
                    kind_value,
                    source_value,
                    source_ref_value,
                    confidence_value,
                    _now(),
                    expires_value,
                    validated_by_value,
                    evidence_json,
                    metadata_json,
                    identifier,
                ),
            )
        node = self.get_node(identifier)
        if node is None:
            raise KnowledgeGraphError("nó desapareceu após atualização")
        return node

    def get_node(self, node_id: str) -> GraphNode | None:
        identifier = _node_id(node_id)
        rows = self._db.query(
            "SELECT * FROM knowledge_nodes WHERE node_id = ?", (identifier,)
        )
        return self._node_from_row(rows[0]) if rows else None

    def list_nodes(
        self,
        *,
        node_type: str | None = None,
        kind: Any = None,
        source: Any = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[GraphNode]:
        include_expired = _bool(include_expired, "include_expired")
        clauses: list[str] = []
        params: list[Any] = []
        if node_type is not None:
            clauses.append("node_type = ?")
            params.append(_identifier(node_type, "node_type"))
        if kind is not None:
            clauses.append("kind = ?")
            params.append(_enum(kind, KnowledgeKind, "kind").name)
        if source is not None:
            clauses.append("source = ?")
            params.append(_enum(source, KnowledgeSource, "source").name)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_now())
        sql = "SELECT * FROM knowledge_nodes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, node_id ASC LIMIT ?"
        params.append(_limit(limit))
        return [self._node_from_row(row) for row in self._db.query(sql, params)]

    def delete_node(self, node_id: str) -> bool:
        identifier = _node_id(node_id)
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_nodes WHERE node_id = ?", (identifier,)
            )
            return cursor.rowcount > 0

    # -- arestas -------------------------------------------------------------

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        *,
        edge_id: str | None = None,
        properties: Mapping[str, Any] | None = None,
        kind: Any = KnowledgeKind.ASSUMPTION,
        source: Any = KnowledgeSource.MODEL_INFERENCE,
        source_ref: str | None = None,
        confidence: float = 0.5,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validated_by: str | None = None,
        validation_evidence: Sequence[str] = (),
    ) -> GraphEdge:
        identifier = _node_id(edge_id) if edge_id is not None else uuid.uuid4().hex
        (
            source_value,
            target_value,
            relation_value,
            properties_json,
            kind_value,
            provenance_value,
            source_ref_value,
            confidence_value,
            expires_value,
            validated_by_value,
            evidence_json,
            metadata_json,
        ) = _prepare_edge(
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            properties=properties,
            kind=kind,
            source=source,
            source_ref=source_ref,
            confidence=confidence,
            expires_at=expires_at,
            metadata=metadata,
            validated_by=validated_by,
            validation_evidence=validation_evidence,
        )
        now = _now()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_edges WHERE edge_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                raise KnowledgeGraphError(f"aresta já existe: {identifier}")
            for node_id_value in (source_value, target_value):
                if conn.execute(
                    "SELECT 1 FROM knowledge_nodes WHERE node_id = ?",
                    (node_id_value,),
                ).fetchone() is None:
                    raise KnowledgeGraphError(f"nó ausente: {node_id_value}")
            conn.execute(
                """
                INSERT INTO knowledge_edges
                (edge_id, source_id, target_id, relation, properties, kind, source,
                 source_ref, confidence, created_at, updated_at, expires_at,
                 validated_by, validation_evidence, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    source_value,
                    target_value,
                    relation_value,
                    properties_json,
                    kind_value,
                    provenance_value,
                    source_ref_value,
                    confidence_value,
                    now,
                    now,
                    expires_value,
                    validated_by_value,
                    evidence_json,
                    metadata_json,
                ),
            )
        edge = self.get_edge(identifier)
        if edge is None:
            raise KnowledgeGraphError("aresta não pôde ser lida após inserção")
        return edge

    def update_edge(
        self,
        edge_id: str,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        properties: Mapping[str, Any] | None = None,
        kind: Any = KnowledgeKind.ASSUMPTION,
        source: Any = KnowledgeSource.MODEL_INFERENCE,
        source_ref: str | None = None,
        confidence: float = 0.5,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validated_by: str | None = None,
        validation_evidence: Sequence[str] = (),
    ) -> GraphEdge:
        identifier = _node_id(edge_id)
        (
            source_value,
            target_value,
            relation_value,
            properties_json,
            kind_value,
            provenance_value,
            source_ref_value,
            confidence_value,
            expires_value,
            validated_by_value,
            evidence_json,
            metadata_json,
        ) = _prepare_edge(
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            properties=properties,
            kind=kind,
            source=source,
            source_ref=source_ref,
            confidence=confidence,
            expires_at=expires_at,
            metadata=metadata,
            validated_by=validated_by,
            validation_evidence=validation_evidence,
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_edges WHERE edge_id = ?", (identifier,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"aresta ausente: {identifier}")
            for node_id_value in (source_value, target_value):
                if conn.execute(
                    "SELECT 1 FROM knowledge_nodes WHERE node_id = ?",
                    (node_id_value,),
                ).fetchone() is None:
                    raise KnowledgeGraphError(f"nó ausente: {node_id_value}")
            conn.execute(
                """
                UPDATE knowledge_edges
                SET source_id = ?, target_id = ?, relation = ?, properties = ?,
                    kind = ?, source = ?, source_ref = ?, confidence = ?,
                    updated_at = ?, expires_at = ?, validated_by = ?,
                    validation_evidence = ?, metadata = ?
                WHERE edge_id = ?
                """,
                (
                    source_value,
                    target_value,
                    relation_value,
                    properties_json,
                    kind_value,
                    provenance_value,
                    source_ref_value,
                    confidence_value,
                    _now(),
                    expires_value,
                    validated_by_value,
                    evidence_json,
                    metadata_json,
                    identifier,
                ),
            )
        edge = self.get_edge(identifier)
        if edge is None:
            raise KnowledgeGraphError("aresta desapareceu após atualização")
        return edge

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        identifier = _node_id(edge_id)
        rows = self._db.query(
            "SELECT * FROM knowledge_edges WHERE edge_id = ?", (identifier,)
        )
        return self._edge_from_row(rows[0]) if rows else None

    def list_edges(
        self,
        *,
        source_id: str | None = None,
        target_id: str | None = None,
        relation: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[GraphEdge]:
        include_expired = _bool(include_expired, "include_expired")
        clauses: list[str] = []
        params: list[Any] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(_node_id(source_id))
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(_node_id(target_id))
        if relation is not None:
            clauses.append("relation = ?")
            params.append(_identifier(relation, "relation"))
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_now())
        sql = "SELECT * FROM knowledge_edges"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY relation ASC, edge_id ASC LIMIT ?"
        params.append(_limit(limit))
        return [self._edge_from_row(row) for row in self._db.query(sql, params)]

    def delete_edge(self, edge_id: str) -> bool:
        identifier = _node_id(edge_id)
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_edges WHERE edge_id = ?", (identifier,)
            )
            return cursor.rowcount > 0

    # -- consultas -----------------------------------------------------------

    def neighbors(
        self,
        node_id: str,
        *,
        relation: str | None = None,
        direction: str = "both",
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[GraphNode]:
        limit = _limit(limit)
        paths = self._traverse(
            node_id,
            relation=relation,
            direction=direction,
            max_depth=1,
            max_results=limit + 1,
            include_expired=include_expired,
        )
        return [path.node for path in paths[1:]]

    def traverse(
        self,
        start_id: str,
        *,
        relation: str | None = None,
        direction: str = "out",
        max_depth: int = 1,
        limit: int = 1_000,
        include_expired: bool = False,
    ) -> list[GraphPath]:
        return self._traverse(
            start_id,
            relation=relation,
            direction=direction,
            max_depth=max_depth,
            max_results=_limit(limit),
            include_expired=include_expired,
        )

    def _traverse(
        self,
        start_id: str,
        *,
        relation: str | None,
        direction: str,
        max_depth: int,
        max_results: int,
        include_expired: bool,
    ) -> list[GraphPath]:
        start = _node_id(start_id)
        direction = _direction(direction)
        max_depth = _max_depth(max_depth)
        if max_results < 1:
            raise ValueError("max_results deve ser positivo")
        include_expired = _bool(include_expired, "include_expired")
        relation_value = (
            _identifier(relation, "relation") if relation is not None else None
        )
        root = self.get_node(start)
        if root is None:
            raise KeyError(f"nó ausente: {start}")

        paths = [GraphPath(node=root, depth=0, via_edge=None, parent_id=None)]
        visited = {root.node_id}
        queue: deque[tuple[GraphNode, int]] = deque([(root, 0)])
        while queue and len(paths) < max_results:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            edges: list[GraphEdge] = []
            if direction in ("out", "both"):
                edges.extend(
                    self.list_edges(
                        source_id=node.node_id,
                        relation=relation_value,
                        include_expired=include_expired,
                        limit=_MAX_LIMIT,
                    )
                )
            if direction in ("in", "both"):
                edges.extend(
                    self.list_edges(
                        target_id=node.node_id,
                        relation=relation_value,
                        include_expired=include_expired,
                        limit=_MAX_LIMIT,
                    )
                )
            seen_edges: set[str] = set()
            for edge in sorted(edges, key=lambda item: (item.relation, item.edge_id)):
                if edge.edge_id in seen_edges:
                    continue
                seen_edges.add(edge.edge_id)
                if direction == "out":
                    neighbor_id = edge.target_id
                elif direction == "in":
                    neighbor_id = edge.source_id
                elif edge.source_id == node.node_id:
                    neighbor_id = edge.target_id
                else:
                    neighbor_id = edge.source_id
                if neighbor_id in visited:
                    continue
                neighbor = self.get_node(neighbor_id)
                if neighbor is None:
                    continue
                if not include_expired and _is_expired(neighbor.expires_at):
                    visited.add(neighbor_id)
                    continue
                visited.add(neighbor_id)
                paths.append(
                    GraphPath(
                        node=neighbor,
                        depth=depth + 1,
                        via_edge=edge,
                        parent_id=node.node_id,
                    )
                )
                queue.append((neighbor, depth + 1))
                if len(paths) >= max_results:
                    break
        return paths

    def search_nodes(
        self,
        query: str,
        *,
        node_type: str | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> list[GraphNode]:
        query = _text(query, "query", max_len=512)
        include_expired = _bool(include_expired, "include_expired")
        limit = _limit(limit)
        terms: list[str] = []
        for term in _WORD_RE.findall(query.lower()):
            if len(term) > 2 and term not in terms:
                terms.append(term)
        if not terms:
            return []
        term_clauses = [
            "instr(lower(label || ' ' || properties), ?) > 0" for _ in terms
        ]
        clauses = ["(" + " OR ".join(term_clauses) + ")"]
        params: list[Any] = list(terms)
        if node_type is not None:
            clauses.append("node_type = ?")
            params.append(_identifier(node_type, "node_type"))
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_now())
        sql = (
            "SELECT * FROM knowledge_nodes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, node_id ASC LIMIT ?"
        )
        params.append(_MAX_SEARCH_CANDIDATES)
        candidates = [self._node_from_row(row) for row in self._db.query(sql, params)]
        scored: list[tuple[int, GraphNode]] = []
        for node in candidates:
            haystack = (
                node.label
                + " "
                + json.dumps(
                    node.properties,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            scored.append((score, node))
        scored.sort(key=lambda item: item[1].node_id, reverse=True)
        scored.sort(key=lambda item: item[1].updated_at, reverse=True)
        scored.sort(key=lambda item: item[0], reverse=True)
        return [node for _, node in scored[:limit]]

    # -- proveniência --------------------------------------------------------

    def promote_node_to_fact(
        self,
        node_id: str,
        *,
        validated_by: str,
        validation_evidence: Sequence[str] = (),
    ) -> bool:
        identifier = _node_id(node_id)
        validator = _text(validated_by, "validated_by", max_len=_MAX_TEXT)
        evidence_json = _encode_evidence(validation_evidence)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT kind, confidence FROM knowledge_nodes WHERE node_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            if row["kind"] == KnowledgeKind.SIMULATION.name:
                return False
            confidence = max(float(row["confidence"]), 0.8)
            conn.execute(
                """
                UPDATE knowledge_nodes
                SET kind = ?, confidence = ?, validated_by = ?,
                    validation_evidence = ?, updated_at = ?
                WHERE node_id = ?
                """,
                (
                    KnowledgeKind.FACT.name,
                    confidence,
                    validator,
                    evidence_json,
                    _now(),
                    identifier,
                ),
            )
            return True

    def mark_node_as_simulation(self, node_id: str) -> bool:
        identifier = _node_id(node_id)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT kind, confidence, validated_by, metadata "
                "FROM knowledge_nodes WHERE node_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            if row["kind"] == KnowledgeKind.FACT.name and row["validated_by"]:
                return False
            metadata = _decode_object(row["metadata"], "metadata")
            metadata["flagged_as_simulation"] = True
            conn.execute(
                """
                UPDATE knowledge_nodes
                SET kind = ?, source = ?, confidence = ?, metadata = ?,
                    updated_at = ?
                WHERE node_id = ?
                """,
                (
                    KnowledgeKind.SIMULATION.name,
                    KnowledgeSource.SIMULATION_OUTPUT.name,
                    min(float(row["confidence"]), 0.6),
                    _encode_object(metadata, "metadata"),
                    _now(),
                    identifier,
                ),
            )
            return True

    def promote_edge_to_fact(
        self,
        edge_id: str,
        *,
        validated_by: str,
        validation_evidence: Sequence[str] = (),
    ) -> bool:
        identifier = _node_id(edge_id)
        validator = _text(validated_by, "validated_by", max_len=_MAX_TEXT)
        evidence_json = _encode_evidence(validation_evidence)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT kind, confidence FROM knowledge_edges WHERE edge_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            if row["kind"] == KnowledgeKind.SIMULATION.name:
                return False
            confidence = max(float(row["confidence"]), 0.8)
            conn.execute(
                """
                UPDATE knowledge_edges
                SET kind = ?, confidence = ?, validated_by = ?,
                    validation_evidence = ?, updated_at = ?
                WHERE edge_id = ?
                """,
                (
                    KnowledgeKind.FACT.name,
                    confidence,
                    validator,
                    evidence_json,
                    _now(),
                    identifier,
                ),
            )
            return True

    def mark_edge_as_simulation(self, edge_id: str) -> bool:
        identifier = _node_id(edge_id)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT kind, confidence, validated_by, metadata "
                "FROM knowledge_edges WHERE edge_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            if row["kind"] == KnowledgeKind.FACT.name and row["validated_by"]:
                return False
            metadata = _decode_object(row["metadata"], "metadata")
            metadata["flagged_as_simulation"] = True
            conn.execute(
                """
                UPDATE knowledge_edges
                SET kind = ?, source = ?, confidence = ?, metadata = ?,
                    updated_at = ?
                WHERE edge_id = ?
                """,
                (
                    KnowledgeKind.SIMULATION.name,
                    KnowledgeSource.SIMULATION_OUTPUT.name,
                    min(float(row["confidence"]), 0.6),
                    _encode_object(metadata, "metadata"),
                    _now(),
                    identifier,
                ),
            )
            return True

    # -- estatísticas --------------------------------------------------------

    def node_stats(self, *, node_type: str | None = None) -> dict[str, Any]:
        """Agrega somente nós; ``node_type`` restringe todos os contadores."""
        clause = ""
        params: tuple[Any, ...] = ()
        if node_type is not None:
            clause = " WHERE node_type = ?"
            params = (_identifier(node_type, "node_type"),)
        total = int(
            self._db.query(
                "SELECT COUNT(*) FROM knowledge_nodes" + clause, params
            )[0][0]
        )
        by_kind = {name: 0 for name in KnowledgeKind.__members__}
        for row in self._db.query(
            "SELECT kind, COUNT(*) AS total FROM knowledge_nodes"
            + clause
            + " GROUP BY kind",
            params,
        ):
            by_kind[str(row["kind"])] = int(row["total"])
        by_source = {name: 0 for name in KnowledgeSource.__members__}
        for row in self._db.query(
            "SELECT source, COUNT(*) AS total FROM knowledge_nodes"
            + clause
            + " GROUP BY source",
            params,
        ):
            by_source[str(row["source"])] = int(row["total"])
        by_node_type = {
            str(row["node_type"]): int(row["total"])
            for row in self._db.query(
                "SELECT node_type, COUNT(*) AS total FROM knowledge_nodes"
                + clause
                + " GROUP BY node_type ORDER BY node_type",
                params,
            )
        }
        return {
            "nodes": total,
            "by_kind": by_kind,
            "by_source": by_source,
            "by_node_type": by_node_type,
        }

    def stats(self) -> dict[str, Any]:
        node_stats = self.node_stats()
        edge_total = int(
            self._db.query("SELECT COUNT(*) FROM knowledge_edges")[0][0]
        )
        edge_kinds = {name: 0 for name in KnowledgeKind.__members__}
        for row in self._db.query(
            "SELECT kind, COUNT(*) AS total FROM knowledge_edges GROUP BY kind"
        ):
            edge_kinds[str(row["kind"])] = int(row["total"])
        by_relation = {
            str(row["relation"]): int(row["total"])
            for row in self._db.query(
                "SELECT relation, COUNT(*) AS total "
                "FROM knowledge_edges GROUP BY relation ORDER BY relation"
            )
        }
        return {
            "nodes": node_stats["nodes"],
            "edges": edge_total,
            "by_kind": node_stats["by_kind"],
            "by_source": node_stats["by_source"],
            "by_node_type": node_stats["by_node_type"],
            "edge_kinds": edge_kinds,
            "by_relation": by_relation,
        }


__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphPath",
    "KnowledgeGraph",
    "KnowledgeGraphError",
]
