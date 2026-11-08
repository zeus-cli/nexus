"""Distributed Task Scheduler — agendamento de tarefas entre nós do cluster.

Responsabilidade:
* Agendar tarefas de agentes para nós com recursos disponíveis.
* Suportar execução paralela com isolamento de falhas entre nós.
* Reagendar tarefas quando um nó fica inalcançável.
* Manter fila de tarefas pendentes e histórico de alocações.

Princípios:
* Agendamento baseado em recursos (RAM, CPU, GPU).
* Failover: se o nó atribuído falhar, a tarefa é reenfileirada.
* Governança centralizada: o scheduler roda no nó controller.
* Não existe agendamento sem verificação de recursos.
"""

from __future__ import annotations

import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping

from .distributed_node import DistributedNode, NodeRole
from .distributed_node_manager import NodeManager
from .distributed_resources import DistributedResourceMonitor


class TaskPriority(Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class ScheduledTaskStatus(Enum):
    PENDING = auto()
    ASSIGNED = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    RESCHEDULED = auto()


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """Uma tarefa agendada para execução distribuída."""

    task_id: str
    description: str
    input: dict[str, Any]
    required_ram_mb: float = 0.0
    require_gpu: bool = False
    priority: TaskPriority = TaskPriority.NORMAL
    preferred_role: str | None = None
    status: ScheduledTaskStatus = ScheduledTaskStatus.PENDING
    assigned_node_id: str | None = None
    created_at: float = field(default_factory=time.time)
    assigned_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "input": dict(self.input),
            "required_ram_mb": self.required_ram_mb,
            "require_gpu": self.require_gpu,
            "priority": self.priority.name,
            "status": self.status.name,
            "assigned_node_id": self.assigned_node_id,
            "created_at": self.created_at,
            "assigned_at": self.assigned_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": dict(self.result) if self.result else None,
            "error": self.error,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "metadata": dict(self.metadata),
        }


class TaskSchedulerError(Exception):
    """Erro no agendador de tarefas."""


class NoAvailableNode(TaskSchedulerError):
    """Nenhum nó disponível para a tarefa."""


class DistributedTaskScheduler:
    """Agenda tarefas para nós com recursos disponíveis no cluster."""

    def __init__(
        self,
        node_manager: NodeManager,
        resource_monitor: DistributedResourceMonitor,
    ) -> None:
        self._node_manager = node_manager
        self._resource_monitor = resource_monitor
        self._tasks: dict[str, ScheduledTask] = {}
        self._heap: list[tuple[float, int, str]] = []  # (neg_priority, counter, task_id)
        self._counter = 0
        self._lock = asyncio.Lock()

    def submit_task(
        self,
        *,
        description: str,
        input: dict[str, Any],
        required_ram_mb: float = 0.0,
        require_gpu: bool = False,
        priority: TaskPriority = TaskPriority.NORMAL,
        preferred_role: str | None = None,
        task_id: str | None = None,
        max_attempts: int = 3,
    ) -> str:
        """Submete uma tarefa para agendamento. Retorna o task_id."""
        tid = task_id or uuid.uuid4().hex
        task = ScheduledTask(
            task_id=tid,
            description=description,
            input=dict(input),
            required_ram_mb=required_ram_mb,
            require_gpu=require_gpu,
            priority=priority,
            preferred_role=preferred_role,
            max_attempts=max_attempts,
        )
        self._tasks[tid] = task
        self._counter += 1
        # Heap ordenado por prioridade (maior primeiro) e ordem de inserção.
        heapq.heappush(
            self._heap, (-priority.value, self._counter, tid)
        )
        return tid

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def list_pending(self) -> list[ScheduledTask]:
        return [
            t for t in self._tasks.values()
            if t.status == ScheduledTaskStatus.PENDING
        ]

    def list_by_node(self, node_id: str) -> list[ScheduledTask]:
        return [
            t for t in self._tasks.values()
            if t.assigned_node_id == node_id
        ]

    async def schedule_next(self) -> str | None:
        """Escolhe a próxima tarefa pendente e atribui a um nó adequado.

        Retorna o task_id atribuído ou None se não houver nó/tarefa disponível.
        """
        async with self._lock:
            # Esvazia o heap re-inserindo apenas tarefas ainda pendentes.
            pending: list[tuple[float, int, str]] = []
            while self._heap:
                neg_prio, counter, tid = heapq.heappop(self._heap)
                task = self._tasks.get(tid)
                if task is not None and task.status == ScheduledTaskStatus.PENDING:
                    pending.append((neg_prio, counter, tid))
            for item in pending:
                heapq.heappush(self._heap, item)

            if not self._heap:
                return None

            neg_prio, counter, tid = heapq.heappop(self._heap)
            task = self._tasks[tid]

            node_id = self._resource_monitor.find_best_node_for_task(
                required_ram_mb=task.required_ram_mb,
                require_gpu=task.require_gpu,
                preferred_role=task.preferred_role,
            )
            if node_id is None:
                # Re-insere a tarefa para tentar depois.
                heapq.heappush(self._heap, (neg_prio, counter, tid))
                return None

            now = time.time()
            assigned = ScheduledTask(
                task_id=task.task_id,
                description=task.description,
                input=task.input,
                required_ram_mb=task.required_ram_mb,
                require_gpu=task.require_gpu,
                priority=task.priority,
                preferred_role=task.preferred_role,
                status=ScheduledTaskStatus.ASSIGNED,
                assigned_node_id=node_id,
                created_at=task.created_at,
                assigned_at=now,
                started_at=task.started_at,
                completed_at=task.completed_at,
                result=task.result,
                error=task.error,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
            )
            self._tasks[tid] = assigned
            return tid

    async def mark_started(self, task_id: str) -> bool:
        """Marca uma tarefa atribuída como em execução."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != ScheduledTaskStatus.ASSIGNED:
                return False
            now = time.time()
            self._tasks[task_id] = ScheduledTask(
                task_id=task.task_id,
                description=task.description,
                input=task.input,
                required_ram_mb=task.required_ram_mb,
                require_gpu=task.require_gpu,
                priority=task.priority,
                preferred_role=task.preferred_role,
                status=ScheduledTaskStatus.RUNNING,
                assigned_node_id=task.assigned_node_id,
                created_at=task.created_at,
                assigned_at=task.assigned_at,
                started_at=now,
                completed_at=task.completed_at,
                result=task.result,
                error=task.error,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
            )
            return True

    async def complete_task(
        self,
        task_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Marca uma tarefa como concluída ou falhada."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in (
                ScheduledTaskStatus.ASSIGNED,
                ScheduledTaskStatus.RUNNING,
            ):
                return False
            now = time.time()
            status = ScheduledTaskStatus.FAILED if error is not None else ScheduledTaskStatus.COMPLETED
            self._tasks[task_id] = ScheduledTask(
                task_id=task.task_id,
                description=task.description,
                input=task.input,
                required_ram_mb=task.required_ram_mb,
                require_gpu=task.require_gpu,
                priority=task.priority,
                preferred_role=task.preferred_role,
                status=status,
                assigned_node_id=task.assigned_node_id,
                created_at=task.created_at,
                assigned_at=task.assigned_at,
                started_at=task.started_at,
                completed_at=now,
                result=dict(result) if result else None,
                error=error,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
            )
            return True

    async def fail_task(self, task_id: str, error: str) -> bool:
        """Marca uma tarefa como falhada com possibilidade de retry."""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.attempts >= task.max_attempts:
            # Esgotou tentativas: marca como falhada definitivamente.
            return await self.complete_task(task_id, error=error)

        # Reagenda a tarefa.
        now = time.time()
        rescheduled = ScheduledTask(
            task_id=task.task_id,
            description=task.description,
            input=task.input,
            required_ram_mb=task.required_ram_mb,
            require_gpu=task.require_gpu,
            priority=task.priority,
            preferred_role=task.preferred_role,
            status=ScheduledTaskStatus.RESCHEDULED,
            assigned_node_id=None,
            created_at=task.created_at,
            assigned_at=task.assigned_at,
            started_at=task.started_at,
            completed_at=None,
            result=None,
            error=error,
            attempts=task.attempts + 1,
            max_attempts=task.max_attempts,
            metadata=dict(task.metadata, last_error=error, last_attempt_at=now),
        )
        self._tasks[task_id] = rescheduled
        self._counter += 1
        heapq.heappush(
            self._heap, (-rescheduled.priority.value, self._counter, task_id)
        )
        return True

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status in (
            ScheduledTaskStatus.COMPLETED,
            ScheduledTaskStatus.FAILED,
            ScheduledTaskStatus.CANCELLED,
        ):
            return False
        self._tasks[task_id] = ScheduledTask(
            task_id=task.task_id,
            description=task.description,
            input=task.input,
            required_ram_mb=task.required_ram_mb,
            require_gpu=task.require_gpu,
            priority=task.priority,
            preferred_role=task.preferred_role,
            status=ScheduledTaskStatus.CANCELLED,
            assigned_node_id=task.assigned_node_id,
            created_at=task.created_at,
            assigned_at=task.assigned_at,
            started_at=task.started_at,
            completed_at=time.time(),
            result=task.result,
            error=task.error,
            attempts=task.attempts,
            max_attempts=task.max_attempts,
            metadata=task.metadata,
        )
        return True

    def handle_node_unreachable(self, node_id: str) -> list[str]:
        """Reenfileira tarefas atribuídas a um nó inalcançável."""
        requeued: list[str] = []
        for task in list(self._tasks.values()):
            if task.assigned_node_id == node_id and task.status in (
                ScheduledTaskStatus.ASSIGNED,
                ScheduledTaskStatus.RUNNING,
            ):
                self.cancel_task(task.task_id)
                # Re-submete com mesma prioridade e tentativas restantes.
                self.submit_task(
                    description=task.description,
                    input=task.input,
                    required_ram_mb=task.required_ram_mb,
                    require_gpu=task.require_gpu,
                    priority=task.priority,
                    preferred_role=task.preferred_role,
                    task_id=task.task_id,
                    max_attempts=task.max_attempts,
                )
                # Ajusta status de volta para pendente (submit_task cria PENDING).
                requeued.append(task.task_id)
        return requeued

    def scheduler_summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for status in ScheduledTaskStatus:
            counts[status.name] = 0
        for task in self._tasks.values():
            counts[task.status.name] += 1
        return {
            "total_tasks": len(self._tasks),
            "by_status": counts,
            "pending_in_heap": len(self._heap),
        }


__all__ = [
    "DistributedTaskScheduler",
    "ScheduledTask",
    "TaskPriority",
    "ScheduledTaskStatus",
    "TaskSchedulerError",
    "NoAvailableNode",
]
