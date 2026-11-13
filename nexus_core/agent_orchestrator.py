"""Agent Orchestrator — coordenação de múltiplos agentes.

Responsabilidade: decompor objetivos complexos em tarefas, despachá-las para
agentes especializados, consolidar resultados e gerenciar replanejamento em
caso de falha. Agentes podem discordar; consenso não equivale a correção.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

from .agent_registry import AgentRegistry, AgentRole
from .agent_runtime import (
    AgentRuntime,
    AgentTask,
    AgentExecutionResult,
    AgentExecutionStatus,
)
from .task_dispatcher import TaskDispatcher


@dataclass(frozen=True, slots=True)
class OrchestrationPlan:
    """Plano de orquestração: objetivo + tarefas para agentes."""

    plan_id: str
    objective: str
    tasks: tuple[dict[str, Any], ...]
    required_roles: tuple[AgentRole, ...]
    parallel: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "tasks": [dict(t) for t in self.tasks],
            "required_roles": [r.name for r in self.required_roles],
            "parallel": self.parallel,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """Resultado da orquestração: resultados por tarefa e síntese final."""

    plan_id: str
    objective: str
    task_results: tuple[AgentExecutionResult, ...]
    successful: int
    failed: int
    consolidated_output: dict[str, Any]
    agents_involved: tuple[str, ...]
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "task_results": [r.as_dict() for r in self.task_results],
            "successful": self.successful,
            "failed": self.failed,
            "consolidated_output": dict(self.consolidated_output),
            "agents_involved": list(self.agents_involved),
            "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata),
        }


class AgentOrchestrator:
    """Coordena múltiplos agentes para completar um objetivo complexo."""

    def __init__(
        self,
        registry: AgentRegistry,
        runtime: AgentRuntime,
        dispatcher: TaskDispatcher | None = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._dispatcher = dispatcher or TaskDispatcher(registry, runtime)
        self._plans: dict[str, OrchestrationPlan] = {}
        self._results: dict[str, OrchestrationResult] = {}

    def create_plan(
        self,
        *,
        objective: str,
        tasks: tuple[dict[str, Any], ...],
        required_roles: tuple[AgentRole, ...] = (),
        parallel: bool = True,
        plan_id: str | None = None,
    ) -> OrchestrationPlan:
        """Cria um plano de orquestração (não executa ainda)."""
        import uuid

        pid = plan_id or uuid.uuid4().hex
        plan = OrchestrationPlan(
            plan_id=pid,
            objective=objective,
            tasks=tasks,
            required_roles=required_roles,
            parallel=parallel,
        )
        self._plans[pid] = plan
        return plan

    async def execute_plan(self, plan_id: str) -> OrchestrationResult:
        """Executa um plano de orquestração, despachando tarefas para agentes."""
        import time

        plan = self._plans.get(plan_id)
        if plan is None:
            raise KeyError(f"plan not found: {plan_id}")

        started = time.monotonic()
        agents_involved: list[str] = []

        if plan.parallel:
            results = await self._dispatcher.dispatch_parallel(list(plan.tasks))
        else:
            results = []
            for spec in plan.tasks:
                agent_id, task_id = await self._dispatcher.dispatch(
                    description=str(spec.get("description", "task")),
                    input=dict(spec.get("input", {})),
                    required_capabilities=tuple(spec.get("required_capabilities", ())),
                    required_tools=tuple(spec.get("required_tools", ())),
                    preferred_role=(
                        AgentRole[spec["preferred_role"]]
                        if spec.get("preferred_role")
                        else None
                    ),
                    timeout_s=float(spec.get("timeout_s", 300.0)),
                )
                result = await self._runtime.execute_task(task_id)
                results.append((agent_id, task_id, result))

        task_results: list[AgentExecutionResult] = []
        successful = 0
        failed = 0
        for agent_id, task_id, result in results:
            if agent_id:
                agents_involved.append(agent_id)
            task_results.append(result)
            if result.status == AgentExecutionStatus.COMPLETED:
                successful += 1
            else:
                failed += 1

        # Consolida saída: combina outputs de tarefas bem-sucedidas. Um mesmo
        # agente pode executar várias tarefas; nesse caso os outputs são
        # agrupados em lista para não sobrescrever resultados anteriores.
        consolidated: dict[str, Any] = {"task_count": len(task_results)}
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for r in task_results:
            if r.status == AgentExecutionStatus.COMPLETED and r.output:
                by_agent.setdefault(r.agent_id, []).append(r.output)
        for agent_id, outputs in by_agent.items():
            consolidated[agent_id] = outputs[0] if len(outputs) == 1 else outputs

        duration_ms = (time.monotonic() - started) * 1000.0

        orchestration = OrchestrationResult(
            plan_id=plan.plan_id,
            objective=plan.objective,
            task_results=tuple(task_results),
            successful=successful,
            failed=failed,
            consolidated_output=consolidated,
            agents_involved=tuple(sorted(set(agents_involved))),
            duration_ms=duration_ms,
            metadata={"parallel": plan.parallel},
        )
        self._results[plan_id] = orchestration
        return orchestration

    def get_plan(self, plan_id: str) -> OrchestrationPlan | None:
        return self._plans.get(plan_id)

    def get_result(self, plan_id: str) -> OrchestrationResult | None:
        return self._results.get(plan_id)

    def list_plans(self) -> list[OrchestrationPlan]:
        return list(self._plans.values())


__all__ = [
    "AgentOrchestrator",
    "OrchestrationPlan",
    "OrchestrationResult",
]
