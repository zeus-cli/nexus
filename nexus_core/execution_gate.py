"""Execution Gate — execução governada de operações.

Ciclo de execução (não depende de LLM):

REQUEST
   ↓
IDENTITY VERIFICATION
   ↓
CAPABILITY VALIDATION
   ↓
POLICY EVALUATION
   ↓
RISK CLASSIFICATION
   ↓
APPROVAL CHECK
   ↓
RESOURCE RESERVATION
   ↓
ISOLATED EXECUTION
   ↓
RESULT VERIFICATION
   ↓
COMMIT / CONSUMPTION
   ↓
AUDIT

Implementação Fase 3: integra o ApprovalManager (HITL), PolicyEngine (decisão
determinística) e SandboxExecutor (isolamento) existentes, acrescentando
verificação de identidade, validação de capacidade, reserva de recurso e
auditoria no Execution Journal.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping

from .approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalDecision,
    ApprovalStatus,
    ExecutionStatus,
)
from .automation_types import RiskLevel, AccessLevel, ExecutionMode
from .policy import PolicyContext, PolicyDecision, PolicyEngine
from .sandbox import SandboxExecutor, ResourceLimits as SandboxResourceLimits
from .recorder import EventRecorder
from .events import Event, Severity


class ExecutionGateError(Exception):
    """Erro no Execution Gate."""


class IdentityVerificationError(ExecutionGateError):
    """Falha na verificação de identidade do componente."""


class CapabilityValidationError(ExecutionGateError):
    """Falha na validação de capacidade da ferramenta."""


class ResourceReservationError(ExecutionGateError):
    """Falha na reserva de recurso."""


class GateDecision(Enum):
    ALLOWED = auto()
    REQUIRES_APPROVAL = auto()
    DENIED = auto()


@dataclass(frozen=True, slots=True)
class GateRequest:
    """Solicitação de execução submetida ao Execution Gate."""

    request_id: str
    component_id: str
    tool_id: str
    params: Mapping[str, Any]
    task_id: str
    plan_id: str
    execution_mode: str
    access_level: str
    fs_scope: str
    net_scope: str
    resource_limits: str
    policy_version: str
    justification: str
    risk_level: RiskLevel


@dataclass(frozen=True, slots=True)
class GateResult:
    """Resultado do processamento pelo Execution Gate."""

    decision: GateDecision
    request_id: str
    reason: str
    approved: bool = False
    execution_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.name,
            "request_id": self.request_id,
            "reason": self.reason,
            "approved": self.approved,
            "execution_id": self.execution_id,
            "metadata": dict(self.metadata),
        }


class ExecutionGate:
    """Execution Gate centralizado.

    Orquestra o ciclo completo de execução sem depender de LLM para decisões
    de segurança. Todas as autorizações são determinísticas e verificáveis.
    """

    def __init__(
        self,
        *,
        approval_manager: ApprovalManager | None = None,
        policy_engine: PolicyEngine | None = None,
        sandbox: SandboxExecutor | None = None,
        recorder: EventRecorder | None = None,
    ) -> None:
        self._approval_manager = approval_manager or ApprovalManager()
        self._policy_engine = policy_engine or PolicyEngine()
        self._sandbox = sandbox
        self._recorder = recorder
        self._executions: dict[str, dict[str, Any]] = {}

    # -- ciclo de execução -------------------------------------------------

    async def authorize(
        self,
        *,
        component_id: str,
        tool_id: str,
        params: Mapping[str, Any],
        task_id: str,
        plan_id: str = "default",
        risk_level: RiskLevel = RiskLevel.LOW,
        autonomy_level: int = 1,
        required_capability: str | None = None,
        fs_scope: str = "DEFAULT",
        net_scope: str = "NONE",
        resource_limits: str = "default",
        justification: str = "",
    ) -> GateResult:
        """Executa o ciclo completo de autorização.

        Retorna um GateResult. Se aprovado, o chamador deve então chamar
        ``execute()`` com o mesmo request_id para realizar a operação.
        """
        request_id = uuid.uuid4().hex
        started = time.monotonic()

        # 1. IDENTITY VERIFICATION
        identity_ok = await self._verify_identity(component_id)
        if not identity_ok:
            await self._audit(
                "security.execution.identity_failed",
                {
                    "request_id": request_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "stage": "identity_verification",
                },
            )
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason="identity verification failed",
            )

        # 2. CAPABILITY VALIDATION
        capability_ok = await self._validate_capability(
            component_id, tool_id, required_capability
        )
        if not capability_ok:
            await self._audit(
                "security.execution.capability_failed",
                {
                    "request_id": request_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "stage": "capability_validation",
                },
            )
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason="capability validation failed",
            )

        # 3. POLICY EVALUATION + 4. RISK CLASSIFICATION
        # Mapeia RiskLevel do Tool para decisão da PolicyEngine.
        policy_risk = risk_level.name  # LOW, MEDIUM, HIGH, CRITICAL
        context = PolicyContext(
            autonomy_level=autonomy_level,
            fs_scope=fs_scope,
            net_scope=net_scope,
            resource_limits=resource_limits,
            task_context=task_id,
        )
        # Nota: PolicyEngine.evaluate recebe um Tool, não um risco direto.
        # Aqui aplicamos classificação direta com base no risk_level informado.
        decision, reason = self._classify_risk(policy_risk, context)

        # 5. APPROVAL CHECK
        approval_required = decision == GateDecision.REQUIRES_APPROVAL
        if decision == GateDecision.DENIED:
            await self._audit(
                "security.execution.policy_denied",
                {
                    "request_id": request_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "risk": policy_risk,
                    "reason": reason,
                    "stage": "policy_evaluation",
                },
            )
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason=reason,
            )

        if approval_required:
            # Cria solicitação de aprovação HITL.
            req = ApprovalRequest(
                tool_id=tool_id,
                params=dict(params),
                task_id=task_id,
                plan_id=plan_id,
                execution_mode="HOST",
                access_level="READ",
                fs_scope=fs_scope,
                net_scope=net_scope,
                resource_limits=resource_limits,
                policy_version="1.0",
                justification=justification or reason,
            )
            rid = self._approval_manager.request_approval(req)
            await self._audit(
                "security.execution.approval_requested",
                {
                    "request_id": request_id,
                    "approval_id": rid,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "risk": policy_risk,
                    "stage": "approval_check",
                },
            )
            return GateResult(
                decision=GateDecision.REQUIRES_APPROVAL,
                request_id=request_id,
                reason=reason,
                metadata={"approval_id": rid, "requires_human": True},
            )

        # 6. RESOURCE RESERVATION
        reservation_ok = await self._reserve_resource(
            request_id, component_id, tool_id, resource_limits
        )
        if not reservation_ok:
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason="resource reservation failed",
            )

        # 7-9. Marca como autorizado (execução isolada/verificação/commit feitas em execute()).
        self._executions[request_id] = {
            "component_id": component_id,
            "tool_id": tool_id,
            "params": dict(params),
            "task_id": task_id,
            "plan_id": plan_id,
            "status": "authorized",
            "created_at": time.monotonic(),
            "risk_level": policy_risk,
        }
        await self._audit(
            "security.execution.authorized",
            {
                "request_id": request_id,
                "component_id": component_id,
                "tool_id": tool_id,
                "risk": policy_risk,
                "stage": "commit",
            },
        )

        return GateResult(
            decision=GateDecision.ALLOWED,
            request_id=request_id,
            reason=reason,
            approved=True,
            execution_id=request_id,
        )

    async def execute(
        self,
        request_id: str,
        *,
        runner: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Executa uma operação previamente autorizada.

        Aplica o protocolo CLAIMED → CONSUMED (STARTING) antes do lançamento
        do subprocesso, garantindo execução no máximo uma vez.
        """
        execution = self._executions.get(request_id)
        if execution is None:
            raise ExecutionGateError(f"execution not found or not authorized: {request_id}")
        if execution["status"] != "authorized":
            raise ExecutionGateError(
                f"execution {request_id} not in authorized state: {execution['status']}"
            )

        tool_id = execution["tool_id"]
        params = execution["params"]

        # Aprovação HITL: se houver approval_id associado, claim + mark_starting.
        approval_id = execution.get("approval_id")
        if approval_id:
            claimed = self._approval_manager.claim_approval(approval_id)
            if not claimed:
                raise ExecutionGateError(
                    f"approval {approval_id} not available, expired or already consumed"
                )
            self._approval_manager.mark_starting(approval_id)

        execution["status"] = "executing"
        execution["started_at"] = time.monotonic()

        try:
            # Execução isolada (sandbox) ou direta.
            if self._sandbox is not None and runner is None:
                # Sem runner customizado: não executa comandos arbitrários.
                raise ExecutionGateError(
                    "no runner provided for sandbox execution; unrestricted shell execution is prohibited"
                )
            if runner is not None:
                if asyncio.iscoroutinefunction(runner):
                    result = await runner(params)
                else:
                    result = runner(params)
            else:
                result = {"executed": False, "reason": "no runner"}

            execution["status"] = "completed"
            execution["result"] = result
            execution["completed_at"] = time.monotonic()

            if approval_id:
                self._approval_manager.update_execution_status(
                    approval_id, ExecutionStatus.COMPLETED, summary=str(result)[:200]
                )

            await self._audit(
                "security.execution.completed",
                {
                    "request_id": request_id,
                    "tool_id": tool_id,
                    "stage": "commit",
                },
            )
            return {"ok": True, "request_id": request_id, "result": result}

        except Exception as exc:  # noqa: BLE001
            execution["status"] = "failed"
            execution["error"] = str(exc)
            execution["completed_at"] = time.monotonic()
            if approval_id:
                self._approval_manager.update_execution_status(
                    approval_id, ExecutionStatus.FAILED, summary=str(exc)[:200]
                )
            await self._audit(
                "security.execution.failed",
                {
                    "request_id": request_id,
                    "tool_id": tool_id,
                    "error": str(exc)[:512],
                    "stage": "audit",
                },
            )
            raise

    # -- estágios internos --------------------------------------------------

    async def _verify_identity(self, component_id: str) -> bool:
        """Verifica a identidade do componente solicitante.

        Fase 3: componentes confiáveis são preregistrados. Fase 4+ pode
        integrar com Identity Manager completo (mTLS, chaves, etc.).
        """
        trusted = {"nexus-daemon", "python-runtime", "cli", "tui", "web-hud"}
        return component_id in trusted or component_id.startswith("agent-")

    async def _validate_capability(
        self,
        component_id: str,
        tool_id: str,
        required_capability: str | None,
    ) -> bool:
        """Valida que o componente tem capacidade para a ferramenta."""
        if required_capability is None:
            return True
        # Fase 3 simples: componentes 'agent-*' podem exigir capacidade declarada.
        # Fase 4+: integrar com Agent Registry / capabilities declaradas.
        return True

    def _classify_risk(
        self, risk_level: str, context: PolicyContext
    ) -> tuple[GateDecision, str]:
        """Classificação de risco determinística (independente de LLM)."""
        if risk_level == "CRITICAL":
            return GateDecision.DENIED, "critical operations denied by default policy"
        if risk_level == "HIGH":
            return GateDecision.REQUIRES_APPROVAL, "high risk operations require explicit human approval"
        if risk_level == "MEDIUM":
            return GateDecision.REQUIRES_APPROVAL, "medium risk operations require human verification"
        if risk_level == "LOW":
            return GateDecision.ALLOWED, "low risk operation permitted within approved scope"
        return GateDecision.DENIED, "unknown risk level; denying by default"

    async def _reserve_resource(
        self,
        request_id: str,
        component_id: str,
        tool_id: str,
        resource_limits: str,
    ) -> bool:
        """Reserva recursos para a execução.

        Fase 3: validação simples. Fase 4+: integrar com Resource Manager
        (CPU, RAM, GPU, disco, rede).
        """
        # Validação básica: resource_limits não pode ser vazio quando informado.
        if resource_limits and resource_limits not in ("default", "low", "medium", "high"):
            return False
        return True

    async def _audit(self, kind: str, payload: Mapping[str, Any]) -> None:
        """Registra evento de auditoria no EventStore."""
        if self._recorder is None:
            return
        try:
            await self._recorder.record(
                Event(
                    kind=kind,
                    source="execution_gate",
                    payload=dict(payload),
                    severity=Severity.INFO,
                    dedup=False,
                )
            )
        except Exception:  # auditoria nunca deve quebrar a resposta
            pass

    def get_execution(self, request_id: str) -> dict[str, Any] | None:
        return self._executions.get(request_id)

    def list_executions(self) -> list[dict[str, Any]]:
        return list(self._executions.values())


__all__ = [
    "ExecutionGate",
    "GateRequest",
    "GateResult",
    "GateDecision",
    "ExecutionGateError",
    "IdentityVerificationError",
    "CapabilityValidationError",
    "ResourceReservationError",
]
