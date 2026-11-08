"""Intent Engine — interpretação e classificação de solicitações do usuário.

Responsabilidade: receber um prompt bruto e produzir uma ``Intent`` estruturada
que descreve o objetivo, as restrições, o resultado esperado, a informação
necessária, os riscos potenciais e as capacidades requeridas.

Não depende de LLM para classificação heurística simples; o LLM é usado apenas
quando a intenção não puder ser resolvida por regras determinísticas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class IntentType(Enum):
    """Tipos de intenção reconhecidos pelo motor."""

    UNKNOWN = auto()           # Não foi possível classificar
    QUERY = auto()             # Pergunta / busca de informação
    ANALYSIS = auto()          # Análise de dados ou sistema
    PLANNING = auto()          # Plano de ação multi-etapas
    EXECUTION = auto()         # Execução de uma operação específica
    SIMULATION = auto()        # Execução de cenário simulado
    MEMORY = auto()            # Leitura/gravação de memória
    CONFIGURATION = auto()     # Ajuste de configuração do sistema
    ADMINISTRATION = auto()    # Operação administrativa / de sistema


class RiskLevel(Enum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()


@dataclass(frozen=True, slots=True)
class Intent:
    """Representação estruturada da intenção do usuário."""

    raw_prompt: str
    intent_type: IntentType = IntentType.UNKNOWN
    objective: str = ""
    constraints: tuple[str, ...] = ()
    expected_outcome: str = ""
    required_information: tuple[str, ...] = ()
    potential_risks: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type.name,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "expected_outcome": self.expected_outcome,
            "required_information": list(self.required_information),
            "potential_risks": list(self.potential_risks),
            "required_capabilities": list(self.required_capabilities),
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


class IntentEngine:
    """Interpreta solicitações do usuário e produz uma Intent estruturada."""

    def __init__(self) -> None:
        # Padrões heurísticos para classificação determinística.
        self._execution_keywords = (
            "execute", "run", "start", "stop", "restart", "delete",
            "remove", "create", "write", "modify", "change", "set",
        )
        self._simulation_keywords = (
            "simulate", "scenario", "what if", "model", "forecast",
        )
        self._planning_keywords = (
            "plan", "strategy", "roadmap", "steps to", "how to achieve",
        )
        self._analysis_keywords = (
            "analyze", "analyse", "inspect", "review", "evaluate",
            "assess", "diagnose",
        )
        self._memory_keywords = (
            "remember", "recall", "store", "save memory", "forget",
        )

    async def interpret(
        self,
        prompt: str,
        *,
        context: str | None = None,
    ) -> Intent:
        """Interpreta um prompt e retorna a Intent estruturada.

        Args:
            prompt: texto bruto do usuário.
            context: contexto adicional opcional (memória, estado do sistema).

        Returns:
            Intent com classificação e metadados preenchidos.
        """
        if not prompt or not prompt.strip():
            return Intent(
                raw_prompt=prompt,
                intent_type=IntentType.UNKNOWN,
                confidence=0.0,
            )

        normalized = prompt.strip()
        lower = normalized.lower()

        # 1. Classificação heurística determinística.
        intent_type, confidence = self._classify(lower)

        # 2. Extração de campos estruturados.
        objective = self._extract_objective(normalized, intent_type)
        constraints = self._extract_constraints(lower)
        expected_outcome = self._extract_expected_outcome(intent_type)
        required_information = self._extract_required_information(lower, intent_type)
        potential_risks = self._extract_risks(lower, intent_type)
        required_capabilities = self._extract_capabilities(intent_type)

        return Intent(
            raw_prompt=normalized,
            intent_type=intent_type,
            objective=objective,
            constraints=constraints,
            expected_outcome=expected_outcome,
            required_information=required_information,
            potential_risks=potential_risks,
            required_capabilities=required_capabilities,
            confidence=confidence,
            metadata={"context_provided": context is not None},
        )

    # -- classificação ----------------------------------------------------

    def _classify(self, lower: str) -> tuple[IntentType, float]:
        # Verifica por categoria com pontuação simples.
        scores: dict[IntentType, int] = {
            IntentType.EXECUTION: sum(
                1 for kw in self._execution_keywords if kw in lower
            ),
            IntentType.SIMULATION: sum(
                1 for kw in self._simulation_keywords if kw in lower
            ),
            IntentType.PLANNING: sum(
                1 for kw in self._planning_keywords if kw in lower
            ),
            IntentType.ANALYSIS: sum(
                1 for kw in self._analysis_keywords if kw in lower
            ),
            IntentType.MEMORY: sum(
                1 for kw in self._memory_keywords if kw in lower
            ),
        }

        # Palavras que indicam leitura/pesquisa simples.
        if any(kw in lower for kw in ("what is", "quem é", "qual é", "como")):
            scores[IntentType.QUERY] += 1

        best = max(scores, key=scores.get)
        best_score = scores[best]

        if best_score == 0:
            return IntentType.UNKNOWN, 0.2

        # Confiança baseada na força da correspondência.
        total_keywords = sum(len(v) for v in (
            self._execution_keywords,
            self._simulation_keywords,
            self._planning_keywords,
            self._analysis_keywords,
            self._memory_keywords,
        ))
        confidence = min(0.95, 0.5 + 0.1 * best_score)
        return best, confidence

    # -- extração de campos ----------------------------------------------

    @staticmethod
    def _extract_objective(prompt: str, intent_type: IntentType) -> str:
        # Remove palavras-chave de ação para deixar o objetivo limpo.
        objective = prompt.strip()
        # Tenta extrair a frase principal (até o primeiro ponto ou vírgula).
        for sep in (".", ",", " e ", " então ", " portanto "):
            if sep in objective:
                objective = objective.split(sep, 1)[0].strip()
                break
        if not objective:
            objective = prompt.strip()
        return objective

    @staticmethod
    def _extract_constraints(lower: str) -> tuple[str, ...]:
        constraints: list[str] = []
        constraint_patterns = [
            (r"sem (.+)", "sem {0}"),
            (r"apenas (.+)", "apenas {0}"),
            (r"somente (.+)", "somente {0}"),
            (r"não (.+)", "não {0}"),
            (r"dentro de (.+)", "dentro de {0}"),
            (r"até (.+)", "até {0}"),
            (r"máximo de (.+)", "máximo de {0}"),
        ]
        for pattern, template in constraint_patterns:
            for match in re.finditer(pattern, lower):
                value = match.group(1).strip()
                if value:
                    constraints.append(template.format(value))
        return tuple(constraints)

    @staticmethod
    def _extract_expected_outcome(intent_type: IntentType) -> str:
        mapping = {
            IntentType.QUERY: "informação relevante e resposta direta",
            IntentType.ANALYSIS: "análise fundamentada com evidências",
            IntentType.PLANNING: "plano executável com etapas e critérios de sucesso",
            IntentType.EXECUTION: "operação executada com resultado verificado",
            IntentType.SIMULATION: "cenário simulado com métricas e análise",
            IntentType.MEMORY: "informação armazenada ou recuperada",
            IntentType.CONFIGURATION: "configuração ajustada e validada",
            IntentType.ADMINISTRATION: "operação administrativa concluída",
            IntentType.UNKNOWN: "",
        }
        return mapping.get(intent_type, "")

    @staticmethod
    def _extract_required_information(lower: str, intent_type: IntentType) -> tuple[str, ...]:
        info: list[str] = []
        if intent_type == IntentType.EXECUTION:
            info.append("parâmetros da operação a ser executada")
        if intent_type == IntentType.PLANNING:
            info.append("objetivo detalhado e recursos disponíveis")
        if intent_type == IntentType.SIMULATION:
            info.append("parâmetros do cenário e regras de simulação")
        return tuple(info)

    @staticmethod
    def _extract_risks(lower: str, intent_type: IntentType) -> tuple[str, ...]:
        risks: list[str] = []
        if intent_type == IntentType.EXECUTION:
            risks.append("efeitos colaterais não intencionais na operação")
        if intent_type == IntentType.ADMINISTRATION:
            risks.append("alteração de estado do sistema com impacto operacional")
        if any(kw in lower for kw in ("delete", "remove", "rm ", "formato", "format")):
            risks.append("perda de dados potencialmente irreversível")
        return tuple(risks)

    @staticmethod
    def _extract_capabilities(intent_type: IntentType) -> tuple[str, ...]:
        mapping = {
            IntentType.QUERY: ("memory.retrieval",),
            IntentType.ANALYSIS: ("reasoning", "memory.retrieval"),
            IntentType.PLANNING: ("planning", "reasoning"),
            IntentType.EXECUTION: ("automation", "security.approval"),
            IntentType.SIMULATION: ("simulation", "metrics"),
            IntentType.MEMORY: ("memory.write", "memory.read"),
            IntentType.CONFIGURATION: ("system.configure",),
            IntentType.ADMINISTRATION: ("system.admin",),
            IntentType.UNKNOWN: (),
        }
        return mapping.get(intent_type, ())


__all__ = ["IntentEngine", "Intent", "IntentType", "RiskLevel"]
