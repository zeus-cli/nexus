"""Task Dispatcher — despacho de tarefas para agentes disponíveis.

Responsabilidade: atribuir tarefas a agentes com base em capacidades, carga de
trabalho atual e limites de recurso, suportando execução paralela e isolamento
de falhas.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping

from .agent_registry import AgentRegistry, AgentRole, AgentState
from .agent_runtime import (
    AgentRuntime,
    AgentTask,
    AgentExecutionResult,
    AgentExecutionStatus,
)


class TaskDispatcher:
    """Despacha tarefas para agentes adequados."""

    def __init__(self, registry: AgentRegistry, runtime: AgentRuntime) -> None:
        self._registry = registry
        self._runtime = runtime

    def find_agent_for_task(
        self,
        *,
        required_capabilities: tuple[str, ...] = (),
        required_tools: tuple[str, ...] = (),
        preferred_role: AgentRole | None = None,
        exclude_busy: bool = True,
    ) -> str | None:
        """Encontra um agente adequado para a tarefa."""
        candidates = []
        for manifest, state in self._registry.list_agents():
            if exclude_busy and state == AgentState.BUSY:
                continue
            if state not in (AgentState.IDLE, AgentState.REGISTERED):
                continue
            if preferred_role is not None and manifest.role != preferred_role:
                continue
            if required_capabilities and not all(
                manifest.has_capability(c) for c in required_capabilities
            ):
                continue
            if required_tools and not all(
                manifest.has_tool_access(t) for t in required_tools
            ):
                continue
            candidates.append(manifest.agent_id)
        return candidates[0] if candidates else None

    async def dispatch(
        self,
        *,
        description: str,
        input: dict[str, Any],
        required_capabilities: tuple[str, ...] = (),
        required_tools: tuple[str, ...] = (),
        preferred_role: AgentRole | None = None,
        timeout_s: float = 300.0,
    ) -> tuple[str, str]:
        """Despacha uma tarefa e retorna (agent_id, task_id).

        Levanta RuntimeError se nenhum agente estiver disponível.
        """
        agent_id = self.find_agent_for_task(
            required_capabilities=required_capabilities,
            required_tools=required_tools,
            preferred_role=preferred_role,
        )
        if agent_id is None:
            raise RuntimeError("no available agent for task")
        task_id = self._runtime.create_task(
            agent_id=agent_id,
            description=description,
            input=input,
            required_tools=required_tools,
            required_capabilities=required_capabilities,
            timeout_s=timeout_s,
        )
        return agent_id, task_id

    async def dispatch_parallel(
        self,
        tasks: list[Mapping[str, Any]],
    ) -> list[tuple[str, str, AgentExecutionResult]]:
        """Despacha múltiplas tarefas em paralelo e aguarda todas.

        Cada item em ``tasks`` deve conter as chaves de ``dispatch``.
        Retorna (agent_id, task_id, result) para cada tarefa, preservando ordem.
        Falhas de tarefa individual não cancelam as demais (isolamento de falhas).
        """

        async def _run_one(task_spec: Mapping[str, Any]) -> tuple[str, str, AgentExecutionResult]:
            agent_id, task_id = await self.dispatch(
                description=str(task_spec.get("description", "task")),
                input=dict(task_spec.get("input", {})),
                required_capabilities=tuple(task_spec.get("required_capabilities", ())),
                required_tools=tuple(task_spec.get("required_tools", ())),
                preferred_role=(
                    AgentRole[task_spec["preferred_role"]]
                    if task_spec.get("preferred_role")
                    else None
                ),
                timeout_s=float(task_spec.get("timeout_s", 300.0)),
            )
            result = await self._runtime.execute_task(task_id)
            return agent_id, task_id, result

        results = await asyncio.gather(
            *(_run_one(spec) for spec in tasks), return_exceptions=True
        )
        out: list[tuple[str, str, AgentExecutionResult]] = []
        for item in results:
            if isinstance(item, Exception):
                out.append(("", "", AgentExecutionResult(
                    task_id="", agent_id="",
                    status=AgentExecutionStatus.FAILED,
                    output={},
                    duration_ms=0.0,
                    error=str(item)[:512],
                )))
            else:
                out.append(item)
        return out


__all__ = ["TaskDispatcher"]
