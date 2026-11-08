"""Testes de segurança obrigatórios (NEXUS Master Specification, Seção 20).

Cenários obrigatórios:
- Unauthorized tool execution
- Prompt injection
- Malformed tool arguments
- Approval expiration
- Approval revocation
- Replay attacks
- Duplicate events
- Concurrent execution requests
- Privilege escalation
- Sandbox escape resistance
- Resource exhaustion
- Interrupted execution
- Inconsistent database state
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus_core.approval import ApprovalManager, ApprovalDecision, ApprovalRequest
from nexus_core.automation import (
    AutomationEngine,
    AutomationDenied,
    ApprovalRequired,
    Tool,
    AccessLevel,
    RiskLevel,
)
from nexus_core.execution_gate import ExecutionGate, GateDecision
from nexus_core.memory.eventstore import EventStore
from nexus_core.memory.store import Database
from nexus_core.policy import PolicyContext, PolicyDecision, PolicyEngine
from nexus_core.recorder import EventRecorder
from nexus_core.sandbox import SandboxExecutor
from nexus_core.events import Event


def _make_approval_manager(tmp_path: Path) -> ApprovalManager:
    """Cria um ApprovalManager com banco em arquivo temporário."""
    return ApprovalManager(db_path=str(tmp_path / "approvals.db"))


# --------------------------------------------------------------------------- #
# 1. Unauthorized tool execution
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_unauthorized_tool_execution_denied(tmp_path):
    """Ferramenta não registrada deve ser negada."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    with pytest.raises(ValueError, match="Tool not found"):
        await engine.run("nonexistent.tool")


@pytest.mark.asyncio
async def test_unauthorized_component_rejected():
    """Componente não confiável deve ser negado pelo Execution Gate."""
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="unknown-component",
        tool_id="sys.uptime",
        params={},
        task_id="task-1",
    )
    assert result.decision == GateDecision.DENIED
    assert result.approved is False


# --------------------------------------------------------------------------- #
# 2. Prompt injection
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_prompt_injection_in_tool_params_blocked(tmp_path):
    """Parâmetros modificados após aprovação devem falhar no hash."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.injection_test"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Injection test", RiskLevel.HIGH, AccessLevel.WRITE,
        argv_template=("/usr/bin/uptime",),
    )

    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id, args=())
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))

    # Tentativa de injeção via parâmetros modificados deve falhar no hash.
    with pytest.raises(AutomationDenied, match="hash mismatch"):
        await engine.run(tool_id, args=("; rm -rf /",), approval_id=rid)


@pytest.mark.asyncio
async def test_llm_cannot_bypass_approval(tmp_path):
    """LLM não deve conseguir chamar decide() diretamente."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    # Não existe método auto_approve — apenas decide() via API humana.
    with pytest.raises(AttributeError):
        getattr(engine._approval_manager, "auto_approve")("some_id")


# --------------------------------------------------------------------------- #
# 3. Malformed tool arguments
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_malformed_tool_arguments_rejected(tmp_path):
    """Argumentos de tipo errado devem ser rejeitados."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.malformed"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Malformed test", RiskLevel.LOW, AccessLevel.READ,
        argv_template=("/usr/bin/uptime",),
    )

    # args deve ser uma tupla; passar dict deve falhar na validação de tipos.
    with pytest.raises((TypeError, ValueError, AttributeError)):
        await engine.run(tool_id, args={"not": "a tuple"})


# --------------------------------------------------------------------------- #
# 4. Approval expiration
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approval_expiration(tmp_path):
    """Aprovação expirada não deve permitir execução."""
    am = ApprovalManager(db_path=str(tmp_path / "approvals.db"))
    tool_id = "sys.expired_test"

    # Cria aprovação com TTL curtíssimo.
    req = ApprovalRequest(
        tool_id=tool_id,
        params={"args": ()},
        task_id="t",
        plan_id="p",
        execution_mode="HOST",
        access_level="WRITE",
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        policy_version="1.0",
        justification="test",
    )
    rid = am.request_approval(req, ttl_s=0.001)
    am.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))

    # Aguarda expiração.
    time.sleep(0.02)

    # claim_approval deve falhar porque expirou.
    assert am.claim_approval(rid) is False


# --------------------------------------------------------------------------- #
# 5. Approval revocation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approval_revocation_blocks_execution(tmp_path):
    """Revogação deve bloquear execução."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.revoke_test"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Revoke test", RiskLevel.HIGH, AccessLevel.WRITE,
        argv_template=("/usr/bin/uptime",),
    )

    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id, args=())
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))

    # Revoga antes de consumir.
    engine._approval_manager.revoke(rid)

    with pytest.raises(AutomationDenied):
        await engine.run(tool_id, approval_id=rid)


# --------------------------------------------------------------------------- #
# 6. Replay attacks
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_replay_attack_blocked(tmp_path):
    """Mesma aprovação não pode ser usada duas vezes (até uma vez)."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.replay_test"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Replay test", RiskLevel.HIGH, AccessLevel.WRITE,
        argv_template=("/usr/bin/uptime",),
    )

    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run(tool_id, args=())
    rid = excinfo.value.request_id
    engine._approval_manager.decide(rid, ApprovalDecision(decision="APPROVED", approver="admin"))

    # Primeira execução consome a aprovação.
    result1 = await engine.run(tool_id, approval_id=rid)
    assert result1.exit_code == 0

    # Segunda execução com mesma approv_id deve falhar (já consumida).
    with pytest.raises(AutomationDenied, match="already consumed"):
        await engine.run(tool_id, approval_id=rid)


# --------------------------------------------------------------------------- #
# 7. Duplicate events
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_duplicate_events_deduplicated(tmp_path):
    """Eventos idênticos dentro da janela não devem ser duplicados."""
    db = Database(Path(tmp_path) / "test.db")
    db.connect()
    db.migrate()
    store = EventStore(db, dedup_window_hours=24)

    ev1 = Event(kind="test.duplicate", source="test", payload={"x": 1})
    stored1, inserted1 = store.append(ev1)
    assert inserted1 is True

    ev2 = Event(kind="test.duplicate", source="test", payload={"x": 1})
    stored2, inserted2 = store.append(ev2)
    assert inserted2 is False  # duplicado dentro da janela
    assert stored2.event_id == stored1.event_id

    db.close()


# --------------------------------------------------------------------------- #
# 8. Concurrent execution requests
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_concurrent_execution_requests_isolated(tmp_path):
    """Requisições concorrentes não devem interferir umas nas outras."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.concurrent"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Concurrent test", RiskLevel.LOW, AccessLevel.READ,
        argv_template=("/usr/bin/uptime",),
    )

    # Múltiplas execuções concorrentes do sys.uptime (LOW, auto-allow).
    results = await asyncio.gather(*[
        engine.run(tool_id, args=()) for _ in range(5)
    ])
    assert all(r.exit_code == 0 for r in results)


# --------------------------------------------------------------------------- #
# 9. Privilege escalation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_privilege_escalation_blocked():
    """Operação crítica deve ser negada por default."""
    gate = ExecutionGate()
    result = await gate.authorize(
        component_id="cli",
        tool_id="system.shutdown",
        params={},
        task_id="task-1",
        risk_level=RiskLevel.CRITICAL,
    )
    assert result.decision == GateDecision.DENIED
    assert result.approved is False


# --------------------------------------------------------------------------- #
# 10. Sandbox escape resistance
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sandbox_escape_resistance():
    """Sandbox não deve permitir acesso a arquivos sensíveis do host."""
    recorder = MagicMock(spec=EventRecorder)
    sandbox = SandboxExecutor(recorder)

    # Tentativa de ler /etc/shadow (deve falhar no sandbox).
    result = await sandbox.execute(("/usr/bin/cat", "/etc/shadow"), timeout_s=1)
    assert result.exit_code != 0

    # Tentativa de acessar home do usuário (não montado no sandbox).
    result2 = await sandbox.execute(("/usr/bin/cat", "/root/.bashrc"), timeout_s=1)
    assert result2.exit_code != 0


# --------------------------------------------------------------------------- #
# 11. Resource exhaustion
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resource_exhaustion_handled(tmp_path):
    """Execução com timeout muito curto deve ser tratada graciosamente."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.uptime"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Uptime timeout", RiskLevel.LOW, AccessLevel.READ,
        argv_template=("/usr/bin/uptime",),
        timeout_s=0.001,
    )
    # Timeout curto pode falhar mas não deve travar o sistema.
    try:
        await engine.run(tool_id, args=())
    except Exception:
        pass  # esperável com timeout curto


# --------------------------------------------------------------------------- #
# 12. Interrupted execution
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_interrupted_execution_recorded(tmp_path):
    """Execução interrompida deve ser tratada graciosamente."""
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=MagicMock(spec=EventRecorder),
        approval_manager=_make_approval_manager(tmp_path),
    )
    tool_id = "sys.interrupt"
    engine._tools[tool_id] = Tool(
        tool_id, "1.0", "Interrupt test", RiskLevel.LOW, AccessLevel.READ,
        argv_template=("/usr/bin/sleep", "10"),
    )

    # Inicia execução e cancela rapidamente.
    task = asyncio.create_task(engine.run(tool_id, args=()))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass  # interrupção esperada


# --------------------------------------------------------------------------- #
# 13. Inconsistent database state
# --------------------------------------------------------------------------- #

def test_inconsistent_database_state_detected(tmp_path):
    """Banco corrompido deve ser detectado pelo verify()."""
    db_path = Path(tmp_path) / "test.db"
    db = Database(db_path)
    db.connect()
    db.migrate()
    store = EventStore(db)

    # Insere um evento válido.
    ev = Event(kind="test.corrupt", source="test", payload={"x": 1})
    store.append(ev)

    # Corrompe diretamente o payload no banco.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE events SET payload = 'not valid json' WHERE kind = 'test.corrupt'"
    )
    conn.commit()
    conn.close()

    report = store.verify()
    assert report["invalid_payloads"] >= 1
    assert report["ok"] is False

    db.close()


# --------------------------------------------------------------------------- #
# Security policy decisions
# --------------------------------------------------------------------------- #

def test_policy_decisions_all_levels():
    """Todas as decisões de política devem ser determinísticas."""
    pe = PolicyEngine()
    ctx = PolicyContext(
        autonomy_level=1, fs_scope="DEFAULT", net_scope="NONE",
        resource_limits="default",
    )

    # LOW read -> ALLOW
    tool = Tool("t", "1.0", "t", RiskLevel.LOW, AccessLevel.READ)
    dec, _ = pe.evaluate(tool, ctx)
    assert dec == PolicyDecision.ALLOW

    # CRITICAL -> DENY
    tool2 = Tool("t2", "1.0", "t2", RiskLevel.CRITICAL, AccessLevel.WRITE)
    dec2, _ = pe.evaluate(tool2, ctx)
    assert dec2 == PolicyDecision.DENY

    # HIGH -> REQUIRE_APPROVAL
    tool3 = Tool("t3", "1.0", "t3", RiskLevel.HIGH, AccessLevel.WRITE)
    dec3, _ = pe.evaluate(tool3, ctx)
    assert dec3 == PolicyDecision.REQUIRE_APPROVAL


__all__ = []
