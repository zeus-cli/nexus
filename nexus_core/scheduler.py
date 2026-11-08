"""Agendador de tarefas em background para NEXUS.

Permite registrar tarefas (diagnósticos de automação ou tarefas cognitivas)
para execução periódica (intervalo) ou em horário absoluto (cron-like simplificado).
Todas as execuções passam pelo Execution Gate e respeitam o nível de autonomia.
"""

from __future__ import annotations

import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .automation import AutomationEngine, ApprovalRequired
from .cognitive import CognitiveCore


class SchedulerError(Exception):
    """Erro no agendador."""


class TaskAlreadyExists(SchedulerError):
    """Uma tarefa com este ID já está agendada."""


class TaskNotFound(SchedulerError):
    """Tarefa agendada não encontrada."""


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """Tarefa agendada."""

    task_id: str
    name: str
    kind: str  # "automation" ou "cognitive"
    target: str  # action_id para automation, prompt para cognitive
    interval_s: Optional[float]  # None para execução única (one-shot)
    next_run: float  # timestamp Unix do próximo run
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __lt__(self, other: "ScheduledTask") -> bool:
        return self.next_run < other.next_run

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "kind": self.kind,
            "target": self.target,
            "interval_s": self.interval_s,
            "next_run": self.next_run,
            "next_run_iso": datetime.fromtimestamp(self.next_run, tz=timezone.utc).isoformat(),
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }


class Scheduler:
    """Agendador de tarefas em background.

    Executa tarefas em loop assíncrono, respeitando o next_run de cada tarefa.
    Tarefas de automação passam por AutomationEngine (com HITL se necessário).
    Tarefas cognitivas passam por CognitiveCore.
    """

    def __init__(
        self,
        automation: AutomationEngine,
        cognitive: CognitiveCore,
        *,
        tick_s: float = 1.0,
    ) -> None:
        self._automation = automation
        self._cognitive = cognitive
        self._tick_s = tick_s
        self._heap: list[ScheduledTask] = []
        self._index: dict[str, ScheduledTask] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._lock = asyncio.Lock()
        self._history: list[dict[str, Any]] = []
        self._max_history = 500

    def _heap_push(self, task: ScheduledTask) -> None:
        heapq.heappush(self._heap, task)
        self._index[task.task_id] = task

    def _heap_remove(self, task_id: str) -> None:
        task = self._index.pop(task_id, None)
        if task is None:
            raise TaskNotFound(f"tarefa agendada não encontrada: {task_id}")
        # Marca como desabilitada; será ignorada no pop. O heap não é
        # rearranjado (lazy deletion) — aceitável para poucos agendamentos.
        self._index[task_id] = task.__class__(
            task_id=task.task_id,
            name=task.name,
            kind=task.kind,
            target=task.target,
            interval_s=task.interval_s,
            next_run=task.next_run,
            enabled=False,
            metadata=task.metadata,
        )

    def schedule(
        self,
        *,
        name: str,
        kind: str,
        target: str,
        interval_s: Optional[float] = None,
        start_at: Optional[float] = None,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        enabled: bool = True,
    ) -> str:
        """Agenda uma nova tarefa.

        - kind: "automation" (target = action_id) ou "cognitive" (target = prompt).
        - interval_s: segundos entre execuções (None = one-shot).
        - start_at: timestamp Unix para primeira execução (None = agora + interval ou agora).
        """
        if kind not in ("automation", "cognitive"):
            raise SchedulerError(f"kind inválido: {kind!r} (use 'automation' ou 'cognitive')")

        tid = task_id or uuid.uuid4().hex
        if tid in self._index:
            raise TaskAlreadyExists(f"tarefa já agendada: {tid}")

        now = time.monotonic()
        if start_at is not None:
            first_run = start_at
        elif interval_s is not None:
            first_run = now + interval_s
        else:
            first_run = now

        task = ScheduledTask(
            task_id=tid,
            name=name,
            kind=kind,
            target=target,
            interval_s=interval_s,
            next_run=first_run,
            enabled=enabled,
            metadata=metadata or {},
        )
        self._heap_push(task)
        return tid

    def cancel(self, task_id: str) -> None:
        """Cancela uma tarefa agendada."""
        self._heap_remove(task_id)

    def enable(self, task_id: str) -> None:
        task = self._index.get(task_id)
        if task is None:
            raise TaskNotFound(f"tarefa agendada não encontrada: {task_id}")
        self._index[task_id] = task.__class__(
            task_id=task.task_id,
            name=task.name,
            kind=task.kind,
            target=task.target,
            interval_s=task.interval_s,
            next_run=task.next_run,
            enabled=True,
            metadata=task.metadata,
        )
        # Reinsere no heap para reposicionar
        heapq.heapify(self._heap)

    def disable(self, task_id: str) -> None:
        task = self._index.get(task_id)
        if task is None:
            raise TaskNotFound(f"tarefa agendada não encontrada: {task_id}")
        self._index[task_id] = task.__class__(
            task_id=task.task_id,
            name=task.name,
            kind=task.kind,
            target=task.target,
            interval_s=task.interval_s,
            next_run=task.next_run,
            enabled=False,
            metadata=task.metadata,
        )
        heapq.heapify(self._heap)

    def list_tasks(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self._index.values()]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._index.get(task_id)
        return task.as_dict() if task else None

    def _record_history(self, entry: dict[str, Any]) -> None:
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._history[-limit:]))

    async def _run_once(self, task: ScheduledTask) -> None:
        started = time.monotonic()
        entry: dict[str, Any] = {
            "task_id": task.task_id,
            "name": task.name,
            "kind": task.kind,
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            if task.kind == "automation":
                result = await self._automation.run(task.target)
                entry["status"] = "ok"
                entry["exit_code"] = result.exit_code
                entry["duration_ms"] = result.duration_ms
                entry["stdout"] = result.stdout
                entry["stderr"] = result.stderr
            else:  # cognitive
                result = await self._cognitive.run(task.target)
                entry["status"] = "ok"
                entry["task_id_result"] = result.task_id
                entry["duration_ms"] = int(
                    (result.completed_at - result.started_at).total_seconds() * 1000
                )
        except ApprovalRequired as exc:
            entry["status"] = "approval_required"
            entry["request_id"] = exc.request_id
            entry["justification"] = exc.justification
        except Exception as exc:  # noqa: BLE001 - scheduler não deve morrer por uma tarefa
            entry["status"] = "error"
            entry["error"] = str(exc)[:512]
        finally:
            entry["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
            entry["duration_s"] = round(time.monotonic() - started, 3)
            self._record_history(entry)

    async def _loop(self) -> None:
        while self._running:
            now = time.monotonic()
            due: list[ScheduledTask] = []

            # Coleta todas as tarefas vencidas
            while self._heap and self._heap[0].next_run <= now:
                task = heapq.heappop(self._heap)
                if task.enabled:
                    due.append(task)

            # Reagenda tarefas periódicas
            for task in due:
                if task.interval_s is not None:
                    rescheduled = task.__class__(
                        task_id=task.task_id,
                        name=task.name,
                        kind=task.kind,
                        target=task.target,
                        interval_s=task.interval_s,
                        next_run=now + task.interval_s,
                        enabled=True,
                        metadata=task.metadata,
                    )
                    self._heap_push(rescheduled)
                else:
                    # One-shot: remove do index
                    self._index.pop(task.task_id, None)

            # Executa tarefas vencidas
            if due:
                await asyncio.gather(*(self._run_once(t) for t in due))

            await asyncio.sleep(self._tick_s)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="nexus-scheduler")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._running


__all__ = [
    "Scheduler",
    "ScheduledTask",
    "SchedulerError",
    "TaskAlreadyExists",
    "TaskNotFound",
]
