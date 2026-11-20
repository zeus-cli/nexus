"""Cliente Python do Control Plane Rust (T6).

Quando ``[control_plane] enabled = true``, o daemon Rust é a autoridade final
de execução e a fonte de verdade das aprovações. Este módulo concentra:

* ``ControlPlaneClient`` — conexão preguiçosa e serializada sobre a mesma
  bridge newline-JSON usada pelo barramento de eventos;
* ``RustApprovalManager`` — adaptador com a interface do ``ApprovalManager``
  Python, para que CLI, API e HUD continuem funcionando sem abrir o SQLite de
  aprovações em paralelo (nada de dual-write).

Falha de conexão, timeout, frame inválido ou erro do daemon são traduzidos
para ``ControlPlaneUnavailable``. O ``ExecutionGate`` transforma qualquer
falha em negação; nenhuma operação é autorizada sem o daemon.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from .approval import ApprovalDecision, ApprovalRequest, ExecutionStatus
from .event_bridge import BridgeError, RustEventBridge

T = TypeVar("T")

DEFAULT_APPROVAL_TTL_SECONDS = 3600.0


class ControlPlaneError(RuntimeError):
    """Erro de protocolo ou de contrato na chamada ao Control Plane."""


class ControlPlaneUnavailable(ControlPlaneError):
    """O daemon não está disponível; a operação deve ser negada."""


def _as_record(value: Any, *, operation: str) -> dict[str, Any]:
    """Normaliza ``approval_record`` do Rust para o formato do SQLite Python."""
    if not isinstance(value, Mapping):
        raise ControlPlaneError(f"{operation}: registro de aprovação inválido")
    record = dict(value)
    approval_id = record.pop("approval_id", None)
    if not isinstance(approval_id, str) or not approval_id:
        raise ControlPlaneError(f"{operation}: registro sem approval_id")
    record["id"] = approval_id
    return record


def _execution_body(value: Any, *, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlPlaneError(f"{operation}: resposta de execução inválida")
    body = dict(value)
    approval = _as_record(body.get("approval"), operation=operation)
    execution = body.get("execution")
    if not isinstance(execution, Mapping):
        raise ControlPlaneError(f"{operation}: execução ausente")
    return {"approval": approval, "execution": dict(execution)}


def approval_request_to_wire(request: ApprovalRequest) -> dict[str, Any]:
    """Projeção canônica usada pelo contrato ``approval_request`` v1."""
    return {
        "tool_id": request.tool_id,
        "params": dict(request.params),
        "task_id": request.task_id,
        "plan_id": request.plan_id,
        "execution_mode": request.execution_mode,
        "access_level": request.access_level,
        "fs_scope": request.fs_scope,
        "net_scope": request.net_scope,
        "resource_limits": request.resource_limits,
        "policy_version": request.policy_version,
        "justification": request.justification,
    }


def _local_token_hash(token: str) -> str:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token local não pode ser vazio")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ControlPlaneClient:
    """Cliente serializado para os comandos de controle do daemon Rust."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 5.0,
        component_id: str = "python-runtime",
        logger: logging.Logger | None = None,
        bridge_factory: Callable[..., RustEventBridge] = RustEventBridge,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._component_id = component_id
        self._logger = logger or logging.getLogger("nexus.control_plane")
        self._bridge_factory = bridge_factory
        self._bridge: RustEventBridge | None = None
        self._lock = threading.RLock()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._bridge is not None and self._bridge.connected

    def connect(self) -> ControlPlaneClient:
        with self._lock:
            if self._bridge is not None and self._bridge.connected:
                return self
            self.close()
            bridge = self._bridge_factory(
                self._socket_path,
                timeout=self._timeout,
                component_id=self._component_id,
            )
            try:
                bridge.connect()
            except BridgeError as exc:
                raise ControlPlaneUnavailable(
                    f"control plane indisponível em {self._socket_path}: {exc}"
                ) from exc
            self._bridge = bridge
            return self

    def close(self) -> None:
        with self._lock:
            bridge = self._bridge
            self._bridge = None
            if bridge is not None:
                bridge.close()

    def __enter__(self) -> ControlPlaneClient:
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _call(self, operation: str, action: Callable[[RustEventBridge], T]) -> T:
        """Executa um comando com reconexão preguiçosa e erro classificado.

        O lock do cliente cobre envio e leitura: a bridge já serializa uma
        chamada individual, mas duas threads não podem negociar conexões
        concorrentes depois de uma queda do daemon.
        """
        with self._lock:
            bridge = self._bridge
            if bridge is None or not bridge.connected:
                self.connect()
                bridge = self._bridge
            assert bridge is not None
            try:
                return action(bridge)
            except BridgeError as exc:
                self.close()
                raise ControlPlaneUnavailable(
                    f"{operation}: daemon indisponível: {exc}"
                ) from exc
            except (TypeError, ValueError, KeyError) as exc:
                raise ControlPlaneError(f"{operation}: {exc}") from exc

    # -- saúde -------------------------------------------------------------

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call(
            "health",
            lambda bridge: bridge.health(
                timeout=self._timeout if timeout is None else timeout
            ),
        )

    # -- autorização -------------------------------------------------------

    def authorize(
        self,
        request: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "authorize",
            lambda bridge: bridge.authorize(
                request, timeout=self._timeout if timeout is None else timeout
            ),
        )

    # -- aprovações --------------------------------------------------------

    def approval_request(
        self,
        request: Mapping[str, Any],
        *,
        risk_level: str | None = None,
        ttl_s: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        record = self._call(
            "approval_request",
            lambda bridge: bridge.approval_request(
                request,
                risk_level=risk_level,
                ttl_s=ttl_s,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )
        return _as_record(record, operation="approval_request")

    def approval_decide(
        self,
        decision: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        record = self._call(
            "approval_decide",
            lambda bridge: bridge.approval_decide(
                decision, timeout=self._timeout if timeout is None else timeout
            ),
        )
        return _as_record(record, operation="approval_decide")

    def approval_status(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        record = self._call(
            "approval_status",
            lambda bridge: bridge.approval_status(
                approval_id, timeout=self._timeout if timeout is None else timeout
            ),
        )
        return _as_record(record, operation="approval_status")

    def approval_revoke(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        record = self._call(
            "approval_revoke",
            lambda bridge: bridge.approval_revoke(
                approval_id, timeout=self._timeout if timeout is None else timeout
            ),
        )
        return _as_record(record, operation="approval_revoke")

    def approval_list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        records = self._call(
            "approval_list",
            lambda bridge: bridge.approval_list(
                status=status,
                limit=limit,
                offset=offset,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )
        return [_as_record(item, operation="approval_list") for item in records]

    def approval_verify(
        self,
        approval_id: str,
        request: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> bool:
        return self._call(
            "approval_verify",
            lambda bridge: bridge.approval_verify(
                approval_id,
                request,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def approval_claim(
        self,
        approval_id: str,
        request: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        record = self._call(
            "approval_claim",
            lambda bridge: bridge.approval_claim(
                approval_id,
                request,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )
        return _as_record(record, operation="approval_claim")

    def approval_expire(self, *, timeout: float | None = None) -> int:
        return self._call(
            "approval_expire",
            lambda bridge: bridge.approval_expire(
                timeout=self._timeout if timeout is None else timeout
            ),
        )

    # -- ciclo de execução -------------------------------------------------

    def execution_start(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = self._call(
            "execution_start",
            lambda bridge: bridge.execution_start(
                approval_id, timeout=self._timeout if timeout is None else timeout
            ),
        )
        return _execution_body(body, operation="execution_start")

    def execution_complete(
        self,
        approval_id: str,
        status: str,
        *,
        summary: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = self._call(
            "execution_complete",
            lambda bridge: bridge.execution_complete(
                approval_id,
                status,
                summary=summary,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )
        return _execution_body(body, operation="execution_complete")

    def execution_status(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = self._call(
            "execution_status",
            lambda bridge: bridge.execution_status(
                approval_id, timeout=self._timeout if timeout is None else timeout
            ),
        )
        return _execution_body(body, operation="execution_status")

    def execution_recover(
        self,
        timeout_s: float = 300.0,
        *,
        timeout: float | None = None,
    ) -> int:
        return self._call(
            "execution_recover",
            lambda bridge: bridge.execution_recover(
                timeout_s, timeout=self._timeout if timeout is None else timeout
            ),
        )

    # -- identidade (T8.3) -------------------------------------------------

    def identity_list(
        self,
        actor_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._call(
            "identity_list",
            lambda bridge: bridge.identity_list(
                actor_id,
                kind=kind,
                status=status,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_get(
        self,
        actor_id: str,
        principal_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "identity_get",
            lambda bridge: bridge.identity_get(
                actor_id,
                principal_id,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_create_principal(
        self,
        actor_id: str,
        *,
        kind: str,
        display_name: str,
        role_id: str,
        parent_id: str | None = None,
        max_autonomy: int = 0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "identity_create_principal",
            lambda bridge: bridge.identity_create_principal(
                actor_id,
                kind=kind,
                display_name=display_name,
                role_id=role_id,
                parent_id=parent_id,
                max_autonomy=max_autonomy,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_revoke_principal(
        self,
        actor_id: str,
        principal_id: str,
        reason: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "identity_revoke_principal",
            lambda bridge: bridge.identity_revoke_principal(
                actor_id,
                principal_id,
                reason,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_effective_scopes(
        self,
        principal_id: str,
        *,
        timeout: float | None = None,
    ) -> list[str]:
        return self._call(
            "identity_effective_scopes",
            lambda bridge: bridge.identity_effective_scopes(
                principal_id, timeout=self._timeout if timeout is None else timeout
            ),
        )

    def identity_permission_check(
        self,
        principal_id: str,
        scope: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "identity_permission_check",
            lambda bridge: bridge.identity_permission_check(
                principal_id,
                scope,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_device_list(
        self,
        actor_id: str,
        *,
        principal_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._call(
            "identity_device_list",
            lambda bridge: bridge.identity_device_list(
                actor_id,
                principal_id=principal_id,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_device_revoke(
        self,
        actor_id: str,
        device_id: str,
        reason: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "identity_device_revoke",
            lambda bridge: bridge.identity_device_revoke(
                actor_id,
                device_id,
                reason,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_pair_start(
        self,
        actor_id: str,
        role_id: str,
        *,
        constraints: Mapping[str, Any] | None = None,
        ttl_s: float = 300.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Cria pairing e devolve segredo/código brutos apenas ao chamador.

        O wire transporta somente SHA-256 do segredo e do código; o segredo
        bruto nunca cruza o IPC nem é persistido.
        """
        secret = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(100_000_000):08d}"
        session = self._call(
            "identity_pair_start",
            lambda bridge: bridge.identity_pair_start(
                actor_id,
                role_id,
                hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
                constraints=constraints,
                ttl_s=ttl_s,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )
        return {"session": session, "secret": secret, "code": code}

    # -- bootstrap local (T8.4) -------------------------------------------

    def identity_init_owner(
        self,
        token: str,
        *,
        display_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Cria/reutiliza o owner local ligado ao token da API.

        O token bruto nunca cruza o IPC: o cliente envia somente seu SHA-256.
        """
        return self._call(
            "identity_init_owner",
            lambda bridge: bridge.identity_init_owner(
                _local_token_hash(token),
                display_name=display_name,
                timeout=self._timeout if timeout is None else timeout,
            ),
        )

    def identity_owner_status(
        self,
        token: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Consulta o owner local ligado ao token, sem criar identidade."""
        return self._call(
            "identity_owner_status",
            lambda bridge: bridge.identity_owner_status(
                _local_token_hash(token),
                timeout=self._timeout if timeout is None else timeout,
            ),
        )


class RustApprovalManager:
    """Adaptador do ``ApprovalManager`` Python sobre o store do daemon.

    A interface é intencionalmente compatível com o manager local, mas todas
    as operações vão por IPC. Em modo autoritativo o Python não abre
    ``nexus_approvals.db`` para escrita, eliminando o risco de dual-write.
    """

    def __init__(self, client: ControlPlaneClient) -> None:
        self._client = client

    @property
    def authoritative(self) -> bool:
        return True

    @property
    def client(self) -> ControlPlaneClient:
        return self._client

    def request_approval(
        self,
        req: ApprovalRequest,
        ttl_s: float = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> str:
        record = self._client.approval_request(
            approval_request_to_wire(req),
            ttl_s=ttl_s,
        )
        return str(record["id"])

    def decide(self, request_id: str, decision: ApprovalDecision) -> None:
        self._client.approval_decide(
            {
                "approval_id": request_id,
                "decision": decision.decision,
                "approver": decision.approver,
                "timestamp": decision.timestamp,
            }
        )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        try:
            return self._client.approval_status(request_id)
        except ControlPlaneUnavailable:
            raise
        except ControlPlaneError as exc:
            message = str(exc).lower()
            if "não encontrada" in message or "not found" in message:
                return None
            raise

    def list_pending(self) -> list[dict[str, Any]]:
        return self._client.approval_list(status="PENDING", limit=1000)

    def get_history(
        self,
        limit: int = 100,
        status: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._client.approval_list(
            status=status,
            limit=limit,
            offset=offset,
        )

    def verify_action(self, request_id: str, req: ApprovalRequest) -> bool:
        try:
            return self._client.approval_verify(
                request_id, approval_request_to_wire(req)
            )
        except ControlPlaneUnavailable:
            return False

    def claim_approval(self, request_id: str) -> bool:
        try:
            self._client.approval_claim(request_id)
            return True
        except ControlPlaneUnavailable:
            return False
        except ControlPlaneError:
            return False

    def mark_starting(self, request_id: str) -> None:
        self._client.execution_start(request_id)

    def update_execution_status(
        self,
        request_id: str,
        status: ExecutionStatus,
        summary: str | None = None,
    ) -> None:
        if status not in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.INTERRUPTED,
        ):
            raise ValueError(
                f"status de execução terminal esperado (recebido: {status.value})"
            )
        self._client.execution_complete(request_id, status.value, summary=summary)

    def revoke(self, request_id: str) -> None:
        self._client.approval_revoke(request_id)

    def expire_stale(self) -> int:
        return self._client.approval_expire()

    def recover_orphans(self, timeout_s: float = 300.0) -> int:
        return self._client.execution_recover(timeout_s)

    def get_execution(self, request_id: str) -> dict[str, Any] | None:
        try:
            return self._client.execution_status(request_id)["execution"]
        except ControlPlaneError as exc:
            message = str(exc).lower()
            if "não encontrada" in message or "not found" in message:
                return None
            raise


__all__ = [
    "ControlPlaneClient",
    "ControlPlaneError",
    "ControlPlaneUnavailable",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "RustApprovalManager",
    "approval_request_to_wire",
]
