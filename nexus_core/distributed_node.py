"""Distributed Node — representação de um nó no cluster NEXUS.

Responsabilidade: modelar um nó participante do ecossistema distribuído,
incluindo identidade, estado, recursos, capacidades e conectividade.

Princípios:
* Cada nó tem um ID estável e endereço de comunicação.
* Nós reportam recursos locais (CPU, RAM, GPU, disco, rede).
* O estado do nó é sincronizado com o controlador central (governança).
* Operações distribuídas respeitam políticas de segurança centralizadas.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class NodeRole(Enum):
    CONTROLLER = auto()  # Nó central de governança
    WORKER = auto()      # Nó de execução de tarefas/agentes
    HYBRID = auto()      # Ambos


class NodeStatus(Enum):
    JOINING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    UNREACHABLE = auto()
    LEAVING = auto()
    OFFLINE = auto()


@dataclass(frozen=True, slots=True)
class NodeResources:
    """Recursos reportados por um nó."""

    cpu_cores: int
    cpu_percent: float
    ram_total_mb: float
    ram_available_mb: float
    ram_percent: float
    gpu_available: bool
    gpu_memory_mb: float | None
    disk_free_mb: float
    disk_total_mb: float
    network_mbps: float | None
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.cpu_cores,
            "cpu_percent": self.cpu_percent,
            "ram_total_mb": self.ram_total_mb,
            "ram_available_mb": self.ram_available_mb,
            "ram_percent": self.ram_percent,
            "gpu_available": self.gpu_available,
            "gpu_memory_mb": self.gpu_memory_mb,
            "disk_free_mb": self.disk_free_mb,
            "disk_total_mb": self.disk_total_mb,
            "network_mbps": self.network_mbps,
            "updated_at": self.updated_at,
        }

    @property
    def ram_free_ratio(self) -> float:
        if self.ram_total_mb <= 0:
            return 0.0
        return self.ram_available_mb / self.ram_total_mb

    @property
    def cpu_free_ratio(self) -> float:
        if self.cpu_percent >= 100.0:
            return 0.0
        return max(0.0, (100.0 - self.cpu_percent) / 100.0)


@dataclass(frozen=True, slots=True)
class DistributedNode:
    """Um nó no cluster NEXUS."""

    node_id: str
    hostname: str
    address: str  # endereço de comunicação (ex.: UDS path ou host:port)
    role: NodeRole
    status: NodeStatus = NodeStatus.JOINING
    resources: NodeResources | None = None
    capabilities: tuple[str, ...] = ()
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "address": self.address,
            "role": self.role.name,
            "status": self.status.name,
            "resources": self.resources.as_dict() if self.resources else None,
            "capabilities": list(self.capabilities),
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "metadata": dict(self.metadata),
        }

    @property
    def is_healthy(self) -> bool:
        if self.status != NodeStatus.ACTIVE:
            return False
        if self.last_heartbeat is None:
            return False
        # Considera não saudável se não há heartbeat há mais de 60s.
        return (time.time() - self.last_heartbeat) < 60.0

    def can_accept_task(self, required_ram_mb: float = 0.0) -> bool:
        if not self.is_healthy:
            return False
        if self.resources is None:
            return False
        if self.resources.ram_available_mb < required_ram_mb:
            return False
        if self.resources.cpu_percent > 90.0:
            return False
        return True


class NodeError(Exception):
    """Erro no gerenciamento de nós."""


class NodeNotFound(NodeError):
    """Nó não encontrado no registro."""


__all__ = [
    "DistributedNode",
    "NodeResources",
    "NodeRole",
    "NodeStatus",
    "NodeError",
    "NodeNotFound",
]
