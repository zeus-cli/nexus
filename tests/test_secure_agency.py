"""Testes dos componentes de Secure Agency (Fase 3)."""

from __future__ import annotations

import asyncio
import time
import tempfile
from pathlib import Path

import pytest

from nexus_core.tool_registry import (
    ToolRegistry,
    ToolSpec,
    ToolRiskLevel,
    ToolAccessLevel,
    ToolExecutionMode,
    ToolNotFound,
    ToolRegistryError,
)
from nexus_core.execution_gate import (
    ExecutionGate,
    GateDecision,
    GateRequest,
    GateResult,
    ExecutionGateError,
)
from nexus_core.rollback_manager import RollbackManager, RollbackStatus
from nexus_core.sudo_broker import SudoBroker, SudoRequestStatus
from nexus_core.execution_journal import (
    ExecutionJournal,
    ExecutionJournalEntry,
    ExecutionStatusJournal,
)
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision
from nexus_core.policy import PolicyEngine
from nexus_core.sandbox import SandboxExecutor, ResourceLimits
from nexus_core.recorder import EventRecorder
from nexus_core.events import Event
from nexus_core.memory.store import Database
from nexus_core.memory.records import MemoryStore


# --------------------------------------------------------------------------- #
# Tool Registry
# --------------------------------------------------------------------------- #

def test_tool_registry_register_and_list():
    reg = ToolRegistry()
    spec = ToolSpec(
        tool_id="fs.read",
        version="1.0",
        description="Read a file",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
        execution_mode=ToolExecutionMode.HOST,
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    reg.register(spec)
    assert reg.get("fs.read") is spec
    assert len(reg.list_tools()) == 1
    assert reg.list_tools_dict()[0]["tool_id"] == "fs.read"


def test_tool_registry_duplicate_rejected():
    reg = ToolRegistry()
    spec = ToolSpec(
        tool_id="fs.read",
        version="1.0",
        description="Read a file",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
    )
    reg.register(spec)
    with pytest.raises(ToolRegistryError):
        reg.register(spec)


def test_tool_registry_unregister():
    reg = ToolRegistry()
    spec = ToolSpec(
        tool_id="fs.read",
        version="1.0",
        description="Read a file",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
    )
    reg.register(spec)
    assert reg.unregister("fs.read") is True
    assert reg.get("fs.read") is None
    assert reg.unregister("fs.read") is False


def test_tool_registry_validate_input():
    reg = ToolRegistry()
    spec = ToolSpec(
        tool_id="fs.read",
        version="1.0",
        description="Read a file",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    reg.register(spec)
    reg.validate_input("fs.read", {"path": "/etc/hostname"})
    with pytest.raises(ValueError):
        reg.validate_input("fs.read", {})  # missing required 'path'
    with pytest.raises(ToolNotFound):
        reg.validate_input("nonexistent", {})


def test_tool_registry_validate_input_rejects_bool_for_integer():
    """Booleanos não devem ser aceitos como inteiros (bool é subclasse de int)."""
    reg = ToolRegistry()
    spec = ToolSpec(
        tool_id="test.count",
        version="1.0",
        description="Count items",
        risk=ToolRiskLevel.LOW,
        access=ToolAccessLevel.READ,
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    reg.register(spec)

    reg.validate_input("test.count", {"count": 0})
    reg.validate_input("test.count", {"count": -7})
    for value in (True, False):
        with pytest.raises(ValueError, match="must be an integer"):
            reg.validate_input("test.count", {"count": value})


# --------------------------------------------------------------------------- #
# Execution Gate
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_execution_gate_low_risk_allowed():
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="python-runtime",
        tool_id="sys.uptime",
        params={},
        task_id="task-1",
        risk_level=ToolRiskLevel.LOW,
        justification="read uptime",
    )
    assert result.decision == GateDecision.ALLOWED
    assert result.approved is True
    assert result.execution_id is not None


@pytest.mark.asyncio
async def test_execution_gate_critical_denied():
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="python-runtime",
        tool_id="rm.rf",
        params={},
        task_id="task-1",
        risk_level=ToolRiskLevel.CRITICAL,
        justification="delete everything",
    )
    assert result.decision == GateDecision.DENIED
    assert result.approved is False


@pytest.mark.asyncio
async def test_execution_gate_requires_approval_high():
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="python-runtime",
        tool_id="system.shutdown",
        params={},
        task_id="task-1",
        risk_level=ToolRiskLevel.HIGH,
        justification="shutdown system",
    )
    assert result.decision == GateDecision.REQUIRES_APPROVAL
    assert result.approved is False
    assert "approval_id" in result.metadata


@pytest.mark.asyncio
async def test_execution_gate_execute_then_commit():
    gate = ExecutionGate()
    auth = await gate.authorize(
        component_id="python-runtime",
        tool_id="sys.uptime",
        params={},
        task_id="task-1",
        risk_level=ToolRiskLevel.LOW,
        justification="read uptime",
    )
    assert auth.approved is True

    executed = await gate.execute(auth.execution_id, runner=lambda p: {"uptime": "10 mins"})
    assert executed["ok"] is True
    assert executed["result"] == {"uptime": "10 mins"}

    # Execução não pode ser feita duas vezes com o mesmo request (status mudou).
    with pytest.raises(ExecutionGateError):
        await gate.execute(auth.execution_id, runner=lambda p: {"uptime": "again"})


@pytest.mark.asyncio
async def test_execution_gate_unknown_identity_denied():
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="unknown-component",
        tool_id="sys.uptime",
        params={},
        task_id="task-1",
        risk_level=ToolRiskLevel.LOW,
        justification="read uptime",
    )
    # unknown-component não está na lista de trusted; mas agent-* é trusted.
    assert result.decision == GateDecision.DENIED


# --------------------------------------------------------------------------- #
# Rollback Manager
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_rollback_manager_record_and_rollback():
    mgr = RollbackManager()
    op_id = mgr.record_operation(
        tool_id="fs.write",
        params={"path": "/tmp/x", "content": "hello"},
        result={"written": 5},
        reversible=True,
        rollback_handler="fs.write.undo",
    )
    mgr.register_rollback_handler("fs.write.undo", lambda p: True)
    ok = await mgr.rollback(op_id)
    assert ok is True
    record = mgr.get_record(op_id)
    assert record.status == RollbackStatus.COMPLETED


@pytest.mark.asyncio
async def test_rollback_not_reversible():
    mgr = RollbackManager()
    op_id = mgr.record_operation(
        tool_id="external.api",
        params={"endpoint": "https://example.com"},
        result={},
        reversible=False,
    )
    ok = await mgr.rollback(op_id)
    assert ok is False
    record = mgr.get_record(op_id)
    assert record.status == RollbackStatus.NOT_REVERSIBLE


# --------------------------------------------------------------------------- #
# Sudo Broker
# --------------------------------------------------------------------------- #

def test_sudo_broker_request_approve_consume():
    broker = SudoBroker()
    rid = broker.request_sudo(
        requester="admin",
        command="restart service",
        tool_id="svc.restart",
        params={"service": "nginx"},
        scope="system",
        ttl_s=3600,
    )
    req = broker.get_request(rid)
    assert req.status == SudoRequestStatus.PENDING

    assert broker.approve(rid, "human-1") is True
    assert broker.get_request(rid).status == SudoRequestStatus.APPROVED

    assert broker.consume(rid, {"service": "nginx"}) is True
    assert broker.get_request(rid).status == SudoRequestStatus.CONSUMED


def test_sudo_broker_parameter_swap_rejected():
    broker = SudoBroker()
    rid = broker.request_sudo(
        requester="admin",
        command="restart service",
        tool_id="svc.restart",
        params={"service": "nginx"},
        scope="system",
        ttl_s=3600,
    )
    broker.approve(rid, "human-1")
    # Tentativa de consumir com parâmetros diferente (parameter swapping).
    assert broker.consume(rid, {"service": "apache"}) is False
    assert broker.get_request(rid).status == SudoRequestStatus.APPROVED  # não consumido


def test_sudo_broker_revoke_before_consume():
    broker = SudoBroker()
    rid = broker.request_sudo(
        requester="admin",
        command="restart service",
        tool_id="svc.restart",
        params={"service": "nginx"},
        scope="system",
        ttl_s=3600,
    )
    broker.approve(rid, "human-1")
    assert broker.revoke(rid) is True
    assert broker.get_request(rid).status == SudoRequestStatus.REVOKED
    assert broker.consume(rid, {"service": "nginx"}) is False


def test_sudo_broker_expiry():
    broker = SudoBroker()
    rid = broker.request_sudo(
        requester="admin",
        command="restart service",
        tool_id="svc.restart",
        params={"service": "nginx"},
        scope="system",
        ttl_s=0.1,  # expira rapidamente
    )
    import time
    time.sleep(0.15)
    assert broker.approve(rid, "human-1") is False  # já expirou
    assert broker.get_request(rid).status == SudoRequestStatus.EXPIRED


# --------------------------------------------------------------------------- #
# Execution Journal
# --------------------------------------------------------------------------- #

def test_execution_journal_append_and_verify():
    with tempfile.TemporaryDirectory() as tmpdir:
        journal = ExecutionJournal(Path(tmpdir) / "journal.db")
        eid = journal.record_entry(
            request_id="req-1",
            component_id="python-runtime",
            tool_id="sys.uptime",
            params={},
            status=ExecutionStatusJournal.COMPLETED,
            result={"uptime": "10 mins"},
            task_id="task-1",
        )
        entry = journal.get_entry(eid)
        assert entry is not None
        assert entry.verify_integrity() is True
        assert entry.status == ExecutionStatusJournal.COMPLETED

        # Tentativa de alterar o resultado deve invalidar o hash (não é possível
        # via API pública, mas o hash está vinculado ao conteúdo original).
        assert journal.verify_all_integrity()["ok"] is True


def test_execution_journal_update_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        journal = ExecutionJournal(Path(tmpdir) / "journal.db")
        eid = journal.record_entry(
            request_id="req-2",
            component_id="python-runtime",
            tool_id="sys.uptime",
            params={},
            status=ExecutionStatusJournal.AUTHORIZED,
            task_id="task-1",
        )
        journal.update_status(
            "req-2",
            ExecutionStatusJournal.COMPLETED,
            result={"uptime": "10 mins"},
            completed_at=time.time(),
        )
        entry = journal.get_entry(eid)
        assert entry.status == ExecutionStatusJournal.COMPLETED
        assert entry.result == {"uptime": "10 mins"}
        # Atualizações legítimas de status precisam atualizar o hash.
        assert entry.verify_integrity() is True
        assert journal.verify_all_integrity()["ok"] is True


def test_execution_journal_list_and_integrity():
    with tempfile.TemporaryDirectory() as tmpdir:
        journal = ExecutionJournal(Path(tmpdir) / "journal.db")
        for i in range(3):
            journal.record_entry(
                request_id=f"req-{i}",
                component_id="python-runtime",
                tool_id=f"tool.{i}",
                params={"idx": i},
                status=ExecutionStatusJournal.COMPLETED if i % 2 == 0 else ExecutionStatusJournal.FAILED,
                task_id="task-1",
            )
        report = journal.verify_all_integrity()
        assert report["total"] == 3
        assert report["invalid"] == 0
        assert report["ok"] is True

        entries = journal.list_entries(status=ExecutionStatusJournal.COMPLETED)
        assert len(entries) == 2


__all__ = []
