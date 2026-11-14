
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from nexus_core.automation import AutomationEngine, Tool, RiskLevel, AccessLevel, ExecutionMode, ApprovalRequired, AutomationDenied
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision
from nexus_core.execution_gate import ExecutionGate, ExecutionGateError, GateDecision
from nexus_core.policy import PolicyEngine, PolicyDecision as PolicyDec
from nexus_core.recorder import EventRecorder

@pytest.fixture
def mock_recorder():
    return MagicMock(spec=EventRecorder)

@pytest.fixture
def engine(mock_recorder, tmp_path):
    return AutomationEngine(
        autonomy_level=1,
        recorder=mock_recorder,
        approval_manager=ApprovalManager(db_path=str(tmp_path / "approvals.db")),
        policy_engine=PolicyEngine()
    )

@pytest.mark.asyncio
async def test_policy_allow_low_risk(engine):
    result = await engine.run("sys.uptime")
    assert result.exit_code == 0

def _approval_request(
    *,
    tool_id: str = "sys.dangerous",
    params: dict | None = None,
    task_id: str = "task-1",
    plan_id: str = "plan-1",
    execution_mode: str = "HOST",
    access_level: str = "WRITE",
    justification: str = "manual review",
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_id=tool_id,
        params={"args": ()} if params is None else params,
        task_id=task_id,
        plan_id=plan_id,
        execution_mode=execution_mode,
        access_level=access_level,
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        policy_version="1.0",
        justification=justification,
    )


async def _authorize_existing_approval(
    gate: ExecutionGate,
    approval_id: str,
    *,
    tool_id: str = "sys.dangerous",
    params: dict | None = None,
    justification: str = "manual review",
):
    return await gate.authorize(
        component_id="automation_engine",
        tool_id=tool_id,
        params={"args": ()} if params is None else params,
        task_id="task-1",
        plan_id="plan-1",
        risk_level=RiskLevel.HIGH,
        autonomy_level=1,
        approval_id=approval_id,
        execution_mode="HOST",
        access_level="WRITE",
        policy_version="1.0",
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        justification=justification,
    )


@pytest.mark.asyncio
async def test_automation_engine_identity_is_trusted_by_execution_gate(engine, tmp_path):
    engine._execution_gate = ExecutionGate(
        approval_manager=ApprovalManager(db_path=str(tmp_path / "gate-approvals.db"))
    )

    result = await engine.run("sys.uptime")

    assert result.exit_code == 0

@pytest.mark.asyncio
async def test_policy_require_approval_high_risk(engine):
    engine._tools["sys.dangerous"] = Tool(
        "sys.dangerous", "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run("sys.dangerous")
    
    assert excinfo.value.request_id is not None

@pytest.mark.asyncio
async def test_high_risk_execution_resumes_after_human_approval(tmp_path):
    approvals = ApprovalManager(db_path=str(tmp_path / "hitl.db"))
    gate = ExecutionGate(approval_manager=approvals)
    authorized = await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.dangerous",
        params={"target": "x"},
        task_id="task-1",
        risk_level=RiskLevel.HIGH,
    )
    assert authorized.decision == GateDecision.REQUIRES_APPROVAL
    approval_id = authorized.metadata["approval_id"]

    with pytest.raises(ExecutionGateError):
        await gate.execute(authorized.request_id, runner=lambda params: {"ran": True})

    approvals.decide(
        approval_id, ApprovalDecision(decision="APPROVED", approver="operator")
    )
    outcome = await gate.execute(
        authorized.request_id, runner=lambda params: {"ran": True}
    )
    assert outcome == {
        "ok": True,
        "request_id": authorized.request_id,
        "result": {"ran": True},
    }
    assert approvals.get_request(approval_id)["status"] == "CONSUMED"

    # Replay: a execução não pode rodar duas vezes.
    with pytest.raises(ExecutionGateError):
        await gate.execute(authorized.request_id, runner=lambda params: {"ran": True})


@pytest.mark.asyncio
async def test_policy_deny_critical_risk(engine):
    engine._tools["sys.critical"] = Tool(
        "sys.critical", "1.0", "Critical tool", RiskLevel.CRITICAL, AccessLevel.WRITE
    )
    
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run("sys.critical")
    
    assert "Policy DENY" in str(excinfo.value)

@pytest.mark.asyncio
async def test_execution_with_valid_approval(engine):
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id)
    rid = excinfo.value.request_id
    
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="approver_1"))
    
    result = await engine.run(tool_id, approval_id=rid)
    assert result.exit_code == 0

@pytest.mark.asyncio
async def test_execution_with_invalid_approval_hash(engine):
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id, args=())
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="approver_1"))
    
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run(tool_id, args=("evil_arg",), approval_id=rid)
    
    assert "Approval hash mismatch" in str(excinfo.value)

@pytest.mark.asyncio
async def test_execution_with_consumed_approval(engine):
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id)
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="approver_1"))
    
    await engine.run(tool_id, approval_id=rid)
    
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run(tool_id, approval_id=rid)
    
    assert "already consumed" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_valid_existing_approval_is_allowed_and_commit_closes_execution(tmp_path):
    approvals = ApprovalManager(db_path=str(tmp_path / "gate.db"))
    gate = ExecutionGate(approval_manager=approvals)
    approval_id = approvals.request_approval(_approval_request())
    approvals.decide(
        approval_id, ApprovalDecision(decision="APPROVED", approver="operator")
    )

    authorized = await _authorize_existing_approval(gate, approval_id)

    assert authorized.decision == GateDecision.ALLOWED
    assert authorized.execution_id == authorized.request_id

    committed = await gate.commit(
        authorized.execution_id, result={"stdout": "ok", "exit_code": 0}
    )
    assert committed["ok"] is True
    execution = gate.get_execution(authorized.execution_id)
    assert execution["status"] == "completed"
    assert execution["result"] == {"stdout": "ok", "exit_code": 0}
    # O gate valida a aprovação, mas o ciclo de vida pertence ao chamador.
    assert approvals.get_request(approval_id)["status"] == "APPROVED"

    with pytest.raises(ExecutionGateError):
        await gate.commit(authorized.execution_id, result={})


@pytest.mark.asyncio
async def test_tampered_params_are_denied_by_gate(tmp_path):
    approvals = ApprovalManager(db_path=str(tmp_path / "gate.db"))
    gate = ExecutionGate(approval_manager=approvals)
    approval_id = approvals.request_approval(_approval_request())
    approvals.decide(
        approval_id, ApprovalDecision(decision="APPROVED", approver="operator")
    )

    denied = await _authorize_existing_approval(
        gate, approval_id, params={"args": ("evil_arg",)}
    )

    assert denied.decision == GateDecision.DENIED
    assert "approval" in denied.reason
    assert gate.list_executions() == []


@pytest.mark.asyncio
async def test_expired_existing_approval_is_denied(tmp_path):
    approvals = ApprovalManager(db_path=str(tmp_path / "gate.db"))
    gate = ExecutionGate(approval_manager=approvals)
    approval_id = approvals.request_approval(_approval_request())
    approvals.decide(
        approval_id, ApprovalDecision(decision="APPROVED", approver="operator")
    )
    record = approvals.get_request(approval_id)

    with patch(
        "nexus_core.execution_gate.time.time",
        return_value=float(record["expires_at"]) + 1,
    ):
        denied = await _authorize_existing_approval(gate, approval_id)

    assert denied.decision == GateDecision.DENIED
    assert "expired" in denied.reason


@pytest.mark.asyncio
async def test_gate_uses_real_autonomy_and_scope_for_low_risk(tmp_path):
    gate = ExecutionGate(
        approval_manager=ApprovalManager(db_path=str(tmp_path / "gate.db"))
    )

    insufficient = await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.uptime",
        params={"args": ()},
        task_id="task-1",
        risk_level=RiskLevel.LOW,
        autonomy_level=0,
        required_autonomy=1,
    )
    assert insufficient.decision == GateDecision.REQUIRES_APPROVAL
    assert "autonomy" in insufficient.reason

    allowed = await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.uptime",
        params={"args": ()},
        task_id="task-1",
        risk_level=RiskLevel.LOW,
        autonomy_level=1,
        required_autonomy=1,
    )
    assert allowed.decision == GateDecision.ALLOWED

    write_outside_safe_zone = await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.write",
        params={"args": ()},
        task_id="task-1",
        risk_level=RiskLevel.LOW,
        autonomy_level=4,
        required_autonomy=1,
        access_level="WRITE",
        fs_scope="DEFAULT",
    )
    assert write_outside_safe_zone.decision == GateDecision.REQUIRES_APPROVAL


@pytest.mark.asyncio
async def test_commit_refuses_execution_with_pending_approval(tmp_path):
    gate = ExecutionGate(
        approval_manager=ApprovalManager(db_path=str(tmp_path / "gate.db"))
    )

    pending = await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.dangerous",
        params={"args": ()},
        task_id="task-1",
        risk_level=RiskLevel.HIGH,
    )

    assert pending.decision == GateDecision.REQUIRES_APPROVAL
    assert pending.execution_id is None
    with pytest.raises(ExecutionGateError, match="still requires approval"):
        await gate.commit(pending.request_id, result={"ran": True})


@pytest.mark.asyncio
async def test_automation_execution_uses_injected_gate_and_consumes_approval(tmp_path):
    approvals = ApprovalManager(db_path=str(tmp_path / "automation-gate.db"))
    gate = ExecutionGate(approval_manager=approvals)
    calls = []

    async def runner(argv, timeout):
        calls.append((argv, timeout))
        return 0, b"up 1 day\n", b""

    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        runner=runner,
        approval_manager=approvals,
        policy_engine=PolicyEngine(),
        execution_gate=gate,
    )
    engine._tools["sys.dangerous"] = Tool(
        "sys.dangerous",
        "1.0",
        "Dangerous tool",
        RiskLevel.HIGH,
        AccessLevel.WRITE,
        ExecutionMode.HOST,
        argv_template=("/usr/bin/uptime",),
    )

    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run("sys.dangerous", task_id="task-hitl")
    approval_id = excinfo.value.request_id
    assert calls == []
    assert gate.list_executions() == []

    approvals.decide(
        approval_id, ApprovalDecision(decision="APPROVED", approver="operator")
    )
    result = await engine.run(
        "sys.dangerous", task_id="task-hitl", approval_id=approval_id
    )

    assert result.exit_code == 0
    assert result.stdout == "up 1 day\n"
    assert calls == [(("/usr/bin/uptime",), 10.0)]
    assert approvals.get_request(approval_id)["status"] == "CONSUMED"
    executions = gate.list_executions()
    assert len(executions) == 1
    assert executions[0]["status"] == "completed"
    assert executions[0]["result"]["exit_code"] == 0
    assert executions[0]["result"]["stdout"] == "up 1 day\n"
