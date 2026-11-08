"""Agent Manifest — declaração de identidade e capacidades de um agente.

Cada agente deve declarar:

* Identidade (id, nome, versão)
* Papel (role)
* Capacidades
* Perfil de modelo (model profile)
* Escopo de memória (memory scope)
* Acesso a ferramentas (tool access)
* Limites de permissão (permission boundaries)
* Limites de recurso (resource limits)
* Timeout
* Esquema de saída (output schema)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class AgentRole(Enum):
    ORCHESTRATOR = auto()  # Coordenação
    RESEARCHER = auto()    # Pesquisa
    ANALYST = auto()       # Análise
    STRATEGIST = auto()    # Estratégia
    ENGINEER = auto()      # Engenharia
    CRITIC = auto()        # Revisão crítica
    SIMULATOR = auto()     # Simulação
    SECURITY = auto()      # Análise de segurança
    MEMORY_CURATOR = auto()  # Qualidade de memória


class AgentState(Enum):
    REGISTERED = auto()
    ACTIVE = auto()
    IDLE = auto()
    BUSY = auto()
    SUSPENDED = auto()
    TERMINATED = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_cpu_percent: float = 50.0
    max_ram_mb: int = 512
    max_concurrent_tasks: int = 3
    timeout_s: float = 300.0


@dataclass(frozen=True, slots=True)
class AgentManifest:
    """Manifesto do agente: identidade, papel, capacidades e limites."""

    agent_id: str
    name: str
    role: AgentRole
    version: str = "1.0.0"
    capabilities: tuple[str, ...] = ()
    model_profile: str = "general"
    memory_scope: tuple[str, ...] = ()  # quais categorias de memória pode acessar
    tool_access: tuple[str, ...] = ()  # tool_ids permitidos
    permission_boundaries: tuple[str, ...] = ()
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    output_schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.name,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "model_profile": self.model_profile,
            "memory_scope": list(self.memory_scope),
            "tool_access": list(self.tool_access),
            "permission_boundaries": list(self.permission_boundaries),
            "resource_limits": {
                "max_cpu_percent": self.resource_limits.max_cpu_percent,
                "max_ram_mb": self.resource_limits.max_ram_mb,
                "max_concurrent_tasks": self.resource_limits.max_concurrent_tasks,
                "timeout_s": self.resource_limits.timeout_s,
            },
            "output_schema": dict(self.output_schema),
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    def has_tool_access(self, tool_id: str) -> bool:
        return "*" in self.tool_access or tool_id in self.tool_access

    def has_capability(self, capability: str) -> bool:
        return "*" in self.capabilities or capability in self.capabilities


class ManifestError(Exception):
    """Erro no manifesto do agente."""


class DuplicateAgentId(ManifestError):
    """ID de agente duplicado no registro."""


__all__ = [
    "AgentManifest",
    "AgentRole",
    "AgentState",
    "ResourceLimits",
    "ManifestError",
    "DuplicateAgentId",
]
