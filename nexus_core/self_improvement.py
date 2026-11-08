"""Controlled Self-Improvement — autoaperfeiçoamento governado do sistema.

Responsabilidade:
* Propor melhorias baseadas em experiências validadas e consolidadas.
* Exigir aprovação humana explícita antes de aplicar mudanças críticas.
* Versionar mudanças para permitir rollback.
* Nunca modificar políticas de segurança automaticamente.

Princípios:
* Melhorias são propostas, não aplicadas automaticamente.
* Mudanças de política de segurança exigem aprovação humana (never automatic).
* Cada mudança é versionada e reversível.
* Experiência consolidada é a base, não a única fonte, de propostas.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class ImprovementType(Enum):
    RESOURCE_TUNING = auto()       # Ajuste de limites de recurso
    POLICY_ADJUSTMENT = auto()     # Ajuste de política (não de segurança)
    WORKFLOW_OPTIMIZATION = auto() # Otimização de fluxo de trabalho
    AGENT_CONFIG = auto()          # Ajuste de configuração de agente
    SECURITY_POLICY = auto()       # Mudança de política de segurança (requer aprovação)


class ImprovementStatus(Enum):
    PROPOSED = auto()          # Proposta, aguardando revisão
    UNDER_REVIEW = auto()      # Em revisão
    APPROVED = auto()          # Aprovada para aplicação
    REJECTED = auto()          # Rejeitada
    APPLIED = auto()           # Aplicada com sucesso
    ROLLED_BACK = auto()       # Revertida
    FAILED = auto()            # Falha ao aplicar


@dataclass(frozen=True, slots=True)
class ImprovementProposal:
    """Uma proposta de autoaperfeiçoamento."""

    proposal_id: str
    improvement_type: ImprovementType
    title: str
    description: str
    rationale: str
    proposed_changes: dict[str, Any]  # mudanças específicas a aplicar
    affected_components: tuple[str, ...]
    risk_level: str  # 'low', 'medium', 'high', 'critical'
    requires_human_approval: bool
    source_experience_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    decided_by: str | None = None
    status: ImprovementStatus = ImprovementStatus.PROPOSED
    applied_at: float | None = None
    rollback_plan: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "improvement_type": self.improvement_type.name,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "proposed_changes": dict(self.proposed_changes),
            "affected_components": list(self.affected_components),
            "risk_level": self.risk_level,
            "requires_human_approval": self.requires_human_approval,
            "source_experience_ids": list(self.source_experience_ids),
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "status": self.status.name,
            "applied_at": self.applied_at,
            "rollback_plan": dict(self.rollback_plan) if self.rollback_plan else None,
            "metadata": dict(self.metadata),
        }


class SelfImprovementEngine:
    """Gerencia propostas de autoaperfeiçoamento com governança."""

    def __init__(self) -> None:
        self._proposals: dict[str, ImprovementProposal] = {}
        self._security_policy_frozen = True  # políticas de segurança nunca mudam automaticamente

    def propose_improvement(
        self,
        *,
        improvement_type: ImprovementType,
        title: str,
        description: str,
        rationale: str,
        proposed_changes: Mapping[str, Any],
        affected_components: tuple[str, ...],
        risk_level: str = "medium",
        source_experience_ids: tuple[str, ...] = (),
    ) -> str:
        """Cria uma proposta de melhoria."""
        proposal_id = uuid.uuid4().hex
        # Mudanças de política de segurança SEMPRE exigem aprovação humana.
        requires_human = (
            improvement_type == ImprovementType.SECURITY_POLICY
            or risk_level in ("high", "critical")
        )
        proposal = ImprovementProposal(
            proposal_id=proposal_id,
            improvement_type=improvement_type,
            title=title,
            description=description,
            rationale=rationale,
            proposed_changes=dict(proposed_changes),
            affected_components=affected_components,
            risk_level=risk_level,
            requires_human_approval=requires_human,
            source_experience_ids=source_experience_ids,
        )
        self._proposals[proposal_id] = proposal
        return proposal_id

    def get_proposal(self, proposal_id: str) -> ImprovementProposal | None:
        return self._proposals.get(proposal_id)

    def list_proposals(
        self,
        *,
        status: ImprovementStatus | None = None,
        improvement_type: ImprovementType | None = None,
    ) -> list[ImprovementProposal]:
        result = []
        for proposal in self._proposals.values():
            if status is not None and proposal.status != status:
                continue
            if improvement_type is not None and proposal.improvement_type != improvement_type:
                continue
            result.append(proposal)
        result.sort(key=lambda p: p.created_at, reverse=True)
        return result

    def submit_for_review(self, proposal_id: str, reviewer: str) -> bool:
        """Submete uma proposta para revisão."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.status != ImprovementStatus.PROPOSED:
            return False
        updated = ImprovementProposal(
            proposal_id=proposal.proposal_id,
            improvement_type=proposal.improvement_type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            proposed_changes=proposal.proposed_changes,
            affected_components=proposal.affected_components,
            risk_level=proposal.risk_level,
            requires_human_approval=proposal.requires_human_approval,
            source_experience_ids=proposal.source_experience_ids,
            created_at=proposal.created_at,
            decided_at=None,
            decided_by=None,
            status=ImprovementStatus.UNDER_REVIEW,
            applied_at=proposal.applied_at,
            rollback_plan=proposal.rollback_plan,
            metadata={**proposal.metadata, "reviewer": reviewer},
        )
        self._proposals[proposal_id] = updated
        return True

    def approve(
        self,
        proposal_id: str,
        *,
        approver: str,
        rollback_plan: Mapping[str, Any] | None = None,
    ) -> bool:
        """Aprova uma proposta para aplicação."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            return False
        if proposal.status != ImprovementStatus.UNDER_REVIEW:
            return False
        if proposal.requires_human_approval and approver is None:
            return False  # requer aprovação humana explícita
        now = time.time()
        updated = ImprovementProposal(
            proposal_id=proposal.proposal_id,
            improvement_type=proposal.improvement_type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            proposed_changes=proposal.proposed_changes,
            affected_components=proposal.affected_components,
            risk_level=proposal.risk_level,
            requires_human_approval=proposal.requires_human_approval,
            source_experience_ids=proposal.source_experience_ids,
            created_at=proposal.created_at,
            decided_at=now,
            decided_by=approver,
            status=ImprovementStatus.APPROVED,
            applied_at=None,
            rollback_plan=dict(rollback_plan) if rollback_plan else None,
            metadata=proposal.metadata,
        )
        self._proposals[proposal_id] = updated
        return True

    def reject(self, proposal_id: str, *, rejector: str) -> bool:
        """Rejeita uma proposta."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.status not in (
            ImprovementStatus.PROPOSED,
            ImprovementStatus.UNDER_REVIEW,
        ):
            return False
        now = time.time()
        updated = ImprovementProposal(
            proposal_id=proposal.proposal_id,
            improvement_type=proposal.improvement_type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            proposed_changes=proposal.proposed_changes,
            affected_components=proposal.affected_components,
            risk_level=proposal.risk_level,
            requires_human_approval=proposal.requires_human_approval,
            source_experience_ids=proposal.source_experience_ids,
            created_at=proposal.created_at,
            decided_at=now,
            decided_by=rejector,
            status=ImprovementStatus.REJECTED,
            applied_at=None,
            rollback_plan=None,
            metadata={**proposal.metadata, "rejected_by": rejector},
        )
        self._proposals[proposal_id] = updated
        return True

    def mark_applied(self, proposal_id: str) -> bool:
        """Marca uma proposta aprovada como aplicada."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.status != ImprovementStatus.APPROVED:
            return False
        now = time.time()
        updated = ImprovementProposal(
            proposal_id=proposal.proposal_id,
            improvement_type=proposal.improvement_type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            proposed_changes=proposal.proposed_changes,
            affected_components=proposal.affected_components,
            risk_level=proposal.risk_level,
            requires_human_approval=proposal.requires_human_approval,
            source_experience_ids=proposal.source_experience_ids,
            created_at=proposal.created_at,
            decided_at=proposal.decided_at,
            decided_by=proposal.decided_by,
            status=ImprovementStatus.APPLIED,
            applied_at=now,
            rollback_plan=proposal.rollback_plan,
            metadata=proposal.metadata,
        )
        self._proposals[proposal_id] = updated
        return True

    def rollback(self, proposal_id: str, *, rolled_back_by: str) -> bool:
        """Reverte uma melhoria aplicada."""
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.status != ImprovementStatus.APPLIED:
            return False
        if proposal.rollback_plan is None:
            return False  # não há plano de reversão
        now = time.time()
        updated = ImprovementProposal(
            proposal_id=proposal.proposal_id,
            improvement_type=proposal.improvement_type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            proposed_changes=proposal.proposed_changes,
            affected_components=proposal.affected_components,
            risk_level=proposal.risk_level,
            requires_human_approval=proposal.requires_human_approval,
            source_experience_ids=proposal.source_experience_ids,
            created_at=proposal.created_at,
            decided_at=proposal.decided_at,
            decided_by=proposal.decided_by,
            status=ImprovementStatus.ROLLED_BACK,
            applied_at=proposal.applied_at,
            rollback_plan=proposal.rollback_plan,
            metadata={**proposal.metadata, "rolled_back_by": rolled_back_by, "rolled_back_at": now},
        )
        self._proposals[proposal_id] = updated
        return True

    def freeze_security_policies(self, frozen: bool = True) -> None:
        """Congela ou descongela políticas de segurança (não afeta propostas existentes)."""
        self._security_policy_frozen = frozen

    def security_policies_frozen(self) -> bool:
        return self._security_policy_frozen

    def stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for p in self._proposals.values():
            by_status[p.status.name] = by_status.get(p.status.name, 0) + 1
            by_type[p.improvement_type.name] = by_type.get(p.improvement_type.name, 0) + 1
        return {
            "total_proposals": len(self._proposals),
            "by_status": by_status,
            "by_type": by_type,
            "security_policies_frozen": self._security_policy_frozen,
        }


__all__ = [
    "SelfImprovementEngine",
    "ImprovementProposal",
    "ImprovementType",
    "ImprovementStatus",
]
