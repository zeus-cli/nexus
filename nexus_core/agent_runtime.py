"""Agent Runtime — execução isolada de agentes.

Responsabilidade: executar agentes dentro de limites de permissão e recurso
declarados, garantindo que agentes nunca bypassem o Execution Gate.

Cada execução de agente é rastreada com contexto de tarefa compartilhado e
isolamento de falhas (uma falha de agente não derruba os demais).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping

from .agent_manifest import AgentManifest, AgentState
from .agent_registry import AgentRegistry
from .execution_gate import ExecutionGate, GateDecision


class AgentExecutionStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    TIMEOUT = auto()


@dataclass(frozen=True, slots=True)
class AgentTask:
    """Tarefa atribuída a um agente."""

    task_id: str
    agent_id: str
    description: str
    input: dict[str, Any]
    required_tools: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    timeout_s: float = 300.0
    status: AgentExecutionStatus = AgentExecutionStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    attempts: int = 0
    max_attempts: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "description": self.description,
            "input": dict(self.input),
            "required_tools": list(self.required_tools),
            "required_capabilities": list(self.required_capabilities),
            "timeout_s": self.timeout_s,
            "status": self.status.name,
            "result": dict(self.result) if self.result else None,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Resultado da execução de um agente."""

    task_id: str
    agent_id: str
    status: AgentExecutionStatus
    output: dict[str, Any]
    duration_ms: float
    error: str | None = None
    execution_id: str | None = None  # ID da execução no Execution Gate

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "status": self.status.name,
            "output": dict(self.output),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "execution_id": self.execution_id,
        }


class AgentRuntimeError(Exception):
    """Erro no runtime de agentes."""


class AgentNotAuthorized(AgentRuntimeError):
    """Agente não autorizado para a operação solicitada."""


class AgentRuntime:
    """Executa agentes isoladamente, respeitando permissões e o Execution Gate."""

    def __init__(
        self,
        registry: AgentRegistry,
        execution_gate: ExecutionGate | None = None,
    ) -> None:
        self._registry = registry
        self._execution_gate = execution_gate
        self._tasks: dict[str, AgentTask] = {}
        self._results: dict[str, AgentExecutionResult] = {}
        self._handlers: dict[str, Callable[[AgentTask], Any]] = {}

    def register_handler(
        self, agent_id: str, handler: Callable[[AgentTask], Any]
    ) -> None:
        """Registra o handler (função de execução) de um agente."""
        self._handlers[agent_id] = handler

    def create_task(
        self,
        *,
        agent_id: str,
        description: str,
        input: dict[str, Any],
        required_tools: tuple[str, ...] = (),
        required_capabilities: tuple[str, ...] = (),
        timeout_s: float = 300.0,
    ) -> str:
        """Cria uma tarefa para um agente (não a executa ainda)."""
        manifest = self._registry.get_manifest(agent_id)
        if manifest is None:
            raise AgentRuntimeError(f"agent not registered: {agent_id}")
        if manifest.role is None:
            raise AgentRuntimeError(f"agent has no role: {agent_id}")

        task_id = uuid.uuid4().hex
        task = AgentTask(
            task_id=task_id,
            agent_id=agent_id,
            description=description,
            input=input,
            required_tools=required_tools,
            required_capabilities=required_capabilities,
            timeout_s=timeout_s,
        )
        self._tasks[task_id] = task
        self._registry.set_state(agent_id, AgentState.IDLE)
        return task_id

    async def execute_task(self, task_id: str) -> AgentExecutionResult:
        """Executa uma tarefa de agente com isolamento e gate de execução.

        O agente só pode usar ferramentas para as quais tem acesso declarado,
        e cada uso de ferramenta passa pelo Execution Gate.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise AgentRuntimeError(f"task not found: {task_id}")
        if task.status != AgentExecutionStatus.PENDING:
            raise AgentRuntimeError(
                f"task {task_id} not in pending state: {task.status.name}"
            )

        manifest = self._registry.get_manifest(task.agent_id)
        if manifest is None:
            raise AgentRuntimeError(f"agent not registered: {task.agent_id}")

        # Atualiza estado para RUNNING.
        started = time.monotonic()
        task = AgentTask(
            task_id=task.task_id,
            agent_id=task.agent_id,
            description=task.description,
            input=task.input,
            required_tools=task.required_tools,
            required_capabilities=task.required_capabilities,
            timeout_s=task.timeout_s,
            status=AgentExecutionStatus.RUNNING,
            attempts=task.attempts + 1,
            max_attempts=task.max_attempts,
            metadata=task.metadata,
            started_at=started,
        )
        self._tasks[task_id] = task
        self._registry.set_state(task.agent_id, AgentState.BUSY)

        execution_id = None
        try:
            # Valida acesso às ferramentas requeridas (dentro do try para
            # retornar FAILED em vez de levantar exceção não tratada).
            for tool_id in task.required_tools:
                if not manifest.has_tool_access(tool_id):
                    raise AgentNotAuthorized(
                        f"agent {task.agent_id} not authorized for tool {tool_id}"
                    )

            # Valida capacidades requeridas.
            for cap in task.required_capabilities:
                if not manifest.has_capability(cap):
                    raise AgentNotAuthorized(
                        f"agent {task.agent_id} lacks capability {cap}"
                    )
            # Se houver Execution Gate, autoriza a execução do agente.
            if self._execution_gate is not None:
                gate = await self._execution_gate.authorize(
                    component_id=f"agent-{task.agent_id}",
                    tool_id="agent.execute",
                    params=task.input,
                    task_id=task.task_id,
                    justification=f"agent task: {task.description}",
                )
                if gate.decision == GateDecision.DENIED:
                    raise AgentNotAuthorized(gate.reason)
                execution_id = gate.execution_id

            # Executa o handler do agente com timeout.
            handler = self._handlers.get(task.agent_id)
            if handler is None:
                raise AgentRuntimeError(
                    f"no handler registered for agent {task.agent_id}"
                )

            if asyncio.iscoroutinefunction(handler):
                output = await asyncio.wait_for(
                    handler(task), timeout=task.timeout_s
                )
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(handler, task), timeout=task.timeout_s
                )

            if not isinstance(output, dict):
                output = {"result": output}

            duration_ms = (time.monotonic() - started) * 1000.0
            result = AgentExecutionResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                status=AgentExecutionStatus.COMPLETED,
                output=output,
                duration_ms=duration_ms,
                execution_id=execution_id,
            )
            task = AgentTask(
                task_id=task.task_id,
                agent_id=task.agent_id,
                description=task.description,
                input=task.input,
                required_tools=task.required_tools,
                required_capabilities=task.required_capabilities,
                timeout_s=task.timeout_s,
                status=AgentExecutionStatus.COMPLETED,
                result=output,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
                started_at=task.started_at,
                completed_at=time.monotonic(),
            )
            self._tasks[task_id] = task
            self._results[task_id] = result
            self._registry.set_state(task.agent_id, AgentState.IDLE)
            return result

        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - started) * 1000.0
            result = AgentExecutionResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                status=AgentExecutionStatus.TIMEOUT,
                output={},
                duration_ms=duration_ms,
                error="task timed out",
                execution_id=execution_id,
            )
            task = AgentTask(
                task_id=task.task_id,
                agent_id=task.agent_id,
                description=task.description,
                input=task.input,
                required_tools=task.required_tools,
                required_capabilities=task.required_capabilities,
                timeout_s=task.timeout_s,
                status=AgentExecutionStatus.TIMEOUT,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
                started_at=task.started_at,
                completed_at=time.monotonic(),
            )
            self._tasks[task_id] = task
            self._results[task_id] = result
            self._registry.set_state(task.agent_id, AgentState.ERROR)
            return result

        except AgentNotAuthorized as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            result = AgentExecutionResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                status=AgentExecutionStatus.FAILED,
                output={},
                duration_ms=duration_ms,
                error=str(exc),
                execution_id=execution_id,
            )
            task = AgentTask(
                task_id=task.task_id,
                agent_id=task.agent_id,
                description=task.description,
                input=task.input,
                required_tools=task.required_tools,
                required_capabilities=task.required_capabilities,
                timeout_s=task.timeout_s,
                status=AgentExecutionStatus.FAILED,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
                started_at=task.started_at,
                completed_at=time.monotonic(),
                error=str(exc),
            )
            self._tasks[task_id] = task
            self._results[task_id] = result
            self._registry.set_state(task.agent_id, AgentState.ERROR)
            return result

        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - started) * 1000.0
            result = AgentExecutionResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                status=AgentExecutionStatus.FAILED,
                output={},
                duration_ms=duration_ms,
                error=str(exc)[:512],
                execution_id=execution_id,
            )
            task = AgentTask(
                task_id=task.task_id,
                agent_id=task.agent_id,
                description=task.description,
                input=task.input,
                required_tools=task.required_tools,
                required_capabilities=task.required_capabilities,
                timeout_s=task.timeout_s,
                status=AgentExecutionStatus.FAILED,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                metadata=task.metadata,
                started_at=task.started_at,
                completed_at=time.monotonic(),
                error=str(exc)[:512],
            )
            self._tasks[task_id] = task
            self._results[task_id] = result
            self._registry.set_state(task.agent_id, AgentState.ERROR)
            return result

    def get_task(self, task_id: str) -> AgentTask | None:
        return self._tasks.get(task_id)

    def get_result(self, task_id: str) -> AgentExecutionResult | None:
        return self._results.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status in (
            AgentExecutionStatus.COMPLETED,
            AgentExecutionStatus.FAILED,
            AgentExecutionStatus.CANCELLED,
        ):
            return False
        task = AgentTask(
            task_id=task.task_id,
            agent_id=task.agent_id,
            description=task.description,
            input=task.input,
            required_tools=task.required_tools,
            required_capabilities=task.required_capabilities,
            timeout_s=task.timeout_s,
            status=AgentExecutionStatus.CANCELLED,
            attempts=task.attempts,
            max_attempts=task.max_attempts,
            metadata=task.metadata,
            started_at=task.started_at,
            completed_at=time.monotonic(),
        )
        self._tasks[task_id] = task
        self._registry.set_state(task.agent_id, AgentState.IDLE)
        return True

    def list_tasks(self) -> list[AgentTask]:
        return list(self._tasks.values())


__all__ = [
    "AgentRuntime",
    "AgentTask",
    "AgentExecutionResult",
    "AgentExecutionStatus",
    "AgentRuntimeError",
    "AgentNotAuthorized",
]
