"""Rollback Manager — reversão de operações executadas.

Responsabilidade: registrar operações executadas e, quando possível, reverter
seus efeitos. Operações irreversíveis (ex.: alterações externas irrevogáveis)
são marcadas como tal e exigem validação prévia mais forte.

Regra de projeto: rollback não é assumido como possível para ações externas
irreversíveis; tais ações exigem validação pré-execução mais forte.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping


class RollbackStatus(Enum):
    PENDING = auto()      # aguardando execução do rollback
    IN_PROGRESS = auto()  # rollback em andamento
    COMPLETED = auto()    # rollback concluído com sucesso
    FAILED = auto()       # rollback falhou
    NOT_REVERSIBLE = auto()  # operação não é reversível


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    """Registro de uma operação e seu potencial de reversão."""

    operation_id: str
    tool_id: str
    params: Mapping[str, Any]
    result: Mapping[str, Any] | None
    reversible: bool
    rollback_handler: str | None  # id do handler de reversão (se disponível)
    status: RollbackStatus
    created_at: float
    completed_at: float | None
    error: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "tool_id": self.tool_id,
            "params": dict(self.params),
            "result": dict(self.result) if self.result else None,
            "reversible": self.reversible,
            "rollback_handler": self.rollback_handler,
            "status": self.status.name,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class RollbackManager:
    """Gerencia registros de reversão de operações."""

    def __init__(self) -> None:
        self._records: dict[str, RollbackRecord] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register_rollback_handler(self, handler_id: str, handler: Callable[..., Any]) -> None:
        """Registra um handler de reversão para um tipo de operação."""
        self._handlers[handler_id] = handler

    def record_operation(
        self,
        *,
        tool_id: str,
        params: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        reversible: bool = False,
        rollback_handler: str | None = None,
    ) -> str:
        """Registra uma operação executada para possível reversão posterior."""
        operation_id = uuid.uuid4().hex
        record = RollbackRecord(
            operation_id=operation_id,
            tool_id=tool_id,
            params=dict(params),
            result=dict(result) if result else None,
            reversible=reversible,
            rollback_handler=rollback_handler,
            status=RollbackStatus.PENDING,
            created_at=time.monotonic(),
            completed_at=None,
            error=None,
        )
        self._records[operation_id] = record
        return operation_id

    def get_record(self, operation_id: str) -> RollbackRecord | None:
        return self._records.get(operation_id)

    async def rollback(self, operation_id: str) -> bool:
        """Tenta reverter uma operação registrada.

        Retorna True se o rollback foi concluído com sucesso, False caso
        contrário (incluindo operações não reversíveis).
        """
        record = self._records.get(operation_id)
        if record is None:
            return False
        if not record.reversible:
            # Marca como não reversível e não tenta.
            self._records[operation_id] = record.__class__(
                operation_id=record.operation_id,
                tool_id=record.tool_id,
                params=record.params,
                result=record.result,
                reversible=record.reversible,
                rollback_handler=record.rollback_handler,
                status=RollbackStatus.NOT_REVERSIBLE,
                created_at=record.created_at,
                completed_at=time.monotonic(),
                error="operation not reversible",
                metadata=record.metadata,
            )
            return False
        if record.status in (
            RollbackStatus.COMPLETED,
            RollbackStatus.FAILED,
            RollbackStatus.NOT_REVERSIBLE,
        ):
            return record.status == RollbackStatus.COMPLETED

        # Marca como em andamento.
        self._records[operation_id] = record.__class__(
            operation_id=record.operation_id,
            tool_id=record.tool_id,
            params=record.params,
            result=record.result,
            reversible=record.reversible,
            rollback_handler=record.rollback_handler,
            status=RollbackStatus.IN_PROGRESS,
            created_at=record.created_at,
            completed_at=None,
            error=None,
            metadata=record.metadata,
        )

        try:
            if record.rollback_handler is not None and record.rollback_handler in self._handlers:
                handler = self._handlers[record.rollback_handler]
                if asyncio_run_coroutine_or_call(handler, record.params):
                    self._records[operation_id] = record.__class__(
                        operation_id=record.operation_id,
                        tool_id=record.tool_id,
                        params=record.params,
                        result=record.result,
                        reversible=record.reversible,
                        rollback_handler=record.rollback_handler,
                        status=RollbackStatus.COMPLETED,
                        created_at=record.created_at,
                        completed_at=time.monotonic(),
                        error=None,
                        metadata=record.metadata,
                    )
                    return True
                else:
                    raise RuntimeError("rollback handler returned false")
            else:
                # Sem handler: não é possível reverter automaticamente.
                self._records[operation_id] = record.__class__(
                    operation_id=record.operation_id,
                    tool_id=record.tool_id,
                    params=record.params,
                    result=record.result,
                    reversible=record.reversible,
                    rollback_handler=record.rollback_handler,
                    status=RollbackStatus.FAILED,
                    created_at=record.created_at,
                    completed_at=time.monotonic(),
                    error="no rollback handler registered",
                    metadata=record.metadata,
                )
                return False
        except Exception as exc:  # noqa: BLE001
            self._records[operation_id] = record.__class__(
                operation_id=record.operation_id,
                tool_id=record.tool_id,
                params=record.params,
                result=record.result,
                reversible=record.reversible,
                rollback_handler=record.rollback_handler,
                status=RollbackStatus.FAILED,
                created_at=record.created_at,
                completed_at=time.monotonic(),
                error=str(exc)[:512],
                metadata=record.metadata,
            )
            return False

    def list_records(self) -> list[RollbackRecord]:
        return list(self._records.values())

    def reversible_operations(self) -> list[RollbackRecord]:
        return [r for r in self._records.values() if r.reversible and r.status == RollbackStatus.PENDING]


def asyncio_run_coroutine_or_call(handler: Callable[..., Any], params: Mapping[str, Any]) -> bool:
    """Executa um handler síncrono ou assíncrono com os parâmetros."""
    import asyncio
    import inspect

    if not inspect.iscoroutinefunction(handler):
        return bool(handler(params))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return bool(asyncio.run(handler(params)))
    raise RuntimeError(
        "rollback assíncrono não pode ser executado de dentro de um event loop ativo"
    )


__all__ = [
    "RollbackManager",
    "RollbackRecord",
    "RollbackStatus",
]
