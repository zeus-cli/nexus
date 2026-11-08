"""Agent Lifecycle Manager — gerenciamento do ciclo de vida de agentes.

Responsabilidade: criação, ativação, suspensão, retomada e terminação de
agentes, respeitando limites de recurso e concorrência declarados no manifesto.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent_manifest import AgentManifest, AgentState, ResourceLimits
from .agent_registry import AgentRegistry, DuplicateAgentId


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """Instância ativa de um agente com estado e métricas de ciclo de vida."""

    agent_id: str
    manifest: AgentManifest
    state: AgentState
    created_at: float
    activated_at: float | None
    terminated_at: float | None
    task_count: int
    success_count: int
    failure_count: int
    last_error: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.manifest.name,
            "role": self.manifest.role.name,
            "state": self.state.name,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "terminated_at": self.terminated_at,
            "task_count": self.task_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


class AgentLifecycleManager:
    """Gerencia o ciclo de vida completo de agentes."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry
        self._instances: dict[str, AgentInstance] = {}

    def spawn(
        self,
        manifest: AgentManifest,
        *,
        auto_activate: bool = False,
    ) -> AgentInstance:
        """Cria uma instância de agente a partir de um manifesto."""
        self._registry.register(manifest)
        now = time.monotonic()
        initial_state = AgentState.ACTIVE if auto_activate else AgentState.REGISTERED
        instance = AgentInstance(
            agent_id=manifest.agent_id,
            manifest=manifest,
            state=initial_state,
            created_at=now,
            activated_at=now if auto_activate else None,
            terminated_at=None,
            task_count=0,
            success_count=0,
            failure_count=0,
            last_error=None,
        )
        self._instances[manifest.agent_id] = instance
        self._registry.set_state(manifest.agent_id, initial_state)
        return instance

    def activate(self, agent_id: str) -> bool:
        """Ativa um agente registrado."""
        instance = self._instances.get(agent_id)
        if instance is None:
            return False
        if instance.state in (AgentState.ACTIVE, AgentState.BUSY):
            return True
        now = time.monotonic()
        instance = AgentInstance(
            agent_id=instance.agent_id,
            manifest=instance.manifest,
            state=AgentState.ACTIVE,
            created_at=instance.created_at,
            activated_at=now,
            terminated_at=instance.terminated_at,
            task_count=instance.task_count,
            success_count=instance.success_count,
            failure_count=instance.failure_count,
            last_error=instance.last_error,
            metadata=instance.metadata,
        )
        self._instances[agent_id] = instance
        self._registry.set_state(agent_id, AgentState.ACTIVE)
        return True

    def suspend(self, agent_id: str) -> bool:
        """Suspende um agente ativo."""
        instance = self._instances.get(agent_id)
        if instance is None or instance.state != AgentState.ACTIVE:
            return False
        instance = AgentInstance(
            agent_id=instance.agent_id,
            manifest=instance.manifest,
            state=AgentState.SUSPENDED,
            created_at=instance.created_at,
            activated_at=instance.activated_at,
            terminated_at=instance.terminated_at,
            task_count=instance.task_count,
            success_count=instance.success_count,
            failure_count=instance.failure_count,
            last_error=instance.last_error,
            metadata=instance.metadata,
        )
        self._instances[agent_id] = instance
        self._registry.set_state(agent_id, AgentState.SUSPENDED)
        return True

    def resume(self, agent_id: str) -> bool:
        """Retoma um agente suspenso."""
        instance = self._instances.get(agent_id)
        if instance is None or instance.state != AgentState.SUSPENDED:
            return False
        now = time.monotonic()
        instance = AgentInstance(
            agent_id=instance.agent_id,
            manifest=instance.manifest,
            state=AgentState.ACTIVE,
            created_at=instance.created_at,
            activated_at=now,
            terminated_at=instance.terminated_at,
            task_count=instance.task_count,
            success_count=instance.success_count,
            failure_count=instance.failure_count,
            last_error=instance.last_error,
            metadata=instance.metadata,
        )
        self._instances[agent_id] = instance
        self._registry.set_state(agent_id, AgentState.ACTIVE)
        return True

    def terminate(self, agent_id: str) -> bool:
        """Termina um agente definitivamente."""
        instance = self._instances.get(agent_id)
        if instance is None or instance.state == AgentState.TERMINATED:
            return False
        now = time.monotonic()
        instance = AgentInstance(
            agent_id=instance.agent_id,
            manifest=instance.manifest,
            state=AgentState.TERMINATED,
            created_at=instance.created_at,
            activated_at=instance.activated_at,
            terminated_at=now,
            task_count=instance.task_count,
            success_count=instance.success_count,
            failure_count=instance.failure_count,
            last_error=instance.last_error,
            metadata=instance.metadata,
        )
        self._instances[agent_id] = instance
        self._registry.set_state(agent_id, AgentState.TERMINATED)
        self._registry.unregister(agent_id)
        return True

    def record_task_result(
        self,
        agent_id: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Registra o resultado de uma tarefa executada pelo agente."""
        instance = self._instances.get(agent_id)
        if instance is None:
            return
        instance = AgentInstance(
            agent_id=instance.agent_id,
            manifest=instance.manifest,
            state=instance.state,
            created_at=instance.created_at,
            activated_at=instance.activated_at,
            terminated_at=instance.terminated_at,
            task_count=instance.task_count + 1,
            success_count=instance.success_count + (1 if success else 0),
            failure_count=instance.failure_count + (0 if success else 1),
            last_error=error if not success else instance.last_error,
            metadata=instance.metadata,
        )
        self._instances[agent_id] = instance

    def get_instance(self, agent_id: str) -> AgentInstance | None:
        return self._instances.get(agent_id)

    def list_instances(self) -> list[AgentInstance]:
        return list(self._instances.values())

    def active_agents(self) -> list[AgentInstance]:
        return [i for i in self._instances.values() if i.state == AgentState.ACTIVE]

    def busy_agents(self) -> list[AgentInstance]:
        return [i for i in self._instances.values() if i.state == AgentState.BUSY]


__all__ = [
    "AgentLifecycleManager",
    "AgentInstance",
]
