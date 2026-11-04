
import pytest
import asyncio
import time
import sqlite3
from pathlib import Path
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision, ApprovalStatus, ExecutionStatus

DB_PATH = "/tmp/test_nexus_approvals.db"

@pytest.fixture
def manager():
    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    return ApprovalManager(db_path=DB_PATH)

def create_req(task_id="t1", plan_id="p1", tool_id="sys.uptime", params=None):
    return ApprovalRequest(
        tool_id=tool_id,
        params=params or {"timeout": 10},
        task_id=task_id,
        plan_id=plan_id,
        execution_mode="HOST",
        access_level="READ",
        fs_scope="/tmp",
        net_scope="NONE",
        resource_limits="mem=512M",
        policy_version="1.0",
        justification="Test request"
    )

@pytest.mark.asyncio
async def test_basic_lifecycle(manager):
    req = create_req()
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    assert manager.claim_approval(rid) is True
    manager.consume_approval(rid, "Success")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM approvals WHERE id = ?", (rid,)).fetchone()
        assert row[0] == ApprovalStatus.CONSUMED.value

@pytest.mark.asyncio
async def test_concurrency_claim(manager):
    req = create_req()
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    async def claim_task():
        return manager.claim_approval(rid)
    results = await asyncio.gather(claim_task(), claim_task())
    assert sum(results) == 1

@pytest.mark.asyncio
async def test_parameter_modification(manager):
    req = create_req(params={"file": "a.txt"})
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    req_modified = create_req(params={"file": "b.txt"})
    assert manager.verify_action(rid, req_modified) is False
    assert manager.verify_action(rid, req) is True

@pytest.mark.asyncio
async def test_expiration(manager):
    req = create_req()
    rid = manager.request_approval(req, ttl_s=0.1)
    # Must be APPROVED to be claimable
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    await asyncio.sleep(0.2)
    assert manager.claim_approval(rid) is False
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM approvals WHERE id = ?", (rid,)).fetchone()
        assert row[0] == ApprovalStatus.EXPIRED.value

@pytest.mark.asyncio
async def test_revocation(manager):
    req = create_req()
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    manager.revoke(rid)
    assert manager.claim_approval(rid) is False

@pytest.mark.asyncio
async def test_canonicalization(manager):
    req1 = create_req(params={"a": 1, "b": 2})
    req2 = create_req(params={"b": 2, "a": 1})
    rid1 = manager.request_approval(req1)
    assert manager.verify_action(rid1, req2) is True

@pytest.mark.asyncio
async def test_crash_recovery_state(manager):
    req = create_req()
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    manager.claim_approval(rid)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM executions WHERE approval_id = ?", (rid,)).fetchone()
        assert row[0] == ExecutionStatus.PREPARED.value
        app_row = conn.execute("SELECT status FROM approvals WHERE id = ?", (rid,)).fetchone()
        assert app_row[0] == ApprovalStatus.CLAIMED.value

@pytest.mark.asyncio
async def test_reuse_rejection(manager):
    req = create_req()
    rid = manager.request_approval(req)
    manager.decide(rid, ApprovalDecision("APPROVED", "human-1"))
    manager.claim_approval(rid)
    manager.consume_approval(rid)
    assert manager.claim_approval(rid) is False

@pytest.mark.asyncio
async def test_invalid_params_hash(manager):
    req = create_req()
    rid = manager.request_approval(req)
    req_wrong = create_req(tool_id="wrong.tool")
    assert manager.verify_action(rid, req_wrong) is False
