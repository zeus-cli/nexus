"""Distributed Resource Monitor — gerenciamento avançado de recursos no cluster.

Responsabilidade:
* Monitorar recursos locais (CPU, RAM, GPU, disco, rede) do nó atual.
* Agregar recursos reportados por nós remotos via Node Manager.
* Fornecer visão centralizada de capacidade e utilização do cluster.
* Apoiar decisões de agendamento distribuído.

Princípios:
* Recursos são reportados pelos próprios nós (auto-relato).
* O controlador central agrega e expõe a visão consolidada.
* Decisões de alocação respeitam limites de cada nó.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import psutil

from .distributed_node import NodeResources
from .distributed_node_manager import NodeManager


@dataclass(frozen=True, slots=True)
class ClusterResourceView:
    """Visão consolidada de recursos do cluster."""

    total_cpu_cores: int
    total_cpu_percent: float
    total_ram_mb: float
    total_ram_available_mb: float
    total_ram_percent: float
    free_cpu_ratio: float
    free_ram_ratio: float
    nodes_with_gpu: int
    total_gpu_memory_mb: float | None
    total_disk_free_mb: float
    total_disk_total_mb: float
    updated_at: float
    node_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cpu_cores": self.total_cpu_cores,
            "total_cpu_percent": round(self.total_cpu_percent, 2),
            "total_ram_mb": round(self.total_ram_mb, 2),
            "total_ram_available_mb": round(self.total_ram_available_mb, 2),
            "total_ram_percent": round(self.total_ram_percent, 2),
            "free_cpu_ratio": round(self.free_cpu_ratio, 3),
            "free_ram_ratio": round(self.free_ram_ratio, 3),
            "nodes_with_gpu": self.nodes_with_gpu,
            "total_gpu_memory_mb": self.total_gpu_memory_mb,
            "total_disk_free_mb": round(self.total_disk_free_mb, 2),
            "total_disk_total_mb": round(self.total_disk_total_mb, 2),
            "updated_at": self.updated_at,
            "node_count": self.node_count,
        }


class ResourceMonitor:
    """Monitor de recursos locais (espelhamento do resources.py existente)."""

    def __init__(self) -> None:
        self._cpu_percent = 0.0

    def get_local_resources(self) -> NodeResources:
        """Coleta recursos locais do nó atual."""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        # GPU: detecção simples (nvidia-smi não é dependência; usa flag de env).
        gpu_available = bool(os.environ.get("NEXUS_GPU_AVAILABLE", "").lower() in ("1", "true"))
        gpu_memory_mb = None
        if gpu_available:
            try:
                gpu_memory_mb = float(os.environ.get("NEXUS_GPU_MEMORY_MB", "0")) or None
            except ValueError:
                gpu_memory_mb = None

        # CPU percent (primeira chamada pode retornar 0.0; é o comportamento do psutil).
        cpu_percent = psutil.cpu_percent(interval=None)

        return NodeResources(
            cpu_cores=psutil.cpu_count(logical=True) or 1,
            cpu_percent=cpu_percent,
            ram_total_mb=mem.total / (1024.0 * 1024.0),
            ram_available_mb=mem.available / (1024.0 * 1024.0),
            ram_percent=mem.percent,
            gpu_available=gpu_available,
            gpu_memory_mb=gpu_memory_mb,
            disk_free_mb=disk.free / (1024.0 * 1024.0),
            disk_total_mb=disk.total / (1024.0 * 1024.0),
            network_mbps=None,
            updated_at=time.time(),
        )


class DistributedResourceMonitor:
    """Agrega recursos locais e remotos em uma visão centralizada."""

    def __init__(self, node_manager: NodeManager) -> None:
        self._local_monitor = ResourceMonitor()
        self._node_manager = node_manager

    def get_cluster_view(self) -> ClusterResourceView:
        """Retorna a visão consolidada de recursos do cluster."""
        active_nodes = self._node_manager.list_active_nodes()

        total_cpu_cores = 0
        total_cpu_percent_sum = 0.0
        total_ram_mb = 0.0
        total_ram_available_mb = 0.0
        nodes_with_gpu = 0
        total_gpu_memory_mb = 0.0
        total_disk_free_mb = 0.0
        total_disk_total_mb = 0.0

        # Inclui o nó local se estiver registrado.
        for node in active_nodes:
            res = node.resources
            if res is None:
                continue
            total_cpu_cores += res.cpu_cores
            total_cpu_percent_sum += res.cpu_percent
            total_ram_mb += res.ram_total_mb
            total_ram_available_mb += res.ram_available_mb
            if res.gpu_available:
                nodes_with_gpu += 1
                if res.gpu_memory_mb is not None:
                    total_gpu_memory_mb += res.gpu_memory_mb
            total_disk_free_mb += res.disk_free_mb
            total_disk_total_mb += res.disk_total_mb

        node_count = len(active_nodes)
        avg_cpu_percent = total_cpu_percent_sum / node_count if node_count > 0 else 0.0
        total_ram_percent = (
            (total_ram_mb - total_ram_available_mb) / total_ram_mb * 100.0
            if total_ram_mb > 0
            else 0.0
        )

        return ClusterResourceView(
            total_cpu_cores=total_cpu_cores,
            total_cpu_percent=avg_cpu_percent,
            total_ram_mb=total_ram_mb,
            total_ram_available_mb=total_ram_available_mb,
            total_ram_percent=total_ram_percent,
            free_cpu_ratio=max(0.0, 1.0 - avg_cpu_percent / 100.0),
            free_ram_ratio=(
                total_ram_available_mb / total_ram_mb if total_ram_mb > 0 else 0.0
            ),
            nodes_with_gpu=nodes_with_gpu,
            total_gpu_memory_mb=total_gpu_memory_mb if nodes_with_gpu > 0 else None,
            total_disk_free_mb=total_disk_free_mb,
            total_disk_total_mb=total_disk_total_mb,
            updated_at=time.time(),
            node_count=node_count,
        )

    def can_schedule_locally(self, required_ram_mb: float = 0.0) -> bool:
        """Verifica se o nó local pode aceitar uma tarefa."""
        res = self._local_monitor.get_local_resources()
        return res.can_accept_task(required_ram_mb)

    def find_best_node_for_task(
        self,
        required_ram_mb: float = 0.0,
        require_gpu: bool = False,
        preferred_role: str | None = None,
    ) -> str | None:
        """Encontra o nó mais adequado para uma tarefa com base em recursos."""
        from .distributed_node import NodeRole

        candidates = []
        for node in self._node_manager.list_active_nodes():
            if not node.can_accept_task(required_ram_mb):
                continue
            if require_gpu and not (node.resources and node.resources.gpu_available):
                continue
            if preferred_role is not None:
                try:
                    role = NodeRole[preferred_role.upper()]
                except (KeyError, AttributeError):
                    role = None
                if role is not None and node.role != role:
                    continue
            candidates.append(node)

        if not candidates:
            return None

        # Escolhe o nó com mais RAM livre proporcional (menor carga).
        def score(node: DistributedNode) -> float:
            res = node.resources
            if res is None:
                return 0.0
            return res.ram_free_ratio + res.cpu_free_ratio

        return max(candidates, key=score).node_id

    def as_dict(self) -> dict[str, Any]:
        return self.get_cluster_view().as_dict()


__all__ = [
    "ResourceMonitor",
    "DistributedResourceMonitor",
    "ClusterResourceView",
]
