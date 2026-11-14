"""Paridade de decisão entre Python e Rust (T5).

O fixture ``contracts/fixtures/parity/policy_cases.json`` é normativo e lido
pelos testes Python e Rust. Ele fixa as decisões do ``PolicyEngine`` e do
``ExecutionGate._classify_risk`` para a mesma matriz de risco, acesso, escopo e
autonomia; qualquer divergência falha nos dois lados.
"""

from __future__ import annotations

import pytest

from nexus_core.approval import ApprovalManager
from nexus_core.automation_types import AccessLevel, RiskLevel, Tool
from nexus_core.contracts import load_fixture
from nexus_core.execution_gate import ExecutionGate
from nexus_core.policy import PolicyContext, PolicyEngine


def _cases() -> list[dict]:
    fixture = load_fixture("parity/policy_cases.json")
    assert fixture["contract_version"] == "1.0.0"
    return fixture["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_policy_engine_matches_rust_parity_cases(case: dict) -> None:
    tool = Tool(
        id="parity.tool",
        version="1.0",
        description="policy parity",
        risk=RiskLevel[case["risk_level"]],
        access=AccessLevel[case["access_level"]],
        required_autonomy=case["required_autonomy"],
    )
    context = PolicyContext(
        autonomy_level=case["autonomy_level"],
        fs_scope=case["fs_scope"],
        net_scope="NONE",
        resource_limits="default",
    )
    decision, _reason = PolicyEngine().evaluate(tool, context)
    assert decision.name == case["policy_decision"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_execution_gate_classification_matches_rust_parity_cases(
    case: dict, tmp_path
) -> None:
    gate = ExecutionGate(approval_manager=ApprovalManager(tmp_path / "approvals.db"))
    context = PolicyContext(
        autonomy_level=case["autonomy_level"],
        fs_scope=case["fs_scope"],
        net_scope="NONE",
        resource_limits="default",
    )
    decision, _reason = gate._classify_risk(
        case["risk_level"],
        context,
        access_level=case["access_level"],
        required_autonomy=case["required_autonomy"],
    )
    assert decision.name == case["gate_decision"]
