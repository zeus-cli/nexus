"""Decision Engine — comparação de decisões candidatas.

Responsabilidade: comparar decisões candidatas de acordo com:

* resultado esperado
* qualidade da evidência
* risco operacional
* consumo de recursos
* reversibilidade
* restrições
* confiança
* requisitos de aprovação humana

Saída: a decisão recomendada com pontuação, justificativa e nível de aprovação.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class ApprovalRequirement(Enum):
    NONE = auto()           # Automático dentro do escopo aprovado
    POLICY = auto()         # Autorização dependente de política
    HUMAN = auto()          # Aprovação humana obrigatória
    DENIED = auto()         # Negado por padrão


@dataclass(frozen=True, slots=True)
class DecisionOption:
    """Uma decisão candidata."""

    option_id: str
    description: str
    expected_outcome: str
    evidence_quality: float  # 0.0 a 1.0
    operational_risk: str    # 'low', 'medium', 'high', 'critical'
    resource_consumption: str  # 'low', 'medium', 'high'
    reversible: bool
    constraints: tuple[str, ...] = ()
    confidence: float = 0.5  # 0.0 a 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "evidence_quality": self.evidence_quality,
            "operational_risk": self.operational_risk,
            "resource_consumption": self.resource_consumption,
            "reversible": self.reversible,
            "constraints": list(self.constraints),
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Resultado da comparação de decisões."""

    recommended_option: DecisionOption
    score: float  # 0.0 a 1.0
    approval_requirement: ApprovalRequirement
    justification: str
    alternatives_evaluated: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recommended_option": self.recommended_option.as_dict(),
            "score": self.score,
            "approval_requirement": self.approval_requirement.name,
            "justification": self.justification,
            "alternatives_evaluated": self.alternatives_evaluated,
            "metadata": dict(self.metadata),
        }


class DecisionEngine:
    """Compara decisões candidatas e recomenda a melhor."""

    # Pesos para cada critério (soma = 1.0).
    _WEIGHTS = {
        "evidence_quality": 0.20,
        "operational_risk": 0.20,
        "reversibility": 0.15,
        "resource_consumption": 0.10,
        "confidence": 0.15,
        "expected_outcome": 0.10,
        "constraints": 0.10,
    }

    _RISK_PENALTY = {
        "low": 0.0,
        "medium": 0.2,
        "high": 0.5,
        "critical": 0.9,
    }

    _RESOURCE_PENALTY = {
        "low": 0.0,
        "medium": 0.15,
        "high": 0.4,
    }

    def __init__(self) -> None:
        pass

    def decide(
        self,
        options: list[DecisionOption],
        *,
        require_human_for: tuple[str, ...] = ("high", "critical"),
    ) -> DecisionResult:
        """Seleciona a melhor opção dentre as candidatas.

        Args:
            options: lista de decisões candidatas (não vazia).
            require_human_for: níveis de risco que exigem aprovação humana.

        Returns:
            DecisionResult com a opção recomendada, pontuação, requisito de
            aprovação e justificativa.
        """
        if not options:
            raise ValueError("pelo menos uma opção é necessária")

        scored = []
        for opt in options:
            score = self._score_option(opt)
            scored.append((opt, score))

        # Ordena por pontuação decrescente.
        scored.sort(key=lambda item: item[1], reverse=True)
        best_opt, best_score = scored[0]

        # Determina requisito de aprovação.
        approval = self._approval_requirement(best_opt, require_human_for)

        # Justificativa.
        justification = self._justify(best_opt, best_score, scored)

        return DecisionResult(
            recommended_option=best_opt,
            score=round(best_score, 3),
            approval_requirement=approval,
            justification=justification,
            alternatives_evaluated=len(options),
            metadata={
                "total_options": len(options),
                "top_score": round(best_score, 3),
            },
        )

    # -- pontuação -------------------------------------------------------

    def _score_option(self, opt: DecisionOption) -> float:
        evidence_score = opt.evidence_quality
        risk_penalty = self._RISK_PENALTY.get(opt.operational_risk, 0.5)
        resource_penalty = self._RESOURCE_PENALTY.get(
            opt.resource_consumption, 0.2
        )
        reversibility_score = 1.0 if opt.reversible else 0.4
        confidence_score = opt.confidence
        outcome_score = 0.8 if opt.expected_outcome else 0.3
        constraints_score = 1.0 if not opt.constraints else 0.7

        score = (
            self._WEIGHTS["evidence_quality"] * evidence_score
            + self._WEIGHTS["operational_risk"] * (1.0 - risk_penalty)
            + self._WEIGHTS["reversibility"] * reversibility_score
            + self._WEIGHTS["resource_consumption"] * (1.0 - resource_penalty)
            + self._WEIGHTS["confidence"] * confidence_score
            + self._WEIGHTS["expected_outcome"] * outcome_score
            + self._WEIGHTS["constraints"] * constraints_score
        )
        return max(0.0, min(1.0, score))

    def _approval_requirement(
        self,
        opt: DecisionOption,
        require_human_for: tuple[str, ...],
    ) -> ApprovalRequirement:
        risk = opt.operational_risk.lower()
        if risk in ("critical",):
            return ApprovalRequirement.DENIED
        if risk in require_human_for:
            return ApprovalRequirement.HUMAN
        if risk in ("medium",):
            return ApprovalRequirement.POLICY
        return ApprovalRequirement.NONE

    @staticmethod
    def _justify(
        best: DecisionOption,
        best_score: float,
        scored: list[tuple[DecisionOption, float]],
    ) -> str:
        parts = [
            f"Selected '{best.description}' (score {best_score:.2f}).",
            f"Evidence quality: {best.evidence_quality:.2f}.",
            f"Risk: {best.operational_risk}.",
            f"Reversible: {best.reversible}.",
            f"Confidence: {best.confidence:.2f}.",
        ]
        if len(scored) > 1:
            runner_up = scored[1]
            parts.append(
                f"Runner-up: '{runner_up[0].description}' "
                f"(score {runner_up[1]:.2f})."
            )
        return " ".join(parts)


__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "DecisionOption",
    "ApprovalRequirement",
]
