"""Planner Engine — conversão de objetivos em planos executáveis.

Responsabilidade: transformar um objetivo em um plano contendo:

* ID único
* Objetivo
* Etapas
* Dependências
* Agente/executor atribuído
* Ferramentas requeridas
* Estimativas de recurso
* Classificação de risco
* Pré-condições
* Critérios de sucesso
* Tratamento de falha
* Estado de conclusão

Suporte a cancelamento, retomada, retentativas e replanejamento.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Mapping


class PlanStepStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    SKIPPED = auto()


class PlanStatus(Enum):
    DRAFT = auto()
    ACTIVE = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    SUSPENDED = auto()  # aguardando aprovação ou recurso


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Uma etapa do plano."""

    step_id: str
    description: str
    action_type: str  # 'reason', 'memory', 'automation', 'simulation'
    action_id: str | None = None  # tool_id ou referência
    args: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()  # step_ids
    assigned_agent: str | None = None
    required_tools: tuple[str, ...] = ()
    resource_estimate: str = "low"
    risk_classification: str = "low"
    preconditions: tuple[str, ...] = ()
    success_criteria: str = ""
    failure_handling: str = "retry"
    status: PlanStepStatus = PlanStepStatus.PENDING
    result: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "action_type": self.action_type,
            "action_id": self.action_id,
            "args": list(self.args),
            "dependencies": list(self.dependencies),
            "assigned_agent": self.assigned_agent,
            "required_tools": list(self.required_tools),
            "resource_estimate": self.resource_estimate,
            "risk_classification": self.risk_classification,
            "preconditions": list(self.preconditions),
            "success_criteria": self.success_criteria,
            "failure_handling": self.failure_handling,
            "status": self.status.name,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanStep":
        return cls(
            step_id=str(data["step_id"]),
            description=str(data.get("description", "")),
            action_type=str(data.get("action_type", "reason")),
            action_id=data.get("action_id"),
            args=tuple(data.get("args", [])),
            dependencies=tuple(data.get("dependencies", [])),
            assigned_agent=data.get("assigned_agent"),
            required_tools=tuple(data.get("required_tools", [])),
            resource_estimate=str(data.get("resource_estimate", "low")),
            risk_classification=str(data.get("risk_classification", "low")),
            preconditions=tuple(data.get("preconditions", [])),
            success_criteria=str(data.get("success_criteria", "")),
            failure_handling=str(data.get("failure_handling", "retry")),
            status=PlanStepStatus[str(data.get("status", "PENDING"))],
            result=data.get("result"),
            error=data.get("error"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """Um plano executável completo."""

    plan_id: str
    objective: str
    steps: tuple[PlanStep, ...]
    dependencies: tuple[str, ...] = ()
    assigned_agent: str | None = None
    required_tools: tuple[str, ...] = ()
    resource_estimate: str = "low"
    risk_classification: str = "low"
    preconditions: tuple[str, ...] = ()
    success_criteria: str = ""
    failure_handling: str = "replan"
    status: PlanStatus = PlanStatus.DRAFT
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    cancellation_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "steps": [s.as_dict() for s in self.steps],
            "dependencies": list(self.dependencies),
            "assigned_agent": self.assigned_agent,
            "required_tools": list(self.required_tools),
            "resource_estimate": self.resource_estimate,
            "risk_classification": self.risk_classification,
            "preconditions": list(self.preconditions),
            "success_criteria": self.success_criteria,
            "failure_handling": self.failure_handling,
            "status": self.status.name,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancellation_requested": self.cancellation_requested,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Plan":
        return cls(
            plan_id=str(data["plan_id"]),
            objective=str(data.get("objective", "")),
            steps=tuple(PlanStep.from_dict(s) for s in data.get("steps", [])),
            dependencies=tuple(data.get("dependencies", [])),
            assigned_agent=data.get("assigned_agent"),
            required_tools=tuple(data.get("required_tools", [])),
            resource_estimate=str(data.get("resource_estimate", "low")),
            risk_classification=str(data.get("risk_classification", "low")),
            preconditions=tuple(data.get("preconditions", [])),
            success_criteria=str(data.get("success_criteria", "")),
            failure_handling=str(data.get("failure_handling", "replan")),
            status=PlanStatus[str(data.get("status", "DRAFT"))],
            created_at=data.get(
                "created_at", datetime.now(timezone.utc).isoformat()
            ),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            cancellation_requested=bool(data.get("cancellation_requested", False)),
            metadata=dict(data.get("metadata", {})),
        )

    def step_by_id(self, step_id: str) -> PlanStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def is_complete(self) -> bool:
        return all(
            s.status in (
                PlanStepStatus.COMPLETED,
                PlanStepStatus.SKIPPED,
                PlanStepStatus.CANCELLED,
            )
            for s in self.steps
        )

    def next_pending_step(self) -> PlanStep | None:
        """Retorna a próxima etapa cujas dependências estão satisfeitas."""
        completed_ids = {
            s.step_id
            for s in self.steps
            if s.status == PlanStepStatus.COMPLETED
        }
        for step in self.steps:
            if step.status != PlanStepStatus.PENDING:
                continue
            if all(dep in completed_ids for dep in step.dependencies):
                return step
        return None


class PlannerEngine:
    """Converte objetivos em planos executáveis."""

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}

    def create_plan(
        self,
        objective: str,
        *,
        steps: tuple[PlanStep, ...] = (),
        assigned_agent: str | None = None,
        required_tools: tuple[str, ...] = (),
        risk_classification: str = "low",
        plan_id: str | None = None,
    ) -> Plan:
        """Cria um novo plano (rascunho)."""
        pid = plan_id or uuid.uuid4().hex
        plan = Plan(
            plan_id=pid,
            objective=objective,
            steps=steps,
            assigned_agent=assigned_agent,
            required_tools=required_tools,
            risk_classification=risk_classification,
            status=PlanStatus.DRAFT,
        )
        self._plans[pid] = plan
        return plan

    def generate_plan(
        self,
        objective: str,
        *,
        intent_capabilities: tuple[str, ...] = (),
        risk_classification: str = "low",
    ) -> Plan:
        """Gera um plano automaticamente a partir do objetivo e das capacidades
        da intenção. Produz um plano com etapas genéricas baseadas no tipo de
        ação necessária."""
        steps: list[PlanStep] = []
        step_index = 0

        # Etapa 1: raciocínio/entendimento.
        steps.append(
            PlanStep(
                step_id=f"step-{step_index}",
                description=f"Understand and refine objective: {objective}",
                action_type="reason",
                action_id=None,
                risk_classification="low",
                success_criteria="Objective is clear and well-defined.",
                failure_handling="retry",
            )
        )
        step_index += 1

        # Etapa 2: recuperação de contexto/memória se relevante.
        if "memory.retrieval" in intent_capabilities:
            steps.append(
                PlanStep(
                    step_id=f"step-{step_index}",
                    description="Retrieve relevant context from memory",
                    action_type="memory",
                    action_id=None,
                    dependencies=(f"step-{step_index - 1}",),
                    risk_classification="low",
                    success_criteria="Relevant memories retrieved.",
                    failure_handling="skip",
                )
            )
            step_index += 1

        # Etapa 3: execução (se houver ferramentas/capacidades de automação).
        if "automation" in intent_capabilities:
            steps.append(
                PlanStep(
                    step_id=f"step-{step_index}",
                    description="Execute required operation",
                    action_type="automation",
                    action_id=None,
                    dependencies=tuple(
                        f"step-{i}" for i in range(step_index)
                    ),
                    risk_classification=risk_classification,
                    success_criteria="Operation executed successfully.",
                    failure_handling="retry",
                )
            )
            step_index += 1

        # Etapa 4: síntese/conclusão.
        steps.append(
            PlanStep(
                step_id=f"step-{step_index}",
                description="Synthesize final answer",
                action_type="reason",
                action_id=None,
                dependencies=tuple(f"step-{i}" for i in range(step_index)),
                risk_classification="low",
                success_criteria="Final answer provided to user.",
                failure_handling="retry",
            )
        )

        return self.create_plan(
            objective=objective,
            steps=tuple(steps),
            risk_classification=risk_classification,
        )

    def get_plan(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def save_plan(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan

    def start_plan(self, plan_id: str) -> bool:
        plan = self._plans.get(plan_id)
        if plan is None or plan.status != PlanStatus.DRAFT:
            return False
        updated = plan.__class__(
            plan_id=plan.plan_id,
            objective=plan.objective,
            steps=plan.steps,
            dependencies=plan.dependencies,
            assigned_agent=plan.assigned_agent,
            required_tools=plan.required_tools,
            resource_estimate=plan.resource_estimate,
            risk_classification=plan.risk_classification,
            preconditions=plan.preconditions,
            success_criteria=plan.success_criteria,
            failure_handling=plan.failure_handling,
            status=PlanStatus.ACTIVE,
            created_at=plan.created_at,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=plan.completed_at,
            cancellation_requested=plan.cancellation_requested,
            metadata=plan.metadata,
        )
        self._plans[plan_id] = updated
        return True

    def complete_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        result: str | None = None,
        error: str | None = None,
        status: PlanStepStatus = PlanStepStatus.COMPLETED,
    ) -> bool:
        plan = self._plans.get(plan_id)
        if plan is None:
            return False
        step = plan.step_by_id(step_id)
        if step is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        updated_step = step.__class__(
            step_id=step.step_id,
            description=step.description,
            action_type=step.action_type,
            action_id=step.action_id,
            args=step.args,
            dependencies=step.dependencies,
            assigned_agent=step.assigned_agent,
            required_tools=step.required_tools,
            resource_estimate=step.resource_estimate,
            risk_classification=step.risk_classification,
            preconditions=step.preconditions,
            success_criteria=step.success_criteria,
            failure_handling=step.failure_handling,
            status=status,
            result=result,
            error=error,
            started_at=step.started_at or now,
            completed_at=now if status in (PlanStepStatus.COMPLETED, PlanStepStatus.FAILED) else step.completed_at,
            attempts=step.attempts + 1,
            max_attempts=step.max_attempts,
            metadata=step.metadata,
        )
        # Reconstrói o plano com a etapa atualizada.
        new_steps = tuple(
            updated_step if s.step_id == step_id else s for s in plan.steps
        )
        new_plan = plan.__class__(
            plan_id=plan.plan_id,
            objective=plan.objective,
            steps=new_steps,
            dependencies=plan.dependencies,
            assigned_agent=plan.assigned_agent,
            required_tools=plan.required_tools,
            resource_estimate=plan.resource_estimate,
            risk_classification=plan.risk_classification,
            preconditions=plan.preconditions,
            success_criteria=plan.success_criteria,
            failure_handling=plan.failure_handling,
            status=plan.status,
            created_at=plan.created_at,
            started_at=plan.started_at,
            completed_at=plan.completed_at,
            cancellation_requested=plan.cancellation_requested,
            metadata=plan.metadata,
        )
        self._plans[plan_id] = new_plan
        return True

    def cancel_plan(self, plan_id: str) -> bool:
        plan = self._plans.get(plan_id)
        if plan is None or plan.status in (
            PlanStatus.COMPLETED,
            PlanStatus.CANCELLED,
            PlanStatus.FAILED,
        ):
            return False
        updated = plan.__class__(
            plan_id=plan.plan_id,
            objective=plan.objective,
            steps=plan.steps,
            dependencies=plan.dependencies,
            assigned_agent=plan.assigned_agent,
            required_tools=plan.required_tools,
            resource_estimate=plan.resource_estimate,
            risk_classification=plan.risk_classification,
            preconditions=plan.preconditions,
            success_criteria=plan.success_criteria,
            failure_handling=plan.failure_handling,
            status=PlanStatus.CANCELLED,
            created_at=plan.created_at,
            started_at=plan.started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            cancellation_requested=True,
            metadata=plan.metadata,
        )
        self._plans[plan_id] = updated
        return True

    def list_plans(self) -> list[Plan]:
        return list(self._plans.values())

    def retry_step(self, plan_id: str, step_id: str) -> bool:
        """Marca uma etapa falhada para nova tentativa (se ainda houver tentativas)."""
        plan = self._plans.get(plan_id)
        if plan is None:
            return False
        step = plan.step_by_id(step_id)
        if step is None or step.status != PlanStepStatus.FAILED:
            return False
        if step.attempts >= step.max_attempts:
            return False
        # Reabrir a etapa não é uma nova execução: ``attempts`` só deve ser
        # incrementado quando a etapa efetivamente terminar de novo.
        updated_step = replace(
            step,
            status=PlanStepStatus.PENDING,
            result=None,
            error=None,
            started_at=None,
            completed_at=None,
        )
        new_plan = replace(
            plan,
            steps=tuple(
                updated_step if s.step_id == step_id else s for s in plan.steps
            ),
        )
        self._plans[plan_id] = new_plan
        return True


__all__ = [
    "PlannerEngine",
    "Plan",
    "PlanStep",
    "PlanStatus",
    "PlanStepStatus",
]
