"""Knowledge Engine — gestão de conhecimento com proveniência.

Responsabilidade: armazenar e consultar conhecimento distinguindo:

* fatos (verificados)
* suposições (não verificadas)
* hipóteses (a serem testadas)
* predições (estimativas futuras)
* informações geradas por simulação

Cada registro deve identificar sua origem (proveniência). Nunca promove
automaticamente resultados simulados ou suposições de modelo a fatos verificados.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .knowledge_graph import GraphNode, KnowledgeGraph


class KnowledgeKind(Enum):
    FACT = auto()           # Informação verificada
    ASSUMPTION = auto()     # Suposição não verificada
    HYPOTHESIS = auto()     # Hipótese a ser testada
    PREDICTION = auto()     # Estimativa de futuro
    SIMULATION = auto()     # Resultado de simulação (não é fato)


class KnowledgeSource(Enum):
    USER = auto()              # Fornecido pelo usuário
    OBSERVATION = auto()       # Observação direta do sistema
    EXTERNAL = auto()          # Fonte externa
    MODEL_INFERENCE = auto()   # Inferência de modelo
    SIMULATION_OUTPUT = auto() # Saída de simulação
    VALIDATED_EXPERIENCE = auto()  # Experiência operacional validada


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    """Um registro de conhecimento com proveniência e confiança."""

    record_id: str
    kind: KnowledgeKind
    content: str
    source: KnowledgeSource
    source_ref: str | None  # referência à origem (event_id, memory_id, etc.)
    confidence: float  # 0.0 a 1.0
    created_at: str
    updated_at: str
    expires_at: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    validated_by: str | None = None  # quem validou (se for fato validado)
    validation_evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind.name,
            "content": self.content,
            "source": self.source.name,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
            "validated_by": self.validated_by,
            "validation_evidence": list(self.validation_evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KnowledgeRecord":
        return cls(
            record_id=str(data["record_id"]),
            kind=KnowledgeKind[str(data.get("kind", "ASSUMPTION"))],
            content=str(data.get("content", "")),
            source=KnowledgeSource[str(data.get("source", "MODEL_INFERENCE"))],
            source_ref=data.get("source_ref"),
            confidence=float(data.get("confidence", 0.5)),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
            expires_at=data.get("expires_at"),
            metadata=dict(data.get("metadata", {})),
            validated_by=data.get("validated_by"),
            validation_evidence=tuple(data.get("validation_evidence", [])),
        )

    def is_fact(self) -> bool:
        return self.kind == KnowledgeKind.FACT

    def is_verified(self) -> bool:
        return self.kind == KnowledgeKind.FACT and self.validated_by is not None

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > datetime.fromisoformat(self.expires_at)


def _record_label(content: str) -> str:
    """Rótulo curto e determinístico para um registro persistido como nó."""
    line = " ".join(part.strip() for part in content.strip().splitlines() if part.strip())
    return line[:200] or "knowledge_record"


def _record_from_node(node: GraphNode) -> KnowledgeRecord:
    content = node.properties.get("content", "")
    return KnowledgeRecord(
        record_id=node.node_id,
        kind=node.kind,
        content=str(content),
        source=node.source,
        source_ref=node.source_ref,
        confidence=node.confidence,
        created_at=node.created_at,
        updated_at=node.updated_at,
        expires_at=node.expires_at,
        metadata=dict(node.metadata),
        validated_by=node.validated_by,
        validation_evidence=node.validation_evidence,
    )


class KnowledgeEngine:
    """Gerencia registros de conhecimento com proveniência.

    Com um :class:`~nexus_core.knowledge_graph.KnowledgeGraph` injetado, os
    registros são nós persistentes (``node_type="knowledge_record"``) e
    sobrevivem a reinícios. Sem grafo, o comportamento em memória é preservado
    para usos isolados e testes.
    """

    def __init__(self, store: KnowledgeGraph | None = None) -> None:
        self._store = store
        self._records: dict[str, KnowledgeRecord] = {}

    @property
    def store(self) -> KnowledgeGraph | None:
        return self._store

    def add(
        self,
        *,
        kind: KnowledgeKind,
        content: str,
        source: KnowledgeSource,
        source_ref: str | None = None,
        confidence: float = 0.5,
        expires_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> KnowledgeRecord:
        """Adiciona um novo registro de conhecimento.

        Nunca promove automaticamente simulação ou suposição a fato.
        """
        if not isinstance(content, str):
            raise TypeError("content deve ser string")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise TypeError("confidence deve ser numérico")
        confidence_value = float(confidence)
        if not 0.0 <= confidence_value <= 1.0:
            raise ValueError("confidence deve estar entre 0.0 e 1.0")
        if self._store is not None:
            node = self._store.add_node(
                "knowledge_record",
                _record_label(content),
                properties={"content": content},
                kind=kind,
                source=source,
                source_ref=source_ref,
                confidence=confidence_value,
                expires_at=expires_at,
                metadata=metadata,
            )
            return _record_from_node(node)

        record_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        record = KnowledgeRecord(
            record_id=record_id,
            kind=kind,
            content=content,
            source=source,
            source_ref=source_ref,
            confidence=confidence_value,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )
        self._records[record_id] = record
        return record

    def get(self, record_id: str) -> KnowledgeRecord | None:
        if self._store is not None:
            node = self._store.get_node(record_id)
            if node is None or node.node_type != "knowledge_record":
                return None
            return _record_from_node(node)
        return self._records.get(record_id)

    def list(
        self,
        *,
        kind: KnowledgeKind | None = None,
        source: KnowledgeSource | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> list[KnowledgeRecord]:
        if self._store is not None:
            nodes = self._store.list_nodes(
                node_type="knowledge_record",
                kind=kind,
                source=source,
                include_expired=include_expired,
                limit=limit,
            )
            return [_record_from_node(node) for node in nodes]
        results = []
        for record in self._records.values():
            if kind is not None and record.kind != kind:
                continue
            if source is not None and record.source != source:
                continue
            if not include_expired and record.is_expired():
                continue
            results.append(record)
        # Ordena por updated_at desc.
        results.sort(key=lambda r: r.updated_at, reverse=True)
        return results[:limit]

    def search(self, query: str, *, limit: int = 10) -> list[KnowledgeRecord]:
        """Busca simples por palavras-chave no conteúdo."""
        if self._store is not None:
            nodes = self._store.search_nodes(
                query, node_type="knowledge_record", limit=limit
            )
            return [_record_from_node(node) for node in nodes]
        terms = {t for t in re.findall(r"[\w-]+", query.lower()) if len(t) > 2}
        scored = []
        for record in self._records.values():
            if record.is_expired():
                continue
            content_terms = {
                t for t in re.findall(r"[\w-]+", record.content.lower()) if len(t) > 2
            }
            overlap = len(terms & content_terms)
            if overlap > 0:
                scored.append((overlap, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]

    def validate_fact(
        self,
        record_id: str,
        *,
        validated_by: str,
        evidence: tuple[str, ...] = (),
    ) -> bool:
        """Promove um registro a fato verificado.

        Registros SIMULATION nunca são promovidos a fato. A validação é sempre
        explícita e exige quem validou; simulações permanecem simulações.
        """
        if self._store is not None:
            node = self._store.get_node(record_id)
            if node is None or node.node_type != "knowledge_record":
                return False
            return self._store.promote_node_to_fact(
                record_id, validated_by=validated_by, validation_evidence=evidence
            )
        record = self._records.get(record_id)
        if record is None:
            return False
        if record.kind == KnowledgeKind.SIMULATION:
            # Simulações nunca viram fato automaticamente.
            return False
        now = datetime.now(timezone.utc).isoformat()
        updated = record.__class__(
            record_id=record.record_id,
            kind=KnowledgeKind.FACT,
            content=record.content,
            source=record.source,
            source_ref=record.source_ref,
            confidence=max(record.confidence, 0.8),
            created_at=record.created_at,
            updated_at=now,
            expires_at=record.expires_at,
            metadata=record.metadata,
            validated_by=validated_by,
            validation_evidence=evidence,
        )
        self._records[record_id] = updated
        return True

    def mark_simulation(self, record_id: str) -> bool:
        """Marca explicitamente um registro como saída de simulação."""
        if self._store is not None:
            node = self._store.get_node(record_id)
            if node is None or node.node_type != "knowledge_record":
                return False
            return self._store.mark_node_as_simulation(record_id)
        record = self._records.get(record_id)
        if record is None:
            return False
        if record.kind == KnowledgeKind.FACT and record.validated_by:
            # Não rebaixa fatos validados.
            return False
        now = datetime.now(timezone.utc).isoformat()
        updated = record.__class__(
            record_id=record.record_id,
            kind=KnowledgeKind.SIMULATION,
            content=record.content,
            source=KnowledgeSource.SIMULATION_OUTPUT,
            source_ref=record.source_ref,
            confidence=min(record.confidence, 0.6),
            created_at=record.created_at,
            updated_at=now,
            expires_at=record.expires_at,
            metadata={**record.metadata, "flagged_as_simulation": True},
            validated_by=record.validated_by,
            validation_evidence=record.validation_evidence,
        )
        self._records[record_id] = updated
        return True

    def delete(self, record_id: str) -> bool:
        if self._store is not None:
            node = self._store.get_node(record_id)
            if node is None or node.node_type != "knowledge_record":
                return False
            return self._store.delete_node(record_id)
        if record_id in self._records:
            del self._records[record_id]
            return True
        return False

    def stats(self) -> dict[str, int]:
        if self._store is not None:
            node_stats = self._store.node_stats(node_type="knowledge_record")
            counts: dict[str, int] = {
                kind.name: int(node_stats["by_kind"].get(kind.name, 0))
                for kind in KnowledgeKind
            }
            counts["total"] = int(node_stats["nodes"])
            return counts
        counts = {k.name: 0 for k in KnowledgeKind}
        for record in self._records.values():
            counts[record.kind.name] = counts.get(record.kind.name, 0) + 1
        counts["total"] = len(self._records)
        return counts


__all__ = [
    "KnowledgeEngine",
    "KnowledgeKind",
    "KnowledgeRecord",
    "KnowledgeSource",
]
