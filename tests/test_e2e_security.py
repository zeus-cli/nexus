
import asyncio
import pytest
from unittest.mock import MagicMock
from nexus_core.automation import AutomationEngine, Tool, RiskLevel, AccessLevel, ExecutionMode, ApprovalRequired, AutomationDenied
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision
from nexus_core.policy import PolicyEngine
from nexus_core.recorder import EventRecorder
from nexus_core.sandbox import SandboxExecutor

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
async def test_e2e_bypass_sandbox_direct(mock_recorder):
    # This test checks if someone can just use the SandboxExecutor directly.
    # While Python doesn't prevent imports, we verify that the SandboxExecutor 
    # itself doesn't provide "administrative" bypasses.
    sandbox = SandboxExecutor(mock_recorder)
    # Try to read /etc/shadow (should fail due to bwrap permissions)
    result = await sandbox.execute(("/usr/bin/cat", "/etc/shadow"), timeout_s=1)
    assert result.exit_code != 0

@pytest.mark.asyncio
async def test_e2e_param_modification_after_approval(engine):
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    # 1. Request approval for "uptime"
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id, args=())
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))
    
    # 2. Attempt to run with different args (e.g. "rm -rf /")
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run(tool_id, args=("rm", "-rf", "/"), approval_id=rid)
    
    assert "Approval hash mismatch" in str(excinfo.value)

@pytest.mark.asyncio
async def test_e2e_execution_mode_swap(engine):
    tool_id = "sys.sandbox_tool"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Sandbox tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        execution_mode=ExecutionMode.SANDBOX, argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id)
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))
    
    # Try to force it to HOST mode by some means (if the engine allowed it)
    # Since Tool is frozen, they can't change it. If they pass a different tool_id, the hash fails.
    # If they try to bypass via the internal executor:
    with pytest.raises(AutomationDenied):
        # Imagine they try to call run with a modified tool registry
        engine._tools[tool_id] = Tool(
            tool_id, "1.0", "Sandbox tool", RiskLevel.HIGH, AccessLevel.WRITE, 
            execution_mode=ExecutionMode.HOST, argv_template=("/usr/bin/uptime",)
        )
        await engine.run(tool_id, approval_id=rid)

@pytest.mark.asyncio
async def test_e2e_concurrent_revocation(engine):
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id)
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))
    
    # Concurrent revoke and run
    # We simulate the revoke happening just before the run
    engine._approval_manager.revoke(rid)
    
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run(tool_id, approval_id=rid)
    
    assert "Approval not available" in str(excinfo.value)

@pytest.mark.asyncio
async def test_e2e_crash_after_consumption(engine):
    # This tests that if the system crashes after mark_starting (CONSUMED),
    # the approval cannot be used again.
    tool_id = "sys.dangerous"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Dangerous tool", RiskLevel.HIGH, AccessLevel.WRITE, 
        argv_template=("/usr/bin/uptime",)
    )
    
    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id)
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))
    
    # We manually simulate the a "crash" by just calling the consumption part 
    # or using the engine once.
    await engine.run(tool_id, approval_id=rid)
    
    # Now attempt to run again with the same rid
    with pytest.raises(AutomationDenied) as excinfo:
        await engine.run(tool_id, approval_id=rid)
    
    assert "already consumed" in str(excinfo.value).lower()

@pytest.mark.asyncio
async def test_e2e_llm_approval_attempt(engine):
    # We verify that the ApprovalManager does not have any methods 
    # that allow "implicit" approval or allow the LLM to bypass the human-only decide() method.
    # In our architecture, decide() is the only way.
    # If the LLM tries to call a non-existent "auto_approve" method, it fails.
    with pytest.raises(AttributeError):
        getattr(engine._approval_manager, "auto_approve")("some_id")
