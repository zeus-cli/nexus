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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Mapping


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


class KnowledgeEngine:
    """Gerencia registros de conhecimento com proveniência."""

    def __init__(self) -> None:
        self._records: dict[str, KnowledgeRecord] = {}

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
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence deve estar entre 0.0 e 1.0")
        record_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        record = KnowledgeRecord(
            record_id=record_id,
            kind=kind,
            content=content,
            source=source,
            source_ref=source_ref,
            confidence=confidence,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )
        self._records[record_id] = record
        return record

    def get(self, record_id: str) -> KnowledgeRecord | None:
        return self._records.get(record_id)

    def list(
        self,
        *,
        kind: KnowledgeKind | None = None,
        source: KnowledgeSource | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> list[KnowledgeRecord]:
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
        terms = set(t for t in re.findall(r"[\w-]+", query.lower()) if len(t) > 2)
        scored = []
        for record in self._records.values():
            if record.is_expired():
                continue
            content_terms = set(
                t for t in re.findall(r"[\w-]+", record.content.lower()) if len(t) > 2
            )
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

        Apenas registros com kind ASSUMPTION ou HYPOTHESIS podem ser promovidos.
        Registros SIMULATION nunca são promovidos automaticamente a fato.
        """
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
        if record_id in self._records:
            del self._records[record_id]
            return True
        return False

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {k.name: 0 for k in KnowledgeKind}
        for record in self._records.values():
            counts[record.kind.name] = counts.get(record.kind.name, 0) + 1
        counts["total"] = len(self._records)
        return counts


__all__ = [
    "KnowledgeEngine",
    "KnowledgeRecord",
    "KnowledgeKind",
    "KnowledgeSource",
]
