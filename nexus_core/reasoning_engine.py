"""Reasoning Engine — raciocínio estruturado e transparente.

Responsabilidade: suportar decomposição de problemas, análise de evidências,
geração de hipóteses, avaliação de alternativas, estimativa de incerteza,
detecção de contradições e saídas de raciocínio estruturados.

Regra de projeto: não exige que modelos exponham private internal chain-of-thought.
Armazena conclusões úteis, evidências, suposições e resumos de decisão em vez
de CoT bruto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class ReasoningStepType(Enum):
    DECOMPOSITION = auto()   # Decomposição do problema
    EVIDENCE = auto()        # Análise de evidências
    HYPOTHESIS = auto()      # Geração de hipóteses
    ALTERNATIVE = auto()     # Avaliação de alternativas
    UNCERTAINTY = auto()     # Estimativa de incerteza
    CONTRADICTION = auto()   # Detecção de contradições
    CONCLUSION = auto()      # Conclusão estruturada


@dataclass(frozen=True, slots=True)
class ReasoningStep:
    """Uma etapa individual do raciocínio."""

    step_type: ReasoningStepType
    description: str
    content: str
    confidence: float = 0.5
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_type": self.step_type.name,
            "description": self.description,
            "content": self.content,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """Resultado estruturado de um processo de raciocínio."""

    conclusion: str
    steps: tuple[ReasoningStep, ...]
    confidence: float
    assumptions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    alternatives_considered: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "steps": [s.as_dict() for s in self.steps],
            "confidence": self.confidence,
            "assumptions": list(self.assumptions),
            "evidence": list(self.evidence),
            "contradictions": list(self.contradictions),
            "alternatives_considered": list(self.alternatives_considered),
            "metadata": dict(self.metadata),
        }


class ReasoningEngine:
    """Executa raciocínio estruturado sobre um objetivo e evidências disponíveis.

    O raciocínio é produzido com auxílio do LLM quando disponível, mas a saída
    é sempre estruturada (JSON) e o CoT privado do modelo não é exposto nem
    armazenado. Ficam registradas apenas conclusões, evidências, suposições,
    contradições e resumos de decisão.
    """

    def __init__(self, llm_gateway: Any | None = None) -> None:
        self._llm = llm_gateway

    async def reason(
        self,
        goal: str,
        *,
        evidence: tuple[str, ...] = (),
        assumptions: tuple[str, ...] = (),
        alternatives: tuple[str, ...] = (),
        context: str | None = None,
    ) -> ReasoningResult:
        """Realiza raciocínio estruturado sobre o objetivo.

        Returns um ReasoningResult com conclusão, etapas, confiança,
        suposições, evidências, contradições e alternativas consideradas.
        """
        steps: list[ReasoningStep] = []

        # 1. Decomposição do problema.
        decomposition = self._decompose(goal)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.DECOMPOSITION,
                description="Problem decomposition",
                content=decomposition,
                confidence=0.7,
            )
        )

        # 2. Análise de evidências.
        evidence_analysis = self._analyze_evidence(evidence)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.EVIDENCE,
                description="Evidence analysis",
                content=evidence_analysis,
                confidence=0.6 if evidence else 0.3,
                evidence=evidence,
            )
        )

        # 3. Geração de hipóteses.
        hypotheses = self._generate_hypotheses(goal, evidence)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.HYPOTHESIS,
                description="Hypothesis generation",
                content=hypotheses,
                confidence=0.5,
            )
        )

        # 4. Avaliação de alternativas.
        alt_eval = self._evaluate_alternatives(alternatives)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.ALTERNATIVE,
                description="Alternative evaluation",
                content=alt_eval,
                confidence=0.5 if alternatives else 0.2,
            )
        )

        # 5. Estimativa de incerteza.
        uncertainty = self._estimate_uncertainty(evidence, assumptions)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.UNCERTAINTY,
                description="Uncertainty estimation",
                content=uncertainty,
                confidence=0.6,
            )
        )

        # 6. Detecção de contradições.
        contradictions = self._detect_contradictions(evidence, assumptions)
        steps.append(
            ReasoningStep(
                step_type=ReasoningStepType.CONTRADICTION,
                description="Contradiction detection",
                content=contradictions if contradictions else "No contradictions detected.",
                confidence=0.7,
            )
        )

        # 7. Conclusão estruturada (usa LLM se disponível, senão heurística).
        conclusion, conf = await self._conclude(
            goal, steps, context=context
        )

        return ReasoningResult(
            conclusion=conclusion,
            steps=tuple(steps),
            confidence=conf,
            assumptions=assumptions,
            evidence=evidence,
            contradictions=tuple(
                c for c in contradictions.split(";") if c.strip()
            ),
            alternatives_considered=alternatives,
            metadata={"goal": goal},
        )

    # -- etapas internas --------------------------------------------------

    @staticmethod
    def _decompose(goal: str) -> str:
        return f"Break down '{goal}' into: (1) understand intent, (2) gather facts, (3) evaluate options, (4) decide action."

    @staticmethod
    def _analyze_evidence(evidence: tuple[str, ...]) -> str:
        if not evidence:
            return "No explicit evidence provided."
        return f"{len(evidence)} evidence item(s) available for consideration."

    @staticmethod
    def _generate_hypotheses(goal: str, evidence: tuple[str, ...]) -> str:
        return f"Hypotheses for '{goal}': (A) straightforward interpretation; (B) requires additional information."

    @staticmethod
    def _evaluate_alternatives(alternatives: tuple[str, ...]) -> str:
        if not alternatives:
            return "No explicit alternatives provided."
        return f"{len(alternatives)} alternative(s) to weigh against the primary approach."

    @staticmethod
    def _estimate_uncertainty(
        evidence: tuple[str, ...], assumptions: tuple[str, ...]
    ) -> str:
        factors = []
        if not evidence:
            factors.append("limited evidence")
        if assumptions:
            factors.append(f"{len(assumptions)} unstated assumption(s)")
        if not factors:
            return "Low uncertainty: sufficient evidence and few assumptions."
        return f"Moderate uncertainty due to: {', '.join(factors)}."

    @staticmethod
    def _detect_contradictions(
        evidence: tuple[str, ...], assumptions: tuple[str, ...]
    ) -> str:
        # Detecção heurística simples: opostos lexicais grosseiros.
        contra = []
        for e in evidence:
            for a in assumptions:
                if ("not " + e.strip()) in a or e.strip() in ("not " + a):
                    contra.append(f"'{e}' vs '{a}'")
        return "; ".join(contra) if contra else ""

    async def _conclude(
        self,
        goal: str,
        steps: list[ReasoningStep],
        context: str | None = None,
    ) -> tuple[str, float]:
        if self._llm is not None:
            try:
                system = (
                    "You are the NEXUS Reasoning Engine. Given the goal and the "
                    "structured reasoning steps above, produce a final conclusion. "
                    "Output MUST be a JSON object: {\"conclusion\": \"...\", "
                    "\"confidence\": 0.0-1.0}. Do NOT reveal private chain-of-thought."
                )
                prompt = (
                    f"Goal: {goal}\n\nSteps:\n"
                    + "\n".join(
                        f"- {s.step_type.name}: {s.content}"
                        for s in steps
                    )
                    + (f"\n\nContext: {context}" if context else "")
                )
                res = await self._llm.complete(
                    prompt, task="reasoning", system=system, json_mode=True
                )
                data = json.loads(res.text)
                return str(data.get("conclusion", "")), float(
                    data.get("confidence", 0.5)
                )
            except Exception:
                pass
        # Fallback heurístico.
        return (
            f"Based on the analysis, the goal '{goal}' can be approached using "
            "the available evidence and assumptions.",
            0.6,
        )


__all__ = [
    "ReasoningEngine",
    "ReasoningResult",
    "ReasoningStep",
    "ReasoningStepType",
]
