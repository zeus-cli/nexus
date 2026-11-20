"""Cliente UDS para a bridge de eventos Python ↔ Rust.

O protocolo é o mesmo do daemon (`core/nexus-daemon/src/ipc/protocol.rs`):
JSON por linha, com frames ``{"type": ..., "body": {...}}``. Toda conexão
começa com ``Hello`` e exige ``Welcome`` compatível (major 1) antes de
qualquer comando. O evento em si segue o contrato v1.0.0 de
``contracts/schemas/event.schema.json`` e nunca transporta ``dedup_key``
(campo derivado).

O cliente é síncrono e destinado a testes de integração e a ferramentas
operacionais. O runtime Python continua usando o ``EventBus`` em processo; o
bridge é a fronteira explícita para o Control Plane e expõe comandos de
controle (``authorize``, aprovação, ``health`` e ``event``), sem substituir a
autoridade de segurança do Python.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

from .contracts import event_from_wire, event_to_wire, validate_contract
from .events import Event

_MAX_FRAME_BYTES = 1_048_576
IPC_PROTOCOL_VERSION = "1.0.0"
_IPC_PROTOCOL_MAJOR = 1


class BridgeError(RuntimeError):
    """Erro de protocolo ou de entrega na bridge."""


class BridgeTimeout(BridgeError):
    """O daemon não respondeu dentro do prazo."""


class BridgeProtocolError(BridgeError):
    """Frame inválido ou conexão encerrada pelo daemon."""


class BridgeVersionMismatch(BridgeError):
    """O daemon rejeitou a versão do protocolo no handshake."""


def _protocol_major(version: str) -> int | None:
    major = version.split(".", 1)[0]
    if not major.isdigit():
        return None
    return int(major)


def _identity_text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} não pode ser vazio")
    return value


def _identity_sha256(field: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdefABCDEF" for ch in value)
    ):
        raise ValueError(f"{field} deve ser SHA-256 em hex (64 caracteres)")
    return value.lower()


class RustEventBridge:
    """Cliente newline-JSON sobre Unix Domain Socket.

    Cada instância mantém uma conexão. O primeiro frame é sempre ``Hello``
    (versão do protocolo + component_id); o daemon responde ``Welcome`` ou
    ``VersionMismatch``. Para testar publicação e assinatura sem intercalar
    ``Ack`` e ``Delivered``, use conexões separadas.
    """

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 5.0,
        max_frame_bytes: int = _MAX_FRAME_BYTES,
        component_id: str = "python-runtime",
        protocol_version: str = IPC_PROTOCOL_VERSION,
        logger: logging.Logger | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout deve ser positivo")
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes deve ser positivo")
        if not component_id or any(ch.isspace() for ch in component_id):
            raise ValueError("component_id deve ser não vazio e sem espaços")
        if _protocol_major(protocol_version) != _IPC_PROTOCOL_MAJOR:
            raise ValueError(
                f"protocol_version incompatível: {protocol_version!r}"
            )
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._max_frame_bytes = max_frame_bytes
        self._component_id = component_id
        self._protocol_version = protocol_version
        self._logger = logger or logging.getLogger("nexus.event_bridge")
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._pending: list[dict[str, Any]] = []
        self._welcome: dict[str, Any] | None = None
        self._lock = threading.RLock()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def welcome(self) -> dict[str, Any] | None:
        """Corpo do ``Welcome`` negociado, ou ``None`` antes do connect."""
        return dict(self._welcome) if self._welcome is not None else None

    def connect(self) -> RustEventBridge:
        with self._lock:
            if self._socket is not None:
                return self
            if not self._socket_path.exists():
                raise BridgeError(f"socket do daemon ausente: {self._socket_path}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            try:
                sock.connect(str(self._socket_path))
            except OSError as exc:
                sock.close()
                raise BridgeError(
                    f"falha ao conectar em {self._socket_path}: {exc}"
                ) from exc
            self._socket = sock
            self._buffer = b""
            self._pending = []
            self._welcome = None
            try:
                self._send(
                    {
                        "type": "Hello",
                        "body": {
                            "protocol_version": self._protocol_version,
                            "component_id": self._component_id,
                        },
                    }
                )
                frame = self._read_until({"Welcome"}, timeout=self._timeout)
                body = frame.get("body") or {}
                received = body.get("protocol_version")
                if not isinstance(received, str) or _protocol_major(received) != _IPC_PROTOCOL_MAJOR:
                    raise BridgeProtocolError(
                        f"Welcome com protocol_version incompatível: {received!r}"
                    )
                echoed = body.get("component_id")
                if echoed != self._component_id:
                    raise BridgeProtocolError(
                        "Welcome com component_id diferente do Hello"
                    )
                if not isinstance(body.get("server_version"), str):
                    raise BridgeProtocolError("Welcome sem server_version")
                self._welcome = dict(body)
                return self
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
            self._buffer = b""
            self._pending = []
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise BridgeError("bridge não conectada; chame connect() primeiro")
        return self._socket

    def _send(self, frame: Mapping[str, Any]) -> None:
        sock = self._require_socket()
        payload = json.dumps(
            dict(frame), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        try:
            sock.sendall(payload)
        except OSError as exc:
            raise BridgeError(f"falha ao enviar frame: {exc}") from exc

    def _read_frame(self, timeout: float | None = None) -> dict[str, Any]:
        sock = self._require_socket()
        old_timeout = sock.gettimeout()
        if timeout is not None:
            if timeout <= 0:
                raise BridgeTimeout("timeout esgotado aguardando frame")
            sock.settimeout(timeout)
        try:
            while b"\n" not in self._buffer:
                chunk = sock.recv(65_536)
                if not chunk:
                    raise BridgeProtocolError("conexão encerrada pelo daemon")
                self._buffer += chunk
                if len(self._buffer) > self._max_frame_bytes:
                    raise BridgeProtocolError("frame excede o limite configurado")
            line, self._buffer = self._buffer.split(b"\n", 1)
            if len(line) > self._max_frame_bytes:
                raise BridgeProtocolError("frame excede o limite configurado")
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BridgeProtocolError(f"frame não é JSON válido: {exc}") from exc
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                raise BridgeProtocolError("frame sem campo 'type'")
            body = frame.get("body")
            if body is not None and not isinstance(body, dict):
                raise BridgeProtocolError("campo 'body' não é um objeto")
            return frame
        except TimeoutError as exc:
            raise BridgeTimeout(
                f"timeout de {timeout or old_timeout}s aguardando resposta do daemon"
            ) from exc
        except OSError as exc:
            raise BridgeError(f"falha ao ler frame: {exc}") from exc
        finally:
            if timeout is not None and self._socket is not None:
                self._socket.settimeout(old_timeout)

    def _read_until(
        self,
        expected: set[str],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # Frames recebidos fora de ordem (ex.: Delivered antes de Ack/Subscribed)
        # ficam pendentes e são devolvidos na chamada correspondente, em vez de
        # descartados.
        for index, frame in enumerate(self._pending):
            kind = frame["type"]
            body = frame.get("body") or {}
            if kind == "Error":
                del self._pending[index]
                raise BridgeError(str(body.get("error", "erro não especificado")))
            if kind == "VersionMismatch":
                del self._pending[index]
                raise BridgeVersionMismatch(
                    "versão de protocolo incompatível: "
                    f"daemon suporta {body.get('supported')!r}, "
                    f"recebeu {body.get('received')!r}"
                )
            if kind in expected:
                del self._pending[index]
                return frame

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout("timeout aguardando frame do daemon")
            frame = self._read_frame(remaining)
            kind = frame["type"]
            body = frame.get("body") or {}
            if kind == "Error":
                raise BridgeError(str(body.get("error", "erro não especificado")))
            if kind == "VersionMismatch":
                raise BridgeVersionMismatch(
                    "versão de protocolo incompatível: "
                    f"daemon suporta {body.get('supported')!r}, "
                    f"recebeu {body.get('received')!r}"
                )
            if kind in expected:
                return frame
            self._pending.append(frame)

    def publish(self, event: Event, *, validate: bool = True) -> dict[str, Any]:
        """Publica um evento e aguarda o ``Ack`` do daemon."""
        wire = event_to_wire(event)
        if validate:
            validate_contract("event", wire)
        with self._lock:
            self._send({"type": "Event", "body": {"event": wire}})
            frame = self._read_until({"Ack"}, timeout=self._timeout)
            body = frame.get("body") or {}
            if not body.get("ok"):
                raise BridgeError(str(body.get("error") or "daemon rejeitou o evento"))
            if body.get("event_id") not in (None, event.event_id):
                raise BridgeProtocolError(
                    "Ack com event_id diferente do evento publicado"
                )
            return body

    def subscribe(
        self,
        patterns: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> list[str]:
        """Assina padrões e retorna os ids confirmados pelo daemon."""
        normalized = [str(pattern).strip() for pattern in patterns]
        if not normalized or any(not pattern for pattern in normalized):
            raise ValueError("patterns deve conter ao menos um padrão não vazio")
        with self._lock:
            self._send({"type": "Subscribe", "body": {"patterns": normalized}})
            ids: list[str] = []
            while len(ids) < len(normalized):
                frame = self._read_until({"Subscribed"}, timeout=timeout)
                subscription_id = (frame.get("body") or {}).get("id")
                if not isinstance(subscription_id, str) or not subscription_id:
                    raise BridgeProtocolError("Subscribed sem id válido")
                ids.append(subscription_id)
            return ids

    def unsubscribe(self, subscription_id: str) -> None:
        if not subscription_id:
            raise ValueError("subscription_id não pode ser vazio")
        with self._lock:
            self._send({"type": "Unsubscribe", "body": {"id": subscription_id}})

    def next_event(self, timeout: float | None = None) -> Event:
        """Aguarda o próximo frame ``Delivered`` e o converte em ``Event``."""
        with self._lock:
            frame = self._read_until({"Delivered"}, timeout=timeout)
            wire = (frame.get("body") or {}).get("event")
            if not isinstance(wire, dict):
                raise BridgeProtocolError("Delivered sem evento")
            return event_from_wire(wire)

    def health(self, timeout: float | None = None) -> dict[str, Any]:
        """Solicita o health check e retorna o corpo de status."""
        with self._lock:
            self._send({"type": "Health"})
            frame = self._read_until({"HealthResponse"}, timeout=timeout)
            body = frame.get("body") or {}
            version = body.get("protocol_version")
            if not isinstance(version, str) or _protocol_major(version) != _IPC_PROTOCOL_MAJOR:
                raise BridgeProtocolError(
                    f"HealthResponse com protocol_version inválida: {version!r}"
                )
            status = body.get("status")
            if not isinstance(status, dict):
                raise BridgeProtocolError("HealthResponse sem status")
            return status

    def authorize(
        self,
        request: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Envia um ``authorization_request`` e retorna o resultado do gate.

        O request é validado contra o schema v1.0.0 antes de sair do processo;
        a resposta também é validada, de modo que uma divergência de contrato
        entre Python e Rust falha explicitamente.
        """
        payload = dict(request)
        validate_contract("authorization_request", payload)
        with self._lock:
            self._send({"type": "Authorize", "body": {"request": payload}})
            frame = self._read_until({"AuthorizationResponse"}, timeout=timeout)
            result = (frame.get("body") or {}).get("result")
            if not isinstance(result, dict):
                raise BridgeProtocolError("AuthorizationResponse sem result")
            validate_contract("authorization_result", result)
            return result

    def approval_request(
        self,
        request: Mapping[str, Any],
        *,
        risk_level: str | None = None,
        ttl_s: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Cria uma solicitação HITL e retorna o ``approval_record``.

        ``risk_level`` e ``ttl_s`` são metadados do frame de controle, fora do
        contrato ``approval_request``; quando omitidos o daemon usa o TTL
        padrão e não grava classificação de risco.
        """
        payload = dict(request)
        validate_contract("approval_request", payload)
        body: dict[str, Any] = {"request": payload}
        if risk_level is not None:
            body["risk_level"] = risk_level
        if ttl_s is not None:
            body["ttl_s"] = float(ttl_s)
        with self._lock:
            self._send({"type": "ApprovalRequest", "body": body})
            frame = self._read_until({"ApprovalResponse"}, timeout=timeout)
            approval = (frame.get("body") or {}).get("approval")
            if not isinstance(approval, dict):
                raise BridgeProtocolError("ApprovalResponse sem approval")
            validate_contract("approval_record", approval)
            return approval

    def approval_decide(
        self,
        decision: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Registra uma decisão humana e retorna o ``approval_record`` atualizado."""
        payload = dict(decision)
        validate_contract("approval_decision", payload)
        with self._lock:
            self._send({"type": "ApprovalDecision", "body": {"decision": payload}})
            frame = self._read_until({"ApprovalResponse"}, timeout=timeout)
            approval = (frame.get("body") or {}).get("approval")
            if not isinstance(approval, dict):
                raise BridgeProtocolError("ApprovalResponse sem approval")
            validate_contract("approval_record", approval)
            return approval

    def approval_status(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Consulta o estado de uma aprovação."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        with self._lock:
            self._send(
                {"type": "ApprovalStatus", "body": {"approval_id": approval_id}}
            )
            frame = self._read_until({"ApprovalResponse"}, timeout=timeout)
            approval = (frame.get("body") or {}).get("approval")
            if not isinstance(approval, dict):
                raise BridgeProtocolError("ApprovalResponse sem approval")
            validate_contract("approval_record", approval)
            if approval.get("approval_id") != approval_id:
                raise BridgeProtocolError(
                    "ApprovalResponse com approval_id divergente"
                )
            return approval

    def approval_revoke(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Revoga uma aprovação PENDING/APPROVED e retorna o registro atualizado."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        with self._lock:
            self._send(
                {"type": "ApprovalRevoke", "body": {"approval_id": approval_id}}
            )
            frame = self._read_until({"ApprovalResponse"}, timeout=timeout)
            approval = (frame.get("body") or {}).get("approval")
            if not isinstance(approval, dict):
                raise BridgeProtocolError("ApprovalResponse sem approval")
            validate_contract("approval_record", approval)
            if approval.get("approval_id") != approval_id:
                raise BridgeProtocolError(
                    "ApprovalResponse com approval_id divergente"
                )
            if approval.get("status") != "REVOKED":
                raise BridgeProtocolError(
                    f"ApprovalResponse com status inesperado: {approval.get('status')!r}"
                )
            return approval

    def approval_list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Lista aprovações com filtro e paginação."""
        if limit < 1:
            raise ValueError("limit deve ser positivo")
        if offset < 0:
            raise ValueError("offset não pode ser negativo")
        body: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if status is not None:
            body["status"] = status
        with self._lock:
            self._send({"type": "ApprovalList", "body": body})
            frame = self._read_until({"ApprovalListResponse"}, timeout=timeout)
            approvals = (frame.get("body") or {}).get("approvals")
            if not isinstance(approvals, list):
                raise BridgeProtocolError("ApprovalListResponse sem approvals")
            for approval in approvals:
                if not isinstance(approval, dict):
                    raise BridgeProtocolError("ApprovalListResponse com item inválido")
                validate_contract("approval_record", approval)
            return approvals

    def approval_verify(
        self,
        approval_id: str,
        request: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> bool:
        """Verifica o vínculo por hash sem consumir a aprovação."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        payload = dict(request)
        validate_contract("approval_request", payload)
        with self._lock:
            self._send(
                {
                    "type": "ApprovalVerify",
                    "body": {"approval_id": approval_id, "request": payload},
                }
            )
            frame = self._read_until({"ApprovalVerifyResponse"}, timeout=timeout)
            valid = (frame.get("body") or {}).get("valid")
            if not isinstance(valid, bool):
                raise BridgeProtocolError("ApprovalVerifyResponse sem valid booleano")
            return valid

    def approval_claim(
        self,
        approval_id: str,
        request: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Reivindica uma aprovação ``APPROVED`` e retorna o registro CLAIMED."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        payload: dict[str, Any] = {"approval_id": approval_id}
        if request is not None:
            wire = dict(request)
            validate_contract("approval_request", wire)
            payload["request"] = wire
        with self._lock:
            self._send({"type": "ApprovalClaim", "body": payload})
            frame = self._read_until({"ApprovalResponse"}, timeout=timeout)
            approval = (frame.get("body") or {}).get("approval")
            if not isinstance(approval, dict):
                raise BridgeProtocolError("ApprovalResponse sem approval")
            validate_contract("approval_record", approval)
            if approval.get("approval_id") != approval_id:
                raise BridgeProtocolError(
                    "ApprovalResponse com approval_id divergente"
                )
            if approval.get("status") != "CLAIMED":
                raise BridgeProtocolError(
                    f"ApprovalResponse com status inesperado: {approval.get('status')!r}"
                )
            return approval

    def approval_expire(self, *, timeout: float | None = None) -> int:
        """Expira aprovações vencidas e retorna a contagem."""
        with self._lock:
            self._send({"type": "ApprovalExpire"})
            frame = self._read_until({"ApprovalExpireResponse"}, timeout=timeout)
            expired = (frame.get("body") or {}).get("expired")
            if not isinstance(expired, int) or isinstance(expired, bool):
                raise BridgeProtocolError("ApprovalExpireResponse sem expired inteiro")
            return expired

    def _execution_response(
        self,
        frame: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        body = frame.get("body") or {}
        approval = body.get("approval")
        execution = body.get("execution")
        if not isinstance(approval, dict):
            raise BridgeProtocolError(f"{operation}: resposta sem approval")
        if not isinstance(execution, dict):
            raise BridgeProtocolError(f"{operation}: resposta sem execution")
        validate_contract("approval_record", approval)
        return {"approval": approval, "execution": execution}

    def execution_start(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Inicia a execução no daemon: ``CLAIMED → CONSUMED`` + ``STARTING``."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        with self._lock:
            self._send(
                {"type": "ExecutionStart", "body": {"approval_id": approval_id}}
            )
            frame = self._read_until({"ExecutionResponse"}, timeout=timeout)
            body = self._execution_response(frame, "execution_start")
            if body["approval"].get("approval_id") != approval_id:
                raise BridgeProtocolError("ExecutionResponse com approval_id divergente")
            return body

    def execution_complete(
        self,
        approval_id: str,
        status: str,
        *,
        summary: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Fecha a execução e retorna aprovação + execução atualizadas."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        if status not in ("COMPLETED", "FAILED", "INTERRUPTED"):
            raise ValueError(f"status de execução inválido: {status!r}")
        payload: dict[str, Any] = {"approval_id": approval_id, "status": status}
        if summary is not None:
            payload["summary"] = summary
        with self._lock:
            self._send({"type": "ExecutionComplete", "body": payload})
            frame = self._read_until({"ExecutionResponse"}, timeout=timeout)
            body = self._execution_response(frame, "execution_complete")
            if body["approval"].get("approval_id") != approval_id:
                raise BridgeProtocolError("ExecutionResponse com approval_id divergente")
            return body

    def execution_status(
        self,
        approval_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Consulta aprovação + execução vinculada."""
        if not approval_id:
            raise ValueError("approval_id não pode ser vazio")
        with self._lock:
            self._send(
                {"type": "ExecutionStatus", "body": {"approval_id": approval_id}}
            )
            frame = self._read_until({"ExecutionResponse"}, timeout=timeout)
            body = self._execution_response(frame, "execution_status")
            if body["approval"].get("approval_id") != approval_id:
                raise BridgeProtocolError("ExecutionResponse com approval_id divergente")
            return body

    def execution_recover(
        self,
        timeout_s: float = 300.0,
        *,
        timeout: float | None = None,
    ) -> int:
        """Marca execuções órfãs antigas como ``UNKNOWN``."""
        if timeout_s < 0 or not isinstance(timeout_s, (int, float)):
            raise ValueError("timeout_s deve ser não negativo")
        with self._lock:
            self._send(
                {
                    "type": "ExecutionRecover",
                    "body": {"timeout_s": float(timeout_s)},
                }
            )
            frame = self._read_until({"ExecutionRecoverResponse"}, timeout=timeout)
            recovered = (frame.get("body") or {}).get("recovered")
            if not isinstance(recovered, int) or isinstance(recovered, bool):
                raise BridgeProtocolError(
                    "ExecutionRecoverResponse sem recovered inteiro"
                )
            return recovered


    # -- identidade (T8.3) --------------------------------------------------

    def identity_list(
        self,
        actor_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Lista principals; o daemon exige ``identity.principals.read``."""
        body: dict[str, Any] = {"actor_id": _identity_text("actor_id", actor_id)}
        if kind is not None:
            body["kind"] = _identity_text("kind", kind)
        if status is not None:
            body["status"] = _identity_text("status", status)
        with self._lock:
            self._send({"type": "IdentityList", "body": body})
            frame = self._read_until({"IdentityPrincipalsResponse"}, timeout=timeout)
            principals = (frame.get("body") or {}).get("principals")
            if not isinstance(principals, list) or not all(
                isinstance(item, dict) for item in principals
            ):
                raise BridgeProtocolError(
                    "IdentityPrincipalsResponse sem principals"
                )
            return principals

    def identity_get(
        self,
        actor_id: str,
        principal_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Consulta um principal; exige ``identity.principals.read``."""
        body = {
            "actor_id": _identity_text("actor_id", actor_id),
            "principal_id": _identity_text("principal_id", principal_id),
        }
        with self._lock:
            self._send({"type": "IdentityGet", "body": body})
            frame = self._read_until({"IdentityPrincipalResponse"}, timeout=timeout)
            principal = (frame.get("body") or {}).get("principal")
            if not isinstance(principal, dict):
                raise BridgeProtocolError("IdentityPrincipalResponse sem principal")
            return principal

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
        """Cria um principal; exige ``identity.principals.manage``."""
        if (
            not isinstance(max_autonomy, int)
            or isinstance(max_autonomy, bool)
            or not 0 <= max_autonomy <= 4
        ):
            raise ValueError("max_autonomy deve estar entre 0 e 4")
        body: dict[str, Any] = {
            "actor_id": _identity_text("actor_id", actor_id),
            "kind": _identity_text("kind", kind),
            "display_name": _identity_text("display_name", display_name),
            "role_id": _identity_text("role_id", role_id),
            "max_autonomy": max_autonomy,
        }
        if parent_id is not None:
            body["parent_id"] = _identity_text("parent_id", parent_id)
        with self._lock:
            self._send({"type": "IdentityCreatePrincipal", "body": body})
            frame = self._read_until({"IdentityPrincipalResponse"}, timeout=timeout)
            principal = (frame.get("body") or {}).get("principal")
            if not isinstance(principal, dict):
                raise BridgeProtocolError("IdentityPrincipalResponse sem principal")
            return principal

    def identity_revoke_principal(
        self,
        actor_id: str,
        principal_id: str,
        reason: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Revoga um principal; exige ``identity.principals.manage``."""
        body = {
            "actor_id": _identity_text("actor_id", actor_id),
            "principal_id": _identity_text("principal_id", principal_id),
            "reason": _identity_text("reason", reason),
        }
        with self._lock:
            self._send({"type": "IdentityRevokePrincipal", "body": body})
            frame = self._read_until({"IdentityRevocationResponse"}, timeout=timeout)
            summary = (frame.get("body") or {}).get("summary")
            if not isinstance(summary, dict):
                raise BridgeProtocolError("IdentityRevocationResponse sem summary")
            return summary

    def identity_effective_scopes(
        self,
        principal_id: str,
        *,
        timeout: float | None = None,
    ) -> list[str]:
        """Escopos efetivos calculados pelo daemon."""
        body = {"principal_id": _identity_text("principal_id", principal_id)}
        with self._lock:
            self._send({"type": "IdentityEffectiveScopes", "body": body})
            frame = self._read_until({"IdentityScopesResponse"}, timeout=timeout)
            scopes = (frame.get("body") or {}).get("scopes")
            if not isinstance(scopes, list) or not all(
                isinstance(scope, str) for scope in scopes
            ):
                raise BridgeProtocolError("IdentityScopesResponse sem scopes")
            return scopes

    def identity_permission_check(
        self,
        principal_id: str,
        scope: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Consulta fail-closed se o principal possui o escopo."""
        body = {
            "principal_id": _identity_text("principal_id", principal_id),
            "scope": _identity_text("scope", scope),
        }
        with self._lock:
            self._send({"type": "IdentityPermissionCheck", "body": body})
            frame = self._read_until({"IdentityPermissionResponse"}, timeout=timeout)
            response = frame.get("body") or {}
            allowed = response.get("allowed")
            if not isinstance(allowed, bool):
                raise BridgeProtocolError(
                    "IdentityPermissionResponse sem allowed booleano"
                )
            reason = response.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise BridgeProtocolError(
                    "IdentityPermissionResponse com reason inválido"
                )
            return {"allowed": allowed, "reason": reason}

    def identity_device_list(
        self,
        actor_id: str,
        *,
        principal_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Lista devices; exige ``identity.devices.read``."""
        body: dict[str, Any] = {"actor_id": _identity_text("actor_id", actor_id)}
        if principal_id is not None:
            body["principal_id"] = _identity_text("principal_id", principal_id)
        with self._lock:
            self._send({"type": "IdentityDeviceList", "body": body})
            frame = self._read_until({"IdentityDevicesResponse"}, timeout=timeout)
            devices = (frame.get("body") or {}).get("devices")
            if not isinstance(devices, list) or not all(
                isinstance(item, dict) for item in devices
            ):
                raise BridgeProtocolError("IdentityDevicesResponse sem devices")
            return devices

    def identity_device_revoke(
        self,
        actor_id: str,
        device_id: str,
        reason: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Revoga um device; exige ``identity.devices.manage``."""
        body = {
            "actor_id": _identity_text("actor_id", actor_id),
            "device_id": _identity_text("device_id", device_id),
            "reason": _identity_text("reason", reason),
        }
        with self._lock:
            self._send({"type": "IdentityDeviceRevoke", "body": body})
            frame = self._read_until({"IdentityRevocationResponse"}, timeout=timeout)
            summary = (frame.get("body") or {}).get("summary")
            if not isinstance(summary, dict):
                raise BridgeProtocolError("IdentityRevocationResponse sem summary")
            return summary

    def identity_pair_start(
        self,
        actor_id: str,
        role_id: str,
        secret_hash: str,
        *,
        code_hash: str | None = None,
        constraints: Mapping[str, Any] | None = None,
        ttl_s: float = 300.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Cria uma sessão de pairing; envia apenas hashes, nunca o segredo."""
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or ttl_s <= 0:
            raise ValueError("ttl_s deve ser positivo")
        body: dict[str, Any] = {
            "actor_id": _identity_text("actor_id", actor_id),
            "role_id": _identity_text("role_id", role_id),
            "secret_hash": _identity_sha256("secret_hash", secret_hash),
            "ttl_s": float(ttl_s),
        }
        if code_hash is not None:
            body["code_hash"] = _identity_sha256("code_hash", code_hash)
        if constraints is not None:
            if not isinstance(constraints, Mapping):
                raise ValueError("constraints deve ser um objeto")
            body["constraints"] = dict(constraints)
        with self._lock:
            self._send({"type": "IdentityPairStart", "body": body})
            frame = self._read_until({"IdentityPairingResponse"}, timeout=timeout)
            session = (frame.get("body") or {}).get("session")
            if not isinstance(session, dict):
                raise BridgeProtocolError("IdentityPairingResponse sem session")
            if "secret_hash" in session:
                raise BridgeProtocolError(
                    "IdentityPairingResponse não pode expor secret_hash"
                )
            return session


    def identity_init_owner(
        self,
        token_hash: str,
        *,
        display_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Bootstrap idempotente do owner local; envia apenas o hash do token."""
        body: dict[str, Any] = {
            "token_hash": _identity_sha256("token_hash", token_hash),
        }
        if display_name is not None:
            body["display_name"] = _identity_text("display_name", display_name)
        with self._lock:
            self._send({"type": "IdentityInitOwner", "body": body})
            frame = self._read_until({"IdentityOwnerResponse"}, timeout=timeout)
            return _identity_owner_body(frame)

    def identity_owner_status(
        self,
        token_hash: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Resolve o owner local pelo hash do token, sem criar nada."""
        body = {"token_hash": _identity_sha256("token_hash", token_hash)}
        with self._lock:
            self._send({"type": "IdentityOwnerStatus", "body": body})
            frame = self._read_until({"IdentityOwnerResponse"}, timeout=timeout)
            return _identity_owner_body(frame)


def _identity_owner_body(frame: Mapping[str, Any]) -> dict[str, Any]:
    body = frame.get("body") or {}
    initialized = body.get("initialized")
    if not isinstance(initialized, bool):
        raise BridgeProtocolError("IdentityOwnerResponse sem initialized booleano")
    principal = body.get("principal")
    if principal is not None and not isinstance(principal, dict):
        raise BridgeProtocolError("IdentityOwnerResponse com principal inválido")
    credential_id = body.get("credential_id")
    if credential_id is not None and not isinstance(credential_id, str):
        raise BridgeProtocolError("IdentityOwnerResponse com credential_id inválido")
    for flag in ("created_principal", "created_credential"):
        if not isinstance(body.get(flag), bool):
            raise BridgeProtocolError(f"IdentityOwnerResponse sem {flag} booleano")
    return {
        "initialized": initialized,
        "principal": principal,
        "credential_id": credential_id,
        "created_principal": body["created_principal"],
        "created_credential": body["created_credential"],
    }


__all__ = [
    "IPC_PROTOCOL_VERSION",
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeout",
    "BridgeVersionMismatch",
    "RustEventBridge",
]
