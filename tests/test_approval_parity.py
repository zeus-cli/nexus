"""Paridade do ciclo de vida de aprovação entre Python e Rust (T5).

O fixture ``contracts/fixtures/parity/approval_lifecycle.json`` é normativo e
executado pelos dois lados. Ele cobre PENDING/APPROVED/DENIED/CLAIMED/CONSUMED/
EXPIRED/REVOKED, TTL por solicitação, vínculo por hash e uso único.
"""

from __future__ import annotations

import pytest

from nexus_core.approval import ApprovalDecision, ApprovalManager, ApprovalRequest
from nexus_core.contracts import load_fixture


def _fixture() -> dict:
    fixture = load_fixture("parity/approval_lifecycle.json")
    assert fixture["contract_version"] == "1.0.0"
    return fixture


def _scenarios() -> list[dict]:
    return _fixture()["scenarios"]


def _pick(step: dict, original: ApprovalRequest, variant: ApprovalRequest):
    return variant if step.get("params") == "variant" else original


def _status(manager: ApprovalManager, request_id: str) -> str:
    # ``decide``/``claim`` já expiram; a leitura do HUD também precisa refletir
    # a expiração, por isso ``expire_stale`` antes de ``get_request``.
    manager.expire_stale()
    row = manager.get_request(request_id)
    assert row is not None
    return row["status"]


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda item: item["name"])
def test_approval_lifecycle_matches_rust(scenario: dict, tmp_path) -> None:
    fixture = _fixture()
    original = ApprovalRequest(**fixture["request"])
    variant = ApprovalRequest(**fixture["variant_request"])
    manager = ApprovalManager(tmp_path / "approvals.db")
    request_id: str | None = None

    for step in scenario["steps"]:
        op = step["op"]
        if op == "create":
            ttl = step.get("ttl_seconds", fixture["default_ttl_seconds"])
            request_id = manager.request_approval(original, ttl_s=ttl)
            row = manager.get_request(request_id)
            assert row is not None
            assert row["params_hash"] == fixture["expected_hash"]
            assert row["status"] == step["expect_status"]
            continue

        assert request_id is not None
        if op == "verify_action":
            assert (
                manager.verify_action(request_id, _pick(step, original, variant))
                is step["expect"]
            )
            continue
        if op == "expire_stale":
            assert manager.expire_stale() == step["expect_expired"]
            continue
        if op == "status":
            ok = True
        elif op == "decide":
            try:
                manager.decide(
                    request_id,
                    ApprovalDecision(step["decision"], "operator"),
                )
                ok = True
            except ValueError:
                ok = False
        elif op == "claim":
            # O ExecutionGate Python chama verify_action antes de
            # claim_approval; o claim do Rust faz as duas checagens juntas.
            ok = manager.verify_action(
                request_id, _pick(step, original, variant)
            ) and manager.claim_approval(request_id)
        elif op == "consume":
            try:
                manager.mark_starting(request_id)
                ok = True
            except ValueError:
                ok = False
        elif op == "revoke":
            try:
                manager.revoke(request_id)
                ok = True
            except ValueError:
                ok = False
        else:
            raise AssertionError(f"op desconhecida: {op}")

        if "expect_ok" in step:
            assert ok is step["expect_ok"], step
        if "expect_status" in step:
            assert _status(manager, request_id) == step["expect_status"], step
