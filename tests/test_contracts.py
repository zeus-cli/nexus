from __future__ import annotations

import pytest

from nexus_core.approval import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    approval_request_hash,
)
from nexus_core.automation import ActionResult
from nexus_core.automation_types import AccessLevel, ExecutionMode, RiskLevel
from nexus_core.contracts import (
    CONTRACT_VERSION,
    ContractValidationError,
    approval_decision_to_contract,
    approval_hash_from_contract,
    approval_record_to_contract,
    approval_request_to_contract,
    authorization_request_contract,
    event_from_wire,
    event_to_wire,
    load_fixture,
    load_manifest,
    load_schema,
    validate_contract,
)
from nexus_core.events import Event
from nexus_core.execution_gate import GateDecision, GateResult
from nexus_core.tool_registry import (
    ToolAccessLevel,
    ToolRiskLevel,
    ToolSpec,
)

MANIFEST = load_manifest()
VALID_FIXTURES = MANIFEST["valid"]
INVALID_FIXTURES = MANIFEST["invalid"]
APPROVAL_VECTORS = [
    entry for entry in MANIFEST["vectors"] if entry["kind"] == "approval_hash"
]
EVENT_VECTORS = [
    entry for entry in MANIFEST["vectors"] if entry["kind"] == "event_dedup"
]


def test_manifest_and_schema_versions_are_consistent() -> None:
    assert MANIFEST["contract_version"] == CONTRACT_VERSION
    for entry in VALID_FIXTURES + INVALID_FIXTURES:
        schema = load_schema(entry["schema"])
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].startswith("urn:nexus:contracts:v1:")
        load_fixture(entry["file"])


@pytest.mark.parametrize(
    "entry", VALID_FIXTURES, ids=[entry["file"] for entry in VALID_FIXTURES]
)
def test_valid_fixtures_match_schemas(entry: dict) -> None:
    validate_contract(entry["schema"], load_fixture(entry["file"]))


@pytest.mark.parametrize(
    "entry", INVALID_FIXTURES, ids=[entry["file"] for entry in INVALID_FIXTURES]
)
def test_invalid_fixtures_are_rejected(entry: dict) -> None:
    with pytest.raises(ContractValidationError):
        validate_contract(entry["schema"], load_fixture(entry["file"]))


def test_event_wire_roundtrip_and_derived_fields_are_not_transmitted() -> None:
    wire = load_fixture("valid/event.json")
    event = event_from_wire(wire)

    assert event.kind == "agent.task.started"
    assert event.correlation_id == "corr-contract-1"
    assert event_to_wire(event) == wire
    validate_contract("event", event_to_wire(event))

    # dedup_key é derivado; não pode entrar no wire.
    with pytest.raises(ContractValidationError):
        validate_contract("event", {**wire, "dedup_key": event.dedup_key()})


def test_event_timestamp_is_really_parsed() -> None:
    wire = load_fixture("valid/event.json")
    with pytest.raises(ContractValidationError):
        validate_contract("event", {**wire, "created_at": "not-a-date"})
    with pytest.raises(ValueError):
        event_from_wire({**wire, "created_at": "not-a-date"})


@pytest.mark.parametrize(
    "entry", EVENT_VECTORS, ids=[entry["file"] for entry in EVENT_VECTORS]
)
def test_event_dedup_vectors_match_python(entry: dict) -> None:
    vector = load_fixture(entry["file"])
    event = event_from_wire(vector["event"])

    assert event.dedup_key() == vector["expected_dedup_key"]


def test_python_domain_objects_validate_against_contracts() -> None:
    event = event_from_wire(load_fixture("valid/event.json"))
    validate_contract("event", event_to_wire(event))

    result = GateResult(
        decision=GateDecision.ALLOWED,
        request_id="req-contract-1",
        reason="ok",
        approved=True,
        execution_id="req-contract-1",
    )
    validate_contract("authorization_result", result.as_dict())

    action = ActionResult(
        tool_id="sys.uptime",
        exit_code=0,
        stdout="up 1 day\n",
        stderr="",
        duration_ms=12,
        truncated=False,
    )
    validate_contract("execution_result", action.as_dict())

    spec = ToolSpec(
        tool_id="sys.uptime",
        version="1.0",
        description="Get system uptime",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
    )
    validate_contract("tool_spec", spec.as_dict())


def test_authorization_request_helper_matches_fixture() -> None:
    contract = authorization_request_contract(
        component_id="automation_engine",
        tool_id="sys.dangerous",
        params={"args": []},
        task_id="task-contract-1",
        plan_id="plan-contract-1",
        risk_level=RiskLevel.HIGH,
        autonomy_level=1,
        required_autonomy=1,
        approval_id=None,
        execution_mode=ExecutionMode.HOST,
        access_level=AccessLevel.WRITE,
        policy_version="1.0",
        required_capability=None,
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        justification="high risk operations require explicit human approval",
    )

    assert contract == load_fixture("valid/authorization_request.json")
    validate_contract("authorization_request", contract)


@pytest.mark.parametrize(
    "entry", APPROVAL_VECTORS, ids=[entry["file"] for entry in APPROVAL_VECTORS]
)
def test_approval_hash_vectors(entry: dict, tmp_path) -> None:
    vector = load_fixture(entry["file"])
    request = ApprovalRequest(**vector["request"])
    assert approval_hash_from_contract(vector["request"]) == vector["expected_hash"]

    manager = ApprovalManager(db_path=str(tmp_path / "approvals.db"))
    assert manager._canonicalize_and_hash(request) == vector["expected_hash"]

    validate_contract("approval_request", approval_request_to_contract(request))

    approval_id = manager.request_approval(request)
    stored = manager.get_request(approval_id)
    assert stored is not None
    record = approval_record_to_contract(stored)
    assert record["params_hash"] == vector["expected_hash"]
    validate_contract("approval_record", record)


def test_approval_record_and_decision_contracts(tmp_path) -> None:
    vector = load_fixture("vectors/approval_hash.json")
    request = ApprovalRequest(**vector["request"])
    manager = ApprovalManager(db_path=str(tmp_path / "approvals.db"))
    approval_id = manager.request_approval(request)

    decision = ApprovalDecision(decision="APPROVED", approver="operator")
    manager.decide(approval_id, decision)
    decided = manager.get_request(approval_id)
    assert decided is not None
    validate_contract("approval_record", approval_record_to_contract(decided))
    validate_contract(
        "approval_decision",
        approval_decision_to_contract(approval_id, decision),
    )


def test_non_ijson_numbers_are_outside_the_hash_domain() -> None:
    vector = load_fixture("vectors/approval_hash.json")
    unsafe_integer = 9_007_199_254_740_992
    request = ApprovalRequest(**{**vector["request"], "params": {"n": unsafe_integer}})

    with pytest.raises(ValueError):
        approval_request_hash(request)

    event = Event(kind="system.metrics", source="test", payload={"n": unsafe_integer})
    assert event.dedup_key() is None
