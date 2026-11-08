"""Node Manager — gerenciamento de nós no cluster distribuído NEXUS.

Responsabilidade:
* Registrar e deregistrar nós participantes.
* Acompanar heartbeats e detectar nós inalcançáveis.
* Manter o inventário de recursos e capacidades por nó.
* Fornecer visão centralizada do estado do cluster (governança).

Princípios:
* Governança centralizada: o nó controller decide quem participa.
* Detecção de falha via heartbeat com timeout configurável.
* Reentrada automática de nós que se reconectam.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .distributed_node import (
    DistributedNode,
    NodeError,
    NodeNotFound,
    NodeResources,
    NodeRole,
    NodeStatus,
)


class NodeManager:
    """Gerencia o ciclo de vida de nós distribuídos."""

    def __init__(self, *, heartbeat_timeout_s: float = 60.0) -> None:
        self._nodes: dict[str, DistributedNode] = {}
        self._lock = threading.RLock()
        self._heartbeat_timeout_s = heartbeat_timeout_s

    def register_node(
        self,
        *,
        hostname: str,
        address: str,
        role: NodeRole = NodeRole.WORKER,
        capabilities: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Registra um novo nó (ou reativa um existente com mesmo endereço)."""
        with self._lock:
            # Verifica se já existe um nó com este endereço (reconexão).
            for existing in self._nodes.values():
                if existing.address == address:
                    node_id = existing.node_id
                    updated = DistributedNode(
                        node_id=node_id,
                        hostname=hostname,
                        address=address,
                        role=role,
                        status=NodeStatus.ACTIVE,
                        resources=existing.resources,
                        capabilities=capabilities,
                        registered_at=existing.registered_at,
                        last_heartbeat=time.time(),
                        metadata=metadata or existing.metadata,
                    )
                    self._nodes[node_id] = updated
                    return node_id

            node_id = uuid_hex()
            node = DistributedNode(
                node_id=node_id,
                hostname=hostname,
                address=address,
                role=role,
                status=NodeStatus.ACTIVE,
                capabilities=capabilities,
                last_heartbeat=time.time(),
                metadata=metadata or {},
            )
            self._nodes[node_id] = node
            return node_id

    def heartbeat(self, node_id: str, resources: NodeResources | None = None) -> bool:
        """Atualiza o heartbeat de um nó e opcionalmente seus recursos."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return False
            updated = DistributedNode(
                node_id=node.node_id,
                hostname=node.hostname,
                address=node.address,
                role=node.role,
                status=NodeStatus.ACTIVE,
                resources=resources if resources is not None else node.resources,
                capabilities=node.capabilities,
                registered_at=node.registered_at,
                last_heartbeat=time.time(),
                metadata=node.metadata,
            )
            self._nodes[node_id] = updated
            return True

    def update_resources(self, node_id: str, resources: NodeResources) -> bool:
        """Atualiza os recursos reportados por um nó."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return False
            updated = DistributedNode(
                node_id=node.node_id,
                hostname=node.hostname,
                address=node.address,
                role=node.role,
                status=node.status,
                resources=resources,
                capabilities=node.capabilities,
                registered_at=node.registered_at,
                last_heartbeat=node.last_heartbeat,
                metadata=node.metadata,
            )
            self._nodes[node_id] = updated
            return True

    def unregister_node(self, node_id: str) -> bool:
        """Remove um nó do cluster."""
        with self._lock:
            if node_id not in self._nodes:
                return False
            del self._nodes[node_id]
            return True

    def mark_unreachable(self, node_id: str) -> bool:
        """Marca um nó como inalcançável (chamado pelo detector de falha)."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return False
            updated = DistributedNode(
                node_id=node.node_id,
                hostname=node.hostname,
                address=node.address,
                role=node.role,
                status=NodeStatus.UNREACHABLE,
                resources=node.resources,
                capabilities=node.capabilities,
                registered_at=node.registered_at,
                last_heartbeat=node.last_heartbeat,
                metadata=node.metadata,
            )
            self._nodes[node_id] = updated
            return True

    def get_node(self, node_id: str) -> DistributedNode | None:
        with self._lock:
            return self._nodes.get(node_id)

    def list_nodes(self) -> list[DistributedNode]:
        with self._lock:
            return list(self._nodes.values())

    def list_active_nodes(self) -> list[DistributedNode]:
        with self._lock:
            return [n for n in self._nodes.values() if n.is_healthy]

    def list_by_role(self, role: NodeRole) -> list[DistributedNode]:
        with self._lock:
            return [n for n in self._nodes.values() if n.role == role and n.is_healthy]

    def count(self) -> int:
        with self._lock:
            return len(self._nodes)

    def count_active(self) -> int:
        with self._lock:
            return sum(1 for n in self._nodes.values() if n.is_healthy)

    def detect_stale_nodes(self) -> list[str]:
        """Marca nós sem heartbeat recente como inalcançáveis. Retorna IDs afetados."""
        affected: list[str] = []
        now = time.time()
        with self._lock:
            for node_id, node in list(self._nodes.items()):
                if node.status == NodeStatus.UNREACHABLE:
                    continue
                if node.last_heartbeat is None:
                    continue
                if now - node.last_heartbeat > self._heartbeat_timeout_s:
                    self.mark_unreachable(node_id)
                    affected.append(node_id)
        return affected

    def cluster_summary(self) -> dict[str, Any]:
        with self._lock:
            active = self.list_active_nodes()
            total_cpu = sum((n.resources.cpu_cores if n.resources else 0) for n in active)
            total_ram = sum((n.resources.ram_total_mb if n.resources else 0) for n in active)
            free_ram = sum((n.resources.ram_available_mb if n.resources else 0) for n in active)
            return {
                "total_nodes": len(self._nodes),
                "active_nodes": len(active),
                "unreachable_nodes": len(self._nodes) - len(active),
                "total_cpu_cores": total_cpu,
                "total_ram_mb": total_ram,
                "free_ram_mb": free_ram,
                "roles": {
                    role.name: len(self.list_by_role(role)) for role in NodeRole
                },
            }


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = ["NodeManager"]
