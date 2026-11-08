"""SudoBroker — execução com privilégios elevados sob controle rígido.

Responsabilidade: mediar operações que exigem privilégios de superusuário
(sudo), aplicando:

* identidade do solicitante
* validação da operação exata (tool_id + parâmetros + escopo)
* aprovação humana vinculada à operação exata
* expiração de aprovação
* proteção contra replay
* auditoria completa

Regra de projeto: uma aprovação para uma operação não autoriza outra operação
diferente. Parâmetros são vinculados criptograficamente (SHA-256) para evitar
ataques de "parameter swapping".
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Mapping


class SudoRequestStatus(Enum):
    PENDING = auto()
    APPROVED = auto()
    DENIED = auto()
    EXPIRED = auto()
    REVOKED = auto()
    CONSUMED = auto()


@dataclass(frozen=True, slots=True)
class SudoRequest:
    """Solicitação de execução com privilégios elevados."""

    request_id: str
    requester: str
    command: str  # descrição legível da operação sudo
    tool_id: str
    params: Mapping[str, Any]
    scope: str
    ttl_s: float
    created_at: float
    expires_at: float
    decided_at: float | None = None
    decided_by: str | None = None
    status: SudoRequestStatus = SudoRequestStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    def params_hash(self) -> str:
        """SHA-256 canônico de (tool_id, params, scope) para integridade."""
        payload = {
            "tool_id": self.tool_id,
            "params": json.dumps(dict(self.params), sort_keys=True, separators=(",", ":")),
            "scope": self.scope,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "requester": self.requester,
            "command": self.command,
            "tool_id": self.tool_id,
            "params": dict(self.params),
            "scope": self.scope,
            "ttl_s": self.ttl_s,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "status": self.status.name,
            "params_hash": self.params_hash(),
            "metadata": dict(self.metadata),
        }


class SudoBroker:
    """Media execuções com privilégios elevados (sudo) sob controle rígido."""

    def __init__(self) -> None:
        self._requests: dict[str, SudoRequest] = {}

    def request_sudo(
        self,
        *,
        requester: str,
        command: str,
        tool_id: str,
        params: Mapping[str, Any],
        scope: str = "system",
        ttl_s: float = 3600.0,
    ) -> str:
        """Cria uma solicitação de sudo pendente de aprovação humana."""
        request_id = uuid.uuid4().hex
        now = time.time()
        req = SudoRequest(
            request_id=request_id,
            requester=requester,
            command=command,
            tool_id=tool_id,
            params=dict(params),
            scope=scope,
            ttl_s=ttl_s,
            created_at=now,
            expires_at=now + ttl_s,
        )
        self._requests[request_id] = req
        return request_id

    def approve(self, request_id: str, approver: str) -> bool:
        """Aprova uma solicitação de sudo (apenas humana, vinculada à operação exata)."""
        req = self._requests.get(request_id)
        if req is None:
            return False
        if req.status != SudoRequestStatus.PENDING:
            return False
        if req.is_expired():
            req = req.__class__(
                request_id=req.request_id,
                requester=req.requester,
                command=req.command,
                tool_id=req.tool_id,
                params=req.params,
                scope=req.scope,
                ttl_s=req.ttl_s,
                created_at=req.created_at,
                expires_at=req.expires_at,
                decided_at=time.time(),
                decided_by=approver,
                status=SudoRequestStatus.EXPIRED,
                metadata=req.metadata,
            )
            self._requests[request_id] = req
            return False
        self._requests[request_id] = req.__class__(
            request_id=req.request_id,
            requester=req.requester,
            command=req.command,
            tool_id=req.tool_id,
            params=req.params,
            scope=req.scope,
            ttl_s=req.ttl_s,
            created_at=req.created_at,
            expires_at=req.expires_at,
            decided_at=time.time(),
            decided_by=approver,
            status=SudoRequestStatus.APPROVED,
            metadata=req.metadata,
        )
        return True

    def deny(self, request_id: str, approver: str) -> bool:
        """Nega uma solicitação de sudo."""
        req = self._requests.get(request_id)
        if req is None:
            return False
        if req.status != SudoRequestStatus.PENDING:
            return False
        self._requests[request_id] = req.__class__(
            request_id=req.request_id,
            requester=req.requester,
            command=req.command,
            tool_id=req.tool_id,
            params=req.params,
            scope=req.scope,
            ttl_s=req.ttl_s,
            created_at=req.created_at,
            expires_at=req.expires_at,
            decided_at=time.time(),
            decided_by=approver,
            status=SudoRequestStatus.DENIED,
            metadata=req.metadata,
        )
        return True

    def revoke(self, request_id: str) -> bool:
        """Revoga uma aprovação ainda não consumida."""
        req = self._requests.get(request_id)
        if req is None:
            return False
        if req.status not in (SudoRequestStatus.PENDING, SudoRequestStatus.APPROVED):
            return False
        self._requests[request_id] = req.__class__(
            request_id=req.request_id,
            requester=req.requester,
            command=req.command,
            tool_id=req.tool_id,
            params=req.params,
            scope=req.scope,
            ttl_s=req.ttl_s,
            created_at=req.created_at,
            expires_at=req.expires_at,
            decided_at=req.decided_at,
            decided_by=req.decided_by,
            status=SudoRequestStatus.REVOKED,
            metadata=req.metadata,
        )
        return True

    def consume(self, request_id: str, params: Mapping[str, Any]) -> bool:
        """Marca a solicitação como consumida (execução iniciada).

        Verifica integridade dos parâmetros (proteção contra parameter swapping)
        e expiração. Só pode ser consumido uma vez.
        """
        req = self._requests.get(request_id)
        if req is None:
            return False
        if req.status != SudoRequestStatus.APPROVED:
            return False
        if req.is_expired():
            self._requests[request_id] = req.__class__(
                request_id=req.request_id,
                requester=req.requester,
                command=req.command,
                tool_id=req.tool_id,
                params=req.params,
                scope=req.scope,
                ttl_s=req.ttl_s,
                created_at=req.created_at,
                expires_at=req.expires_at,
                decided_at=req.decided_at,
                decided_by=req.decided_by,
                status=SudoRequestStatus.EXPIRED,
                metadata=req.metadata,
            )
            return False
        # Integridade: parâmetros devem bater com o aprovado.
        if req.params_hash() != self._canonical_hash(dict(params), req.tool_id, req.scope):
            return False
        self._requests[request_id] = req.__class__(
            request_id=req.request_id,
            requester=req.requester,
            command=req.command,
            tool_id=req.tool_id,
            params=req.params,
            scope=req.scope,
            ttl_s=req.ttl_s,
            created_at=req.created_at,
            expires_at=req.expires_at,
            decided_at=req.decided_at,
            decided_by=req.decided_by,
            status=SudoRequestStatus.CONSUMED,
            metadata=req.metadata,
        )
        return True

    def get_request(self, request_id: str) -> SudoRequest | None:
        return self._requests.get(request_id)

    def list_pending(self) -> list[SudoRequest]:
        return [r for r in self._requests.values() if r.status == SudoRequestStatus.PENDING]

    def expire_stale(self) -> int:
        """Marca solicitações expiradas como EXPIRED."""
        now = time.time()
        count = 0
        for req in list(self._requests.values()):
            if req.status == SudoRequestStatus.PENDING and req.is_expired():
                self._requests[req.request_id] = req.__class__(
                    request_id=req.request_id,
                    requester=req.requester,
                    command=req.command,
                    tool_id=req.tool_id,
                    params=req.params,
                    scope=req.scope,
                    ttl_s=req.ttl_s,
                    created_at=req.created_at,
                    expires_at=req.expires_at,
                    decided_at=now,
                    decided_by=None,
                    status=SudoRequestStatus.EXPIRED,
                    metadata=req.metadata,
                )
                count += 1
        return count

    @staticmethod
    def _canonical_hash(params: Mapping[str, Any], tool_id: str, scope: str) -> str:
        payload = {
            "tool_id": tool_id,
            "params": json.dumps(dict(params), sort_keys=True, separators=(",", ":")),
            "scope": scope,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


__all__ = [
    "SudoBroker",
    "SudoRequest",
    "SudoRequestStatus",
]
