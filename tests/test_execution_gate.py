
import asyncio
import pytest
from unittest.mock import MagicMock
from nexus_core.automation import AutomationEngine, Tool, RiskLevel, AccessLevel, ExecutionMode, ApprovalRequired, AutomationDenied
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision
from nexus_core.policy import PolicyEngine, PolicyDecision as PolicyDec
from nexus_core.recorder import EventRecorder

@pytest.fixture
def mock_recorder():
    return MagicMock(spec=EventRecorder)

@pytest.fixture
def engine(mock_recorder):
    return AutomationEngine(
        autonomy_level=1,
        recorder=mock_recorder,
        approval_manager=ApprovalManager(),
        policy_engine=PolicyEngine()
    )

@pytest.mark.asyncio
async def test_policy_allow_low_risk(engine):
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
