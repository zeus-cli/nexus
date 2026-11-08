"""Agent Registry — registro central de agentes e seus manifestos.

Responsabilidade: cadastrar, consultar e gerenciar o ciclo de vida dos agentes
especializados, garantindo que cada um opere dentro de capacidades explicitamente
declaradas.
"""

from __future__ import annotations

import threading
from typing import Any

from .agent_manifest import (
    AgentManifest,
    AgentRole,
    AgentState,
    DuplicateAgentId,
    ManifestError,
)


class AgentRegistry:
    """Registro de agentes com validação de manifesto e estado de ciclo de vida."""

    def __init__(self) -> None:
        self._manifests: dict[str, AgentManifest] = {}
        self._states: dict[str, AgentState] = {}
        self._lock = threading.RLock()

    def register(self, manifest: AgentManifest) -> None:
        """Registra um agente com seu manifesto."""
        with self._lock:
            if manifest.agent_id in self._manifests:
                raise DuplicateAgentId(
                    f"agent already registered: {manifest.agent_id}"
                )
            self._manifests[manifest.agent_id] = manifest
            self._states[manifest.agent_id] = AgentState.REGISTERED

    def unregister(self, agent_id: str) -> bool:
        """Remove um agente do registro."""
        with self._lock:
            if agent_id not in self._manifests:
                return False
            del self._manifests[agent_id]
            del self._states[agent_id]
            return True

    def get_manifest(self, agent_id: str) -> AgentManifest | None:
        with self._lock:
            return self._manifests.get(agent_id)

    def get_state(self, agent_id: str) -> AgentState | None:
        with self._lock:
            return self._states.get(agent_id)

    def set_state(self, agent_id: str, state: AgentState) -> bool:
        with self._lock:
            if agent_id not in self._manifests:
                return False
            self._states[agent_id] = state
            return True

    def list_agents(self) -> list[tuple[AgentManifest, AgentState]]:
        with self._lock:
            return [
                (self._manifests[aid], self._states[aid])
                for aid in self._manifests
            ]

    def list_by_role(self, role: AgentRole) -> list[AgentManifest]:
        with self._lock:
            return [
                m for m, _ in self.list_agents() if m.role == role
            ]

    def can_access_tool(self, agent_id: str, tool_id: str) -> bool:
        """Verifica se o agente tem acesso à ferramenta informada."""
        with self._lock:
            manifest = self._manifests.get(agent_id)
            if manifest is None:
                return False
            return manifest.has_tool_access(tool_id)

    def has_capability(self, agent_id: str, capability: str) -> bool:
        """Verifica se o agente possui a capacidade declarada."""
        with self._lock:
            manifest = self._manifests.get(agent_id)
            if manifest is None:
                return False
            return manifest.has_capability(capability)

    def count(self) -> int:
        with self._lock:
            return len(self._manifests)

    def list_dict(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "agent_id": m.agent_id,
                    "name": m.name,
                    "role": m.role.name,
                    "version": m.version,
                    "state": self._states[m.agent_id].name,
                    "capabilities": list(m.capabilities),
                    "model_profile": m.model_profile,
                    "tool_access": list(m.tool_access),
                }
                for m, _ in self.list_agents()
            ]


__all__ = [
    "AgentRegistry",
    "DuplicateAgentId",
    "ManifestError",
]
