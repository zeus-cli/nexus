"""Rules Engine — regras de simulação.

Responsabilidade: avaliar regras que disparam ações ou mudanças de estado
dentro de um mundo de simulação com base no estado atual e em eventos.

Princípios:
* Regras são declarativas e determinísticas (independentes de LLM).
* Cada rule tem condição (quando dispara) e ação (o que faz).
* Regras podem ser habilitadas/desabilitadas e versionadas.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping


class RuleStatus(Enum):
    ACTIVE = auto()
    INACTIVE = auto()


@dataclass(frozen=True, slots=True)
class SimulationRule:
    """Uma regra de simulação."""

    rule_id: str
    name: str
    paradigm: str  # identificador do paradigma ao qual pertence
    condition: str  # expressão avaliável (simplificada)
    action: str  # descrição da ação a executar
    priority: int = 0
    status: RuleStatus = RuleStatus.ACTIVE
    created_at: float = field(default_factory=lambda: __import__("time").monotonic())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "paradigm": self.paradigm,
            "condition": self.condition,
            "action": self.action,
            "priority": self.priority,
            "status": self.status.name,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class RulesEngine:
    """Avalia e execução regras de simulação."""

    def __init__(self) -> None:
        self._rules: dict[str, SimulationRule] = {}
        self._handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {}

    def register_rule(self, rule: SimulationRule) -> None:
        self._rules[rule.rule_id] = rule

    def unregister_rule(self, rule_id: str) -> bool:
        if rule_id not in self._rules:
            return False
        del self._rules[rule_id]
        self._handlers.pop(rule_id, None)
        return True

    def register_action_handler(self, rule_id: str, handler: Callable[[Mapping[str, Any]], Any]) -> None:
        self._handlers[rule_id] = handler

    def get_rule(self, rule_id: str) -> SimulationRule | None:
        return self._rules.get(rule_id)

    def list_rules(self, paradigm: str | None = None, active_only: bool = True) -> list[SimulationRule]:
        result = []
        for rule in self._rules.values():
            if active_only and rule.status != RuleStatus.ACTIVE:
                continue
            if paradigm is not None and rule.paradigm != paradigm:
                continue
            result.append(rule)
        return sorted(result, key=lambda r: (-r.priority, r.name))

    def set_status(self, rule_id: str, status: RuleStatus) -> bool:
        rule = self._rules.get(rule_id)
        if rule is None:
            return False
        object.__setattr__(rule, "status", status)  # frozen dataclass workaround
        return True

    def evaluate(self, rule_id: str, context: Mapping[str, Any]) -> bool:
        """Avalia a condição de uma regra contra o contexto."""
        rule = self._rules.get(rule_id)
        if rule is None or rule.status != RuleStatus.ACTIVE:
            return False
        return self._eval_condition(rule.condition, context)

    def _eval_condition(self, condition: str, context: Mapping[str, Any]) -> bool:
        """Avaliação simples de condição: suporta ==, !=, >, <, >=, <=, and, or, not."""
        # Substitui booleanos e operadores lógicos para eval seguro restrito.
        expr = condition
        # Nomes permitidos: chaves do contexto
        safe_dict = {k: v for k, v in context.items() if isinstance(v, (int, float, bool, str))}
        try:
            result = eval(expr, {"__builtins__": {}}, safe_dict)  # noqa: S307
            return bool(result)
        except Exception:
            return False

    def execute_action(self, rule_id: str, context: Mapping[str, Any]) -> Any:
        """Executa a ação de uma regra."""
        handler = self._handlers.get(rule_id)
        if handler is None:
            return None
        return handler(context)

    def fire_matching(
        self,
        context: Mapping[str, Any],
        paradigm: str | None = None,
    ) -> list[tuple[str, Any]]:
        """Dispara todas as regras ativas cuja condição é verdadeira no contexto."""
        fired = []
        for rule in self.list_rules(paradigm=paradigm, active_only=True):
            if self.evaluate(rule.rule_id, context):
                result = self.execute_action(rule.rule_id, context)
                fired.append((rule.rule_id, result))
        return fired


__all__ = [
    "RulesEngine",
    "SimulationRule",
    "RuleStatus",
]
