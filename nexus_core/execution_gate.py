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

T6: quando ``authoritative=True``, identidade e capacidade continuam locais,
mas a decisão final de política e aprovação é do Execution Gate Rust via IPC.
Qualquer indisponibilidade do daemon nega a execução (fail-closed).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from .approval import (
    ApprovalManager,
    ApprovalRequest,
    ExecutionStatus,
)
from .automation_types import RiskLevel
from .contracts import authorization_request_contract
from .control_plane import (
    ControlPlaneClient,
    approval_request_to_wire,
)
from .events import Event, Severity
from .policy import PolicyContext, PolicyEngine
from .recorder import EventRecorder
from .sandbox import SandboxExecutor


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
        control_plane: ControlPlaneClient | None = None,
        authoritative: bool = False,
    ) -> None:
        if authoritative and control_plane is None:
            raise ValueError(
                "modo autoritativo exige control_plane; sem daemon a execução "
                "não pode ser autorizada"
            )
        self._approval_manager = approval_manager or ApprovalManager()
        self._policy_engine = policy_engine or PolicyEngine()
        self._sandbox = sandbox
        self._recorder = recorder
        self._control_plane = control_plane
        self._authoritative = bool(authoritative)
        self._executions: dict[str, dict[str, Any]] = {}

    @property
    def authoritative(self) -> bool:
        """Verdadeiro quando a decisão final vem do daemon Rust."""
        return self._authoritative

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
        required_autonomy: int = 1,
        approval_id: str | None = None,
        execution_mode: str = "HOST",
        access_level: str = "READ",
        policy_version: str = "1.0",
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

        # 3-5. Autoridade final no daemon Rust (T6). Identidade e capacidade
        # continuam sendo verificadas no processo Python; política, aprovação e
        # consumo são decididos pelo Control Plane. Falha do daemon nega.
        if self._authoritative:
            return await self._authorize_authoritative(
                request_id=request_id,
                component_id=component_id,
                tool_id=tool_id,
                params=params,
                task_id=task_id,
                plan_id=plan_id,
                risk_level=risk_level,
                autonomy_level=autonomy_level,
                required_autonomy=required_autonomy,
                approval_id=approval_id,
                execution_mode=execution_mode,
                access_level=access_level,
                policy_version=policy_version,
                required_capability=required_capability,
                fs_scope=fs_scope,
                net_scope=net_scope,
                resource_limits=resource_limits,
                justification=justification,
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
        # Aqui aplicamos a mesma classificação determinística do PolicyEngine,
        # incluindo autonomia real e escopo de escrita.
        decision, reason = self._classify_risk(
            policy_risk,
            context,
            access_level=access_level,
            required_autonomy=required_autonomy,
        )

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

        approval_req = ApprovalRequest(
            tool_id=tool_id,
            params=dict(params),
            task_id=task_id,
            plan_id=plan_id,
            execution_mode=execution_mode,
            access_level=access_level,
            fs_scope=fs_scope,
            net_scope=net_scope,
            resource_limits=resource_limits,
            policy_version=policy_version,
            justification=justification or reason,
        )

        if approval_id is not None:
            # O chamador já possui uma aprovação humana. Validamos o vínculo
            # criptográfico e o estado antes de autorizar; não criamos outra.
            if not self._approval_manager.verify_action(approval_id, approval_req):
                await self._audit(
                    "security.execution.approval_denied",
                    {
                        "request_id": request_id,
                        "approval_id": approval_id,
                        "component_id": component_id,
                        "tool_id": tool_id,
                        "risk": policy_risk,
                        "reason": "approval hash mismatch",
                        "stage": "approval_check",
                    },
                )
                return GateResult(
                    decision=GateDecision.DENIED,
                    request_id=request_id,
                    reason="approval verification failed",
                )

            approval_record = self._approval_manager.get_request(approval_id)
            expires_at = (approval_record or {}).get("expires_at") or 0.0
            if (
                approval_record is None
                or approval_record.get("status") != "APPROVED"
                or float(expires_at) <= time.time()
            ):
                await self._audit(
                    "security.execution.approval_denied",
                    {
                        "request_id": request_id,
                        "approval_id": approval_id,
                        "component_id": component_id,
                        "tool_id": tool_id,
                        "risk": policy_risk,
                        "reason": "approval not available, expired or already consumed",
                        "stage": "approval_check",
                    },
                )
                return GateResult(
                    decision=GateDecision.DENIED,
                    request_id=request_id,
                    reason="approval not available, expired or already consumed",
                )

            await self._audit(
                "security.execution.approval_verified",
                {
                    "request_id": request_id,
                    "approval_id": approval_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "risk": policy_risk,
                    "stage": "approval_check",
                },
            )
        elif approval_required:
            # Cria solicitação de aprovação HITL.
            rid = self._approval_manager.request_approval(approval_req)
            # A execução precisa sobreviver à decisão humana para que o
            # chamador possa retomá-la com ``execute(request_id)``.
            self._executions[request_id] = {
                "component_id": component_id,
                "tool_id": tool_id,
                "params": dict(params),
                "task_id": task_id,
                "plan_id": plan_id,
                "status": "pending_approval",
                "approval_id": rid,
                "created_at": time.monotonic(),
                "risk_level": policy_risk,
            }
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
            "approval_id": approval_id,
            "approval_managed_externally": approval_id is not None,
            "created_at": time.monotonic(),
            "risk_level": policy_risk,
        }
        await self._audit(
            "security.execution.authorized",
            {
                "request_id": request_id,
                "component_id": component_id,
                "tool_id": tool_id,
                "approval_id": approval_id,
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

    async def _authorize_authoritative(
        self,
        *,
        request_id: str,
        component_id: str,
        tool_id: str,
        params: Mapping[str, Any],
        task_id: str,
        plan_id: str,
        risk_level: RiskLevel,
        autonomy_level: int,
        required_autonomy: int,
        approval_id: str | None,
        execution_mode: str,
        access_level: str,
        policy_version: str,
        required_capability: str | None,
        fs_scope: str,
        net_scope: str,
        resource_limits: str,
        justification: str,
    ) -> GateResult:
        """Delega a decisão final ao daemon e registra o ciclo local.

        O daemon cria a aprovação quando necessário ou reivindica a aprovação
        apresentada (``APPROVED → CLAIMED``). A transição seguinte
        (``CLAIMED → CONSUMED``) só acontece em ``begin_execution``, imediatamente
        antes do lançamento da operação.
        """
        risk = risk_level.name
        approval_req = ApprovalRequest(
            tool_id=tool_id,
            params=dict(params),
            task_id=task_id,
            plan_id=plan_id,
            execution_mode=execution_mode,
            access_level=access_level,
            fs_scope=fs_scope,
            net_scope=net_scope,
            resource_limits=resource_limits,
            policy_version=policy_version,
            justification=justification or "control plane authorization",
        )
        wire = authorization_request_contract(
            component_id=component_id,
            tool_id=tool_id,
            params=params,
            task_id=task_id,
            plan_id=plan_id,
            risk_level=risk,
            autonomy_level=autonomy_level,
            required_autonomy=required_autonomy,
            approval_id=approval_id,
            execution_mode=execution_mode,
            access_level=access_level,
            policy_version=policy_version,
            required_capability=required_capability,
            fs_scope=fs_scope,
            net_scope=net_scope,
            resource_limits=resource_limits,
            justification=justification,
        )
        try:
            assert self._control_plane is not None
            result = self._control_plane.authorize(wire)
        except Exception as exc:  # fail-closed: sem daemon, sem autorização
            await self._audit(
                "security.execution.control_plane_unavailable",
                {
                    "request_id": request_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "stage": "policy_evaluation",
                    "error": type(exc).__name__,
                },
            )
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason=f"control plane unavailable: {exc}",
            )

        decision = result.get("decision")
        remote_request_id = result.get("request_id")
        if not isinstance(remote_request_id, str) or not remote_request_id:
            return GateResult(
                decision=GateDecision.DENIED,
                request_id=request_id,
                reason="control plane returned an invalid request_id",
            )
        metadata = result.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        remote_approval_id = metadata.get("approval_id")
        if not isinstance(remote_approval_id, str):
            remote_approval_id = None

        if decision == "ALLOWED":
            execution_id = result.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                execution_id = remote_request_id
            self._executions[execution_id] = {
                "component_id": component_id,
                "tool_id": tool_id,
                "params": dict(params),
                "task_id": task_id,
                "plan_id": plan_id,
                "status": "authorized",
                "approval_id": remote_approval_id,
                "approval_request": approval_req,
                "approval_managed_externally": False,
                "created_at": time.monotonic(),
                "risk_level": risk,
                "authority": "rust",
                "gate_request_id": remote_request_id,
            }
            await self._audit(
                "security.execution.authorized",
                {
                    "request_id": execution_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "approval_id": remote_approval_id,
                    "risk": risk,
                    "stage": "commit",
                    "authority": "rust",
                },
            )
            return GateResult(
                decision=GateDecision.ALLOWED,
                request_id=execution_id,
                reason=str(result.get("reason") or ""),
                approved=True,
                execution_id=execution_id,
                metadata=metadata,
            )

        if decision == "REQUIRES_APPROVAL":
            if remote_approval_id is None:
                return GateResult(
                    decision=GateDecision.DENIED,
                    request_id=remote_request_id,
                    reason="control plane required approval without approval_id",
                )
            self._executions[remote_request_id] = {
                "component_id": component_id,
                "tool_id": tool_id,
                "params": dict(params),
                "task_id": task_id,
                "plan_id": plan_id,
                "status": "pending_approval",
                "approval_id": remote_approval_id,
                "approval_request": approval_req,
                "created_at": time.monotonic(),
                "risk_level": risk,
                "authority": "rust",
            }
            await self._audit(
                "security.execution.approval_requested",
                {
                    "request_id": remote_request_id,
                    "approval_id": remote_approval_id,
                    "component_id": component_id,
                    "tool_id": tool_id,
                    "risk": risk,
                    "stage": "approval_check",
                    "authority": "rust",
                },
            )
            return GateResult(
                decision=GateDecision.REQUIRES_APPROVAL,
                request_id=remote_request_id,
                reason=str(result.get("reason") or ""),
                metadata={**metadata, "requires_human": True},
            )

        await self._audit(
            "security.execution.policy_denied",
            {
                "request_id": remote_request_id,
                "component_id": component_id,
                "tool_id": tool_id,
                "risk": risk,
                "reason": str(result.get("reason") or "denied by control plane"),
                "stage": "policy_evaluation",
                "authority": "rust",
            },
        )
        return GateResult(
            decision=GateDecision.DENIED,
            request_id=remote_request_id,
            reason=str(result.get("reason") or "denied by control plane"),
        )

    async def begin_execution(self, request_id: str) -> dict[str, Any]:
        """Inicia a execução autorizada.

        No modo autoritativo, produz ``CLAIMED → CONSUMED`` no daemon antes de
        qualquer efeito externo. No modo local, mantém o protocolo anterior
        (claim + ``mark_starting`` para aprovação pendente).
        """
        execution = self._executions.get(request_id)
        if execution is None:
            raise ExecutionGateError(
                f"execution not found or not authorized: {request_id}"
            )
        status = execution["status"]
        approval_id = execution.get("approval_id")
        approval_req = execution.get("approval_request")

        if self._authoritative:
            if status not in ("authorized", "pending_approval"):
                raise ExecutionGateError(
                    f"execution {request_id} not in executable state: {status}"
                )
            if approval_id:
                assert self._control_plane is not None
                try:
                    if status == "pending_approval":
                        self._control_plane.approval_claim(
                            approval_id,
                            approval_request_to_wire(approval_req)
                            if isinstance(approval_req, ApprovalRequest)
                            else None,
                        )
                    body = self._control_plane.execution_start(approval_id)
                except Exception as exc:
                    raise ExecutionGateError(
                        "control plane unavailable; refusing to execute: "
                        f"{approval_id}: {exc}"
                    ) from exc
                approval_status = body["approval"].get("status")
                execution_status = body["execution"].get("status")
                if approval_status != "CONSUMED" or execution_status != "STARTING":
                    raise ExecutionGateError(
                        "control plane did not confirm CLAIMED→CONSUMED "
                        f"(approval={approval_status!r}, execution={execution_status!r})"
                    )
            execution["status"] = "executing"
            execution["started_at"] = time.monotonic()
            return {
                "ok": True,
                "request_id": request_id,
                "approval_id": approval_id,
            }

        if status == "pending_approval":
            if not approval_id or not self._approval_manager.claim_approval(approval_id):
                raise ExecutionGateError(
                    f"approval {approval_id} not available, expired or already consumed"
                )
            self._approval_manager.mark_starting(approval_id)
        elif status != "authorized":
            raise ExecutionGateError(
                f"execution {request_id} not in authorized state: {status}"
            )

        execution["status"] = "executing"
        execution["started_at"] = time.monotonic()
        return {"ok": True, "request_id": request_id, "approval_id": approval_id}

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
        tool_id = execution["tool_id"]
        params = execution["params"]

        # Aplica CLAIMED → CONSUMED (STARTING) antes do lançamento. No modo
        # autoritativo isso é confirmado pelo daemon; falha impede a execução.
        await self.begin_execution(request_id)

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

            await self.commit(request_id, result=result, success=True)
            return {"ok": True, "request_id": request_id, "result": result}

        except asyncio.CancelledError:
            try:
                await self.commit(
                    request_id, result=None, success=False, error="cancelled"
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await self.commit(
                    request_id,
                    result=None,
                    success=False,
                    error=str(exc)[:512],
                )
            except Exception:
                # A falha original da ferramenta é a que o chamador precisa ver;
                # o commit remoto pode ser recuperado como UNKNOWN pelo daemon.
                pass
            raise

    async def commit(
        self,
        request_id: str,
        *,
        result: Any = None,
        success: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Fecha o ciclo de uma execução autorizada que já foi realizada.

        Diferente de ``execute()``, este método não dispara a operação. Ele
        registra o resultado da ação executada pelo chamador (o
        ``AutomationEngine``, por exemplo) sem simular uma segunda execução.

        Execuções com aprovação pendente não podem ser commitadas: a
        aprovação precisa ser reivindicada por ``execute()`` ou pelo fluxo
        HITL do chamador, que é o responsável por consumi-la.
        """
        execution = self._executions.get(request_id)
        if execution is None:
            raise ExecutionGateError(
                f"execution not found or not authorized: {request_id}"
            )

        status = execution["status"]
        if status == "pending_approval":
            raise ExecutionGateError(
                f"execution {request_id} still requires approval; "
                "claim the approval before committing"
            )
        if status not in ("authorized", "executing"):
            raise ExecutionGateError(
                f"execution {request_id} not in committable state: {status}"
            )

        execution["status"] = "completed" if success else "failed"
        execution["result"] = result
        execution["completed_at"] = time.monotonic()
        if error:
            execution["error"] = error

        # Fecha primeiro no daemon (autoridade final) e depois registra o
        # estado local. No modo local, aprovações criadas pelo próprio gate são
        # atualizadas aqui; quando o chamador trouxe a aprovação, ele é o dono
        # do ciclo de vida e não deve ser sobrescrito.
        approval_id = execution.get("approval_id")
        summary = (str(result) if result is not None else error or "")[:200]
        if self._authoritative:
            if approval_id:
                assert self._control_plane is not None
                try:
                    self._control_plane.execution_complete(
                        approval_id,
                        "COMPLETED" if success else "FAILED",
                        summary=summary,
                    )
                except Exception as exc:
                    raise ExecutionGateError(
                        "control plane unavailable; execution outcome could not be "
                        f"committed for approval {approval_id}: {exc}"
                    ) from exc
        elif approval_id and not execution.get("approval_managed_externally"):
            self._approval_manager.update_execution_status(
                approval_id,
                ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED,
                summary=summary,
            )

        execution["status"] = "completed" if success else "failed"
        execution["result"] = result
        execution["completed_at"] = time.monotonic()
        if error:
            execution["error"] = error

        await self._audit(
            "security.execution.completed" if success else "security.execution.failed",
            {
                "request_id": request_id,
                "tool_id": execution["tool_id"],
                "stage": "commit",
                **({"authority": "rust"} if self._authoritative else {}),
                **({"error": error[:512]} if error else {}),
            },
        )
        return {"ok": success, "request_id": request_id, "result": result}

    # -- estágios internos --------------------------------------------------

    async def _verify_identity(self, component_id: str) -> bool:
        """Verifica a identidade do componente solicitante.

        Fase 3: componentes confiáveis são preregistrados. Fase 4+ pode
        integrar com Identity Manager completo (mTLS, chaves, etc.).
        """
        trusted = {
            "nexus-daemon",
            "python-runtime",
            "cli",
            "tui",
            "web-hud",
            "automation_engine",
        }
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
        self,
        risk_level: str,
        context: PolicyContext,
        *,
        access_level: str = "READ",
        required_autonomy: int = 1,
    ) -> tuple[GateDecision, str]:
        """Classificação de risco determinística (independente de LLM)."""
        if risk_level == "CRITICAL":
            return GateDecision.DENIED, "critical operations denied by default policy"
        if risk_level == "HIGH":
            return GateDecision.REQUIRES_APPROVAL, "high risk operations require explicit human approval"
        if risk_level == "MEDIUM":
            return GateDecision.REQUIRES_APPROVAL, "medium risk operations require human verification"
        if risk_level == "LOW":
            if access_level == "WRITE" and context.fs_scope != "SAFE_ZONE":
                return (
                    GateDecision.REQUIRES_APPROVAL,
                    "low risk write outside safe zone requires approval",
                )
            if context.autonomy_level >= required_autonomy:
                return (
                    GateDecision.ALLOWED,
                    "low risk operation permitted within approved scope",
                )
            return (
                GateDecision.REQUIRES_APPROVAL,
                "insufficient autonomy for low-risk operation",
            )
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
    "CapabilityValidationError",
    "ExecutionGate",
    "ExecutionGateError",
    "GateDecision",
    "GateRequest",
    "GateResult",
    "IdentityVerificationError",
    "ResourceReservationError",
]
