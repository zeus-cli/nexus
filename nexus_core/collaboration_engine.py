"""Collaboration Engine — compartilhamento de contexto entre agentes.

Responsabilidade: permitir que agentes compartilhem contexto de tarefa,
resultados parciais e insights, mantendo isolamento de permissão. Agentes
podem ler contribuições de outros agentes apenas dentro do escopo da tarefa
compartilhada.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Contribution:
    """Uma contribuição de um agente para uma tarefa colaborativa."""

    contribution_id: str
    task_id: str
    agent_id: str
    content: Mapping[str, Any]
    confidence: float
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "content": dict(self.content),
            "confidence": self.confidence,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class CollaborationEngine:
    """Gerencia contribuições colaborativas entre agentes de uma tarefa."""

    def __init__(self) -> None:
        self._contributions: dict[str, list[Contribution]] = {}
        self._task_agents: dict[str, set[str]] = {}

    def add_contribution(
        self,
        *,
        task_id: str,
        agent_id: str,
        content: Mapping[str, Any],
        confidence: float = 0.5,
    ) -> str:
        """Adiciona uma contribuição de um agente a uma tarefa."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        contribution_id = uuid.uuid4().hex
        contribution = Contribution(
            contribution_id=contribution_id,
            task_id=task_id,
            agent_id=agent_id,
            content=dict(content),
            confidence=confidence,
            created_at=time.monotonic(),
        )
        self._contributions.setdefault(task_id, []).append(contribution)
        self._task_agents.setdefault(task_id, set()).add(agent_id)
        return contribution_id

    def get_contributions(self, task_id: str) -> tuple[Contribution, ...]:
        return tuple(self._contributions.get(task_id, []))

    def get_task_agents(self, task_id: str) -> frozenset[str]:
        return frozenset(self._task_agents.get(task_id, set()))

    def consolidate(self, task_id: str) -> dict[str, Any]:
        """Consolida todas as contribuições de uma tarefa em uma saída unificada.

        Agentes podem discordar; a consolidação preserva contribuições
        individuais com suas confianças, não impõe consenso artificial.
        """
        contributions = self.get_contributions(task_id)
        agents = self.get_task_agents(task_id)

        consolidated: dict[str, Any] = {
            "task_id": task_id,
            "agents": sorted(agents),
            "contribution_count": len(contributions),
            "by_agent": {},
            "conflicts": [],
            "consensus": None,
        }

        # Agrupa por agente.
        for c in contributions:
            consolidated["by_agent"].setdefault(c.agent_id, []).append(
                {
                    "contribution_id": c.contribution_id,
                    "content": dict(c.content),
                    "confidence": c.confidence,
                    "created_at": c.created_at,
                }
            )

        # Detecta conflitos: mesma chave com valores diferentes entre agentes.
        keys_seen: dict[str, list[tuple[str, Any]]] = {}
        for c in contributions:
            for k, v in c.content.items():
                keys_seen.setdefault(k, []).append((c.agent_id, v))
        for k, entries in keys_seen.items():
            # Comparação por igualdade: ``content`` aceita valores arbitrários
            # (dict/list), que não são hashable e não podem ir para um set.
            values: list[Any] = []
            for _, v in entries:
                if not any(v == existing for existing in values):
                    values.append(v)
            if len(values) > 1:
                consolidated["conflicts"].append(
                    {"key": k, "values": [str(v) for v in values], "agents": [a for a, _ in entries]}
                )

        # Consenso simples: valor com maior confiança agregada.
        if not consolidated["conflicts"] and contributions:
            best: dict[str, Any] = {}
            for c in contributions:
                for k, v in c.content.items():
                    if k not in best or c.confidence > best[k][1]:
                        best[k] = (v, c.confidence)
            consolidated["consensus"] = {k: v for k, (v, _) in best.items()}

        return consolidated

    def list_tasks(self) -> list[str]:
        return list(self._contributions.keys())


__all__ = ["CollaborationEngine", "Contribution"]
