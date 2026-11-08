"""Adversarial Review — revisão crítica de decisões importantes.

Responsabilidade: permitir que agentes críticos revisem decisões tomadas por
outros agentes, identificando falhas, vieses e alternativas não consideradas.

Princípio: agentes podem discordar; consenso não equivale a correção. Uma
decisão só é considerada sólida após sobreviver à revisão adversária.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class ReviewVerdict(Enum):
    APPROVED = auto()      # Decisão aprovada pelo revisor
    REJECTED = auto()      # Decisão rejeitada
    NEEDS_REVISION = auto()  # Decisão precisa de revisão
    INCONCLUSIVE = auto()  # Revisor não conseguiu concluir


@dataclass(frozen=True, slots=True)
class ReviewRound:
    """Uma rodada de revisão adversária."""

    round_id: str
    decision_id: str
    reviewer_agent_id: str
    verdict: ReviewVerdict
    reasoning: str
    concerns: tuple[str, ...]
    suggested_alternatives: tuple[str, ...]
    confidence: float
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "decision_id": self.decision_id,
            "reviewer_agent_id": self.reviewer_agent_id,
            "verdict": self.verdict.name,
            "reasoning": self.reasoning,
            "concerns": list(self.concerns),
            "suggested_alternatives": list(self.suggested_alternatives),
            "confidence": self.confidence,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class AdversarialReviewResult:
    """Resultado completo da revisão adversária de uma decisão."""

    decision_id: str
    rounds: tuple[ReviewRound, ...]
    final_verdict: ReviewVerdict
    consensus_reached: bool
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "rounds": [r.as_dict() for r in self.rounds],
            "final_verdict": self.final_verdict.name,
            "consensus_reached": self.consensus_reached,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


class AdversarialReview:
    """Coordena revisões adversárias de decisões importantes."""

    def __init__(self) -> None:
        self._reviews: dict[str, AdversarialReviewResult] = {}

    def request_review(
        self,
        *,
        decision_id: str,
        decision_summary: str,
        decision_maker_agent_id: str,
        reviewer_agent_ids: tuple[str, ...],
    ) -> str:
        """Inicia uma revisão adversária de uma decisão.

        Cada revisor analisa independentemente e pode discordar do tomador
        da decisão. O resultado final é a agregação dos vereditos.
        """
        import time

        rounds: list[ReviewRound] = []
        for reviewer_id in reviewer_agent_ids:
            round_id = f"{decision_id}-rev-{reviewer_id[:8]}"
            # No modo automático, o revisor avalia com base no resumo.
            # Em produção, o revisor seria um agente LLM real.
            verdict, reasoning, concerns = self._automatic_review(
                reviewer_id, decision_summary, decision_maker_agent_id
            )
            rounds.append(
                ReviewRound(
                    round_id=round_id,
                    decision_id=decision_id,
                    reviewer_agent_id=reviewer_id,
                    verdict=verdict,
                    reasoning=reasoning,
                    concerns=concerns,
                    suggested_alternatives=(),
                    confidence=0.6,
                    created_at=time.monotonic(),
                )
            )

        final_verdict, consensus, summary = self._aggregate(rounds)

        result = AdversarialReviewResult(
            decision_id=decision_id,
            rounds=tuple(rounds),
            final_verdict=final_verdict,
            consensus_reached=consensus,
            summary=summary,
            metadata={
                "decision_maker": decision_maker_agent_id,
                "reviewers": list(reviewer_agent_ids),
            },
        )
        self._reviews[decision_id] = result
        return decision_id

    def _automatic_review(
        self,
        reviewer_id: str,
        decision_summary: str,
        decision_maker_id: str,
    ) -> tuple[ReviewVerdict, str, tuple[str, ...]]:
        """Revisão automática heurística (substituída por agente LLM real)."""
        lower = decision_summary.lower()
        concerns: list[str] = []
        if "critical" in lower or "irreversible" in lower:
            concerns.append("operation may be irreversible; requires stronger validation")
        if "delete" in lower or "remove" in lower:
            concerns.append("destructive operation detected")
        if reviewer_id == decision_maker_id:
            return (
                ReviewVerdict.INCONCLUSIVE,
                "reviewer is the same as decision maker; cannot self-review",
                ("self-review not permitted",),
            )
        if concerns:
            return (
                ReviewVerdict.NEEDS_REVISION,
                f"reviewer {reviewer_id} identified concerns with decision by {decision_maker_id}",
                tuple(concerns),
            )
        return (
            ReviewVerdict.APPROVED,
            f"reviewer {reviewer_id} found no concerns with decision by {decision_maker_id}",
            (),
        )

    def _aggregate(
        self, rounds: tuple[ReviewRound, ...]
    ) -> tuple[ReviewVerdict, bool, str]:
        """Agrega vereditos de múltiplos revisores."""
        if not rounds:
            return ReviewVerdict.INCONCLUSIVE, False, "no reviewers"

        verdicts = [r.verdict for r in rounds]
        approved = sum(1 for v in verdicts if v == ReviewVerdict.APPROVED)
        rejected = sum(1 for v in verdicts if v == ReviewVerdict.REJECTED)
        needs_revision = sum(
            1 for v in verdicts if v == ReviewVerdict.NEEDS_REVISION
        )
        inconclusive = sum(1 for v in verdicts if v == ReviewVerdict.INCONCLUSIVE)

        # Consenso: todos aprovam ou todos rejeitam.
        total = len(verdicts)
        consensus = (approved == total) or (rejected == total)

        if approved == total:
            final = ReviewVerdict.APPROVED
            summary = f"all {total} reviewers approved the decision"
        elif rejected == total:
            final = ReviewVerdict.REJECTED
            summary = f"all {total} reviewers rejected the decision"
        elif needs_revision > 0:
            final = ReviewVerdict.NEEDS_REVISION
            summary = f"{needs_revision} reviewer(s) requested revision; {approved} approved"
        else:
            final = ReviewVerdict.INCONCLUSIVE
            summary = f"reviewers could not reach a conclusion ({inconclusive} inconclusive)"

        return final, consensus, summary

    def get_review(self, decision_id: str) -> AdversarialReviewResult | None:
        return self._reviews.get(decision_id)

    def list_reviews(self) -> list[AdversarialReviewResult]:
        return list(self._reviews.values())


__all__ = [
    "AdversarialReview",
    "ReviewRound",
    "AdversarialReviewResult",
    "ReviewVerdict",
]
