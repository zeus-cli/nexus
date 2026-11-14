"""Contratos JSON versionados entre Python e Rust.

A fonte da verdade são os arquivos em ``contracts/schemas``. Este módulo
carrega os schemas, valida fixtures e converte objetos do domínio Python para
o formato de wire. Os testes Rust validam os mesmos schemas e fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .approval import ApprovalDecision, ApprovalRequest, approval_request_hash
from .events import Event, Severity, parse_timestamp

CONTRACT_VERSION = "1.0.0"
CONTRACTS_ROOT = Path(__file__).resolve().parent.parent / "contracts"
SCHEMA_DIR = CONTRACTS_ROOT / "schemas"
FIXTURES_DIR = CONTRACTS_ROOT / "fixtures"

_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _check_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parse_timestamp(value)
    except ValueError:
        return False
    return True


class ContractError(ValueError):
    """Erro genérico de contrato."""


class ContractValidationError(ContractError):
    """Instância não conforma com o schema versionado."""

    def __init__(self, schema_name: str, errors: list[ValidationError]) -> None:
        self.schema_name = schema_name
        self.errors = errors
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors[:5]
        )
        super().__init__(f"contrato {schema_name!r} inválido: {details}")


@cache
def load_manifest() -> dict[str, Any]:
    path = FIXTURES_DIR / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(
            "versão do manifesto "
            f"{manifest.get('contract_version')!r} != {CONTRACT_VERSION!r}"
        )
    return manifest


@cache
def load_schema(schema_name: str) -> dict[str, Any]:
    if not schema_name or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in schema_name
    ):
        raise ContractError(f"nome de schema inválido: {schema_name!r}")
    path = SCHEMA_DIR / f"{schema_name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def validator_for(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        load_schema(schema_name),
        format_checker=_FORMAT_CHECKER,
    )


def validate_contract(schema_name: str, instance: Any) -> None:
    """Valida ``instance`` contra o schema; levanta erro com os caminhos."""
    errors = sorted(
        validator_for(schema_name).iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if errors:
        raise ContractValidationError(schema_name, errors)


def is_valid_contract(schema_name: str, instance: Any) -> bool:
    return validator_for(schema_name).is_valid(instance)


def load_fixture(relative_path: str) -> Any:
    path = (FIXTURES_DIR / relative_path).resolve()
    if not path.is_relative_to(FIXTURES_DIR.resolve()):
        raise ContractError(f"fixture fora do diretório permitido: {relative_path!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def _iso_utc(value: Any) -> str:
    timestamp = parse_timestamp(value, "created_at")
    return timestamp.isoformat().replace("+00:00", "Z")


def event_to_wire(event: Event) -> dict[str, Any]:
    """Formato canônico de evento. ``dedup_key`` é derivado, não trafega."""
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "source": event.source,
        "severity": event.severity.value,
        "payload": dict(event.payload),
        "correlation_id": event.correlation_id,
        "created_at": _iso_utc(event.created_at),
        "dedup": event.dedup,
    }


def event_from_wire(data: Mapping[str, Any]) -> Event:
    """Valida o contrato e reconstrói um ``Event`` Python."""
    validate_contract("event", dict(data))
    return Event(
        kind=data["kind"],
        source=data["source"],
        payload=data["payload"],
        severity=Severity(data["severity"]),
        event_id=data["event_id"],
        correlation_id=data["correlation_id"],
        created_at=parse_timestamp(data["created_at"], "event.created_at"),
        dedup=data["dedup"],
    )


def _enum_name(value: Any) -> str:
    return getattr(value, "name", str(value))


def authorization_request_contract(
    *,
    component_id: str,
    tool_id: str,
    params: Mapping[str, Any],
    task_id: str,
    plan_id: str = "default",
    risk_level: Any = "LOW",
    autonomy_level: int = 1,
    required_autonomy: int = 1,
    approval_id: str | None = None,
    execution_mode: Any = "HOST",
    access_level: Any = "READ",
    policy_version: str = "1.0",
    required_capability: str | None = None,
    fs_scope: str = "DEFAULT",
    net_scope: str = "NONE",
    resource_limits: str = "default",
    justification: str = "",
) -> dict[str, Any]:
    """Monta a solicitação de autorização no formato de wire."""
    return {
        "component_id": component_id,
        "tool_id": tool_id,
        "params": dict(params),
        "task_id": task_id,
        "plan_id": plan_id,
        "risk_level": _enum_name(risk_level),
        "autonomy_level": autonomy_level,
        "required_autonomy": required_autonomy,
        "approval_id": approval_id,
        "execution_mode": _enum_name(execution_mode),
        "access_level": _enum_name(access_level),
        "policy_version": policy_version,
        "required_capability": required_capability,
        "fs_scope": fs_scope,
        "net_scope": net_scope,
        "resource_limits": resource_limits,
        "justification": justification,
    }


def approval_request_to_contract(request: ApprovalRequest) -> dict[str, Any]:
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


def approval_record_to_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    """Converte a linha do SQLite (``id``) no contrato (``approval_id``)."""
    return {
        "approval_id": row["id"],
        "tool_id": row["tool_id"],
        "params_hash": row["params_hash"],
        "task_id": row["task_id"],
        "plan_id": row["plan_id"],
        "execution_mode": row["execution_mode"],
        "access_level": row["access_level"],
        "fs_scope": row["fs_scope"],
        "net_scope": row["net_scope"],
        "resource_limits": row["resource_limits"],
        "policy_version": row["policy_version"],
        "justification": row["justification"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "consumed_at": row["consumed_at"],
        "risk_level": row["risk_level"],
    }


def approval_decision_to_contract(
    approval_id: str, decision: ApprovalDecision
) -> dict[str, Any]:
    value = decision.decision.upper()
    if value not in ("APPROVED", "DENIED"):
        raise ContractError(f"decisão inválida: {decision.decision!r}")
    return {
        "approval_id": approval_id,
        "decision": value,
        "approver": decision.approver,
        "timestamp": decision.timestamp,
    }


def approval_hash_from_contract(request: Mapping[str, Any]) -> str:
    return approval_request_hash(ApprovalRequest(**dict(request)))


__all__ = [
    "CONTRACTS_ROOT",
    "CONTRACT_VERSION",
    "ContractError",
    "ContractValidationError",
    "approval_decision_to_contract",
    "approval_hash_from_contract",
    "approval_record_to_contract",
    "approval_request_to_contract",
    "authorization_request_contract",
    "event_from_wire",
    "event_to_wire",
    "is_valid_contract",
    "load_fixture",
    "load_manifest",
    "load_schema",
    "validate_contract",
    "validator_for",
]
