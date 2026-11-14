"""E2E do Execution Gate pelo runtime real (bootstrap), sem mocks de gate.

Estes testes cobrem o caminho que faltava no T0: ``bootstrap`` -> ``AutomationEngine``
-> ``ExecutionGate``. O runner de subprocesso é substituído para não depender de
comandos do host, mas o gate, a política, a aprovação e a auditoria são reais.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from nexus_core.app import bootstrap
from nexus_core.approval import ApprovalDecision
from nexus_core.automation import ApprovalRequired, AutomationDenied
from nexus_core.automation_types import AccessLevel, ExecutionMode, RiskLevel, Tool


def _write_config(root: Path, *, autonomy_level: int = 1) -> Path:
    config_dir = root / "config"
    config_dir.mkdir()
    config_path = config_dir / "nexus.toml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            [paths]
            data_dir = "data"

            [safety]
            autonomy_level = {autonomy_level}

            [events]
            dedup_window_hours = 0
            max_payload_bytes = 16384
            retention_days = 7
            query_limit = 500

            [llm]
            providers = []
            chain = []

            [logging]
            level = "INFO"
            """
        ),
        encoding="utf-8",
    )
    (root / "data").mkdir()
    return config_path


def _add_tool(runtime, tool_id: str, risk: RiskLevel) -> None:
    runtime.automation._tools[tool_id] = Tool(
        id=tool_id,
        version="1.0",
        description=f"E2E {risk.name} tool",
        risk=risk,
        access=AccessLevel.WRITE,
        execution_mode=ExecutionMode.HOST,
        argv_template=("/usr/bin/uptime",),
    )


def _kinds(runtime) -> list[str]:
    return [event.kind for event in runtime.store.query(limit=500)]


@pytest.mark.asyncio
async def test_bootstrap_executes_low_risk_through_gate_without_phantom_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime = bootstrap(_write_config(Path(tmpdir)), console_logs=False)
        try:
            calls = []

            async def runner(argv, timeout):
                calls.append((argv, timeout))
                return 0, b"gate e2e\n", b""

            runtime.automation._runner = runner

            captured = {}
            original_authorize = runtime.execution_gate.authorize

            async def spy_authorize(**kwargs):
                captured.update(kwargs)
                return await original_authorize(**kwargs)

            runtime.execution_gate.authorize = spy_authorize

            result = await runtime.automation.run("sys.uptime", task_id="e2e-low")

            assert result.exit_code == 0
            assert result.stdout == "gate e2e\n"
            assert calls == [(("/usr/bin/uptime",), 10.0)]

            # O gate do runtime é o mesmo injetado no AutomationEngine.
            assert runtime.execution_gate is runtime.automation._execution_gate
            assert runtime.automation._autonomy_level == 1
            assert runtime.config.safety.autonomy_level == 1
            assert captured["autonomy_level"] == 1
            assert captured["execution_mode"] == ExecutionMode.HOST.value
            assert captured["access_level"] == AccessLevel.READ.value

            executions = runtime.execution_gate.list_executions()
            assert len(executions) == 1
            assert executions[0]["status"] == "completed"
            # Resultado real, não a "execução fantasma" com runner=None.
            assert executions[0]["result"]["stdout"] == "gate e2e\n"
            assert executions[0]["result"]["exit_code"] == 0

            kinds = _kinds(runtime)
            assert "security.execution.authorized" in kinds
            assert "security.execution.completed" in kinds
            assert "automation.action.completed" in kinds
            assert "automation.gate.commit_failed" not in kinds
        finally:
            runtime.close()


@pytest.mark.asyncio
async def test_bootstrap_medium_risk_requires_approval_then_commits_gate():
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime = bootstrap(_write_config(Path(tmpdir)), console_logs=False)
        try:
            calls = []

            async def runner(argv, timeout):
                calls.append(argv)
                return 0, b"approved e2e\n", b""

            runtime.automation._runner = runner
            _add_tool(runtime, "e2e.medium", RiskLevel.MEDIUM)

            with pytest.raises(ApprovalRequired) as excinfo:
                await runtime.automation.run("e2e.medium", task_id="e2e-medium")

            approval_id = excinfo.value.request_id
            assert calls == []
            assert runtime.execution_gate.list_executions() == []

            runtime.approvals.decide(
                approval_id,
                ApprovalDecision(decision="APPROVED", approver="operator"),
            )
            result = await runtime.automation.run(
                "e2e.medium",
                task_id="e2e-medium",
                approval_id=approval_id,
            )

            assert result.exit_code == 0
            assert result.stdout == "approved e2e\n"
            assert calls == [("/usr/bin/uptime",)]
            assert runtime.approvals.get_request(approval_id)["status"] == "CONSUMED"

            executions = runtime.execution_gate.list_executions()
            assert len(executions) == 1
            assert executions[0]["status"] == "completed"
            assert executions[0]["result"]["stdout"] == "approved e2e\n"

            kinds = _kinds(runtime)
            assert "security.execution.approval_verified" in kinds
            assert "security.execution.authorized" in kinds
            assert "security.execution.completed" in kinds
            assert "automation.gate.commit_failed" not in kinds
        finally:
            runtime.close()


@pytest.mark.asyncio
async def test_bootstrap_denies_critical_tool_before_runner_and_gate():
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime = bootstrap(_write_config(Path(tmpdir)), console_logs=False)
        try:
            async def runner(argv, timeout):
                raise AssertionError("critical tool must never run")

            runtime.automation._runner = runner
            _add_tool(runtime, "e2e.critical", RiskLevel.CRITICAL)

            with pytest.raises(AutomationDenied):
                await runtime.automation.run("e2e.critical", task_id="e2e-critical")

            assert runtime.execution_gate.list_executions() == []
            kinds = _kinds(runtime)
            assert "automation.denied" in kinds
            assert "automation.action.started" not in kinds
        finally:
            runtime.close()
