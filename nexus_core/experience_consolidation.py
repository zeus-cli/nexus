"""Experience Consolidation Engine — consolidação de experiências entre nós.

Responsabilidade:
* Consolidar aprendizados e experiências validadas de múltiplos nós.
* Promover apenas experiências validadas para conhecimento reutilizável.
* Detectar padrões recorrentes entre execuções de tarefas.
* Manter provedência (origem) de cada experiência consolidada.

Princípios:
* Apenas experiências validadas (não simuladas) se tornam conhecimento reutilizável.
* Cada experiência identifica sua origem (nó, tarefa, usuário).
* Deduplicação de conhecimentos similares.
* Controle de qualidade: confiança mínima para consolidação.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping

class ExperienceType(Enum):
    TASK_SUCCESS = auto()      # Tarefa executada com sucesso
    TASK_FAILURE = auto()      # Tarefa falhou (lição aprendida)
    PLAN_SUCCESS = auto()      # Plano executado com sucesso
    PLAN_FAILURE = auto()      # Plano falhou
    SIMULATION_INSIGHT = auto()  # Insight de simulação (labelado como simulado)
    SECURITY_EVENT = auto()    # Evento de segurança aprendido


class ExperienceStatus(Enum):
    PROPOSED = auto()          # Proposta, aguardando validação
    VALIDATED = auto()         # Validada, pode ser reutilizada
    REJECTED = auto()          # Rejeitada
    DEPRECATED = auto()        # Obsoleta


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    """Uma experiência ou lição aprendida."""

    record_id: str
    experience_type: ExperienceType
    title: str
    description: str
    source_node_id: str
    source_task_id: str | None
    context: dict[str, Any]  # contexto em que ocorreu
    outcome: dict[str, Any]  # resultado observado
    confidence: float  # 0.0 a 1.0
    status: ExperienceStatus = ExperienceStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    validated_at: float | None = None
    validated_by: str | None = None
    reuse_count: int = 0
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "experience_type": self.experience_type.name,
            "title": self.title,
            "description": self.description,
            "source_node_id": self.source_node_id,
            "source_task_id": self.source_task_id,
            "context": dict(self.context),
            "outcome": dict(self.outcome),
            "confidence": self.confidence,
            "status": self.status.name,
            "created_at": self.created_at,
            "validated_at": self.validated_at,
            "validated_by": self.validated_by,
            "reuse_count": self.reuse_count,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }

    def is_reusable(self) -> bool:
        return (
            self.status == ExperienceStatus.VALIDATED
            and self.confidence >= 0.7
        )


@dataclass(frozen=True, slots=True)
class ConsolidationProposal:
    """Proposta de consolidação gerada a partir de padrões."""

    proposal_id: str
    suggested_title: str
    suggested_description: str
    source_records: tuple[str, ...]
    confidence: float
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExperienceConsolidationEngine:
    """Consolida experiências de múltiplos nós em conhecimento reutilizável."""

    def __init__(
        self,
        *,
        min_confidence_for_validation: float = 0.7,
        min_recurrence_for_pattern: int = 3,
    ) -> None:
        self._records: dict[str, ExperienceRecord] = {}
        self._proposals: dict[str, ConsolidationProposal] = {}
        self._min_confidence = min_confidence_for_validation
        self._min_recurrence = min_recurrence_for_pattern

    def add_experience(
        self,
        *,
        experience_type: ExperienceType,
        title: str,
        description: str,
        source_node_id: str,
        source_task_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        outcome: Mapping[str, Any] | None = None,
        confidence: float = 0.5,
        tags: tuple[str, ...] = (),
    ) -> str:
        """Registra uma nova experiência (não validada inicialmente)."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence deve estar entre 0.0 e 1.0")
        record_id = uuid.uuid4().hex
        record = ExperienceRecord(
            record_id=record_id,
            experience_type=experience_type,
            title=title,
            description=description,
            source_node_id=source_node_id,
            source_task_id=source_task_id,
            context=dict(context or {}),
            outcome=dict(outcome or {}),
            confidence=confidence,
            tags=tags,
        )
        self._records[record_id] = record
        return record_id

    def get_record(self, record_id: str) -> ExperienceRecord | None:
        return self._records.get(record_id)

    def list_records(
        self,
        *,
        experience_type: ExperienceType | None = None,
        status: ExperienceStatus | None = None,
        source_node_id: str | None = None,
        reusable_only: bool = False,
        limit: int = 50,
    ) -> list[ExperienceRecord]:
        result = []
        for record in self._records.values():
            if experience_type is not None and record.experience_type != experience_type:
                continue
            if status is not None and record.status != status:
                continue
            if source_node_id is not None and record.source_node_id != source_node_id:
                continue
            if reusable_only and not record.is_reusable():
                continue
            result.append(record)
        # Ordena por confiança desc, depois por criação.
        result.sort(key=lambda r: (r.confidence, r.created_at), reverse=True)
        return result[:limit]

    def validate_experience(
        self,
        record_id: str,
        *,
        validated_by: str,
    ) -> bool:
        """Promove uma experiência para validada (reutilizável)."""
        record = self._records.get(record_id)
        if record is None:
            return False
        if record.status != ExperienceStatus.PROPOSED:
            return False
        if record.confidence < self._min_confidence:
            return False
        now = time.time()
        updated = ExperienceRecord(
            record_id=record.record_id,
            experience_type=record.experience_type,
            title=record.title,
            description=record.description,
            source_node_id=record.source_node_id,
            source_task_id=record.source_task_id,
            context=record.context,
            outcome=record.outcome,
            confidence=record.confidence,
            status=ExperienceStatus.VALIDATED,
            created_at=record.created_at,
            validated_at=now,
            validated_by=validated_by,
            reuse_count=record.reuse_count,
            tags=record.tags,
            metadata=record.metadata,
        )
        self._records[record_id] = updated
        return True

    def reject_experience(
        self,
        record_id: str,
        *,
        validated_by: str,
    ) -> bool:
        """Rejeita uma experiência (não será reutilizada)."""
        record = self._records.get(record_id)
        if record is None:
            return False
        if record.status != ExperienceStatus.PROPOSED:
            return False
        now = time.time()
        updated = ExperienceRecord(
            record_id=record.record_id,
            experience_type=record.experience_type,
            title=record.title,
            description=record.description,
            source_node_id=record.source_node_id,
            source_task_id=record.source_task_id,
            context=record.context,
            outcome=record.outcome,
            confidence=record.confidence,
            status=ExperienceStatus.REJECTED,
            created_at=record.created_at,
            validated_at=now,
            validated_by=validated_by,
            reuse_count=record.reuse_count,
            tags=record.tags,
            metadata=record.metadata,
        )
        self._records[record_id] = updated
        return True

    def reuse_experience(self, record_id: str) -> bool:
        """Incrementa o contador de reuso de uma experiência validada."""
        record = self._records.get(record_id)
        if record is None or not record.is_reusable():
            return False
        updated = ExperienceRecord(
            record_id=record.record_id,
            experience_type=record.experience_type,
            title=record.title,
            description=record.description,
            source_node_id=record.source_node_id,
            source_task_id=record.source_task_id,
            context=record.context,
            outcome=record.outcome,
            confidence=record.confidence,
            status=record.status,
            created_at=record.created_at,
            validated_at=record.validated_at,
            validated_by=record.validated_by,
            reuse_count=record.reuse_count + 1,
            tags=record.tags,
            metadata=record.metadata,
        )
        self._records[record_id] = updated
        return True

    def propose_consolidation(
        self,
        *,
        suggested_title: str,
        suggested_description: str,
        source_record_ids: tuple[str, ...],
        confidence: float,
    ) -> str:
        """Cria uma proposta de consolidação a partir de registros relacionados."""
        proposal_id = uuid.uuid4().hex
        proposal = ConsolidationProposal(
            proposal_id=proposal_id,
            suggested_title=suggested_title,
            suggested_description=suggested_description,
            source_records=source_record_ids,
            confidence=confidence,
        )
        self._proposals[proposal_id] = proposal
        return proposal_id

    def list_proposals(self) -> list[ConsolidationProposal]:
        return list(self._proposals.values())

    def detect_patterns(self) -> list[ConsolidationProposal]:
        """Detecta padrões recorrentes entre experiências validadas."""
        proposals: list[ConsolidationProposal] = []
        # Agrupa por tipo de experiência e tags similares.
        groups: dict[str, list[ExperienceRecord]] = {}
        for record in self._records.values():
            if record.status != ExperienceStatus.VALIDATED:
                continue
            for tag in record.tags:
                groups.setdefault(tag, []).append(record)

        for tag, records in groups.items():
            if len(records) < self._min_recurrence:
                continue
            avg_confidence = sum(r.confidence for r in records) / len(records)
            proposal_id = uuid.uuid4().hex
            proposal = ConsolidationProposal(
                proposal_id=proposal_id,
                suggested_title=f"Padrão recorrente: {tag}",
                suggested_description=(
                    f"Identificado em {len(records)} experiências validadas "
                    f"com tag '{tag}'. Confiança média: {avg_confidence:.2f}."
                ),
                source_records=tuple(r.record_id for r in records),
                confidence=avg_confidence,
            )
            proposals.append(proposal)
            self._proposals[proposal_id] = proposal
        return proposals

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        reusable = 0
        for record in self._records.values():
            by_type[record.experience_type.name] = by_type.get(record.experience_type.name, 0) + 1
            by_status[record.status.name] = by_status.get(record.status.name, 0) + 1
            if record.is_reusable():
                reusable += 1
        return {
            "total_records": len(self._records),
            "by_type": by_type,
            "by_status": by_status,
            "reusable": reusable,
            "proposals": len(self._proposals),
        }


__all__ = [
    "ExperienceConsolidationEngine",
    "ExperienceRecord",
    "ConsolidationProposal",
    "ExperienceType",
    "ExperienceStatus",
]
