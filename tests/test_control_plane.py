"""Testes do Control Plane Rust e da autoridade de execução (T6).

Cobrem quatro camadas:

* configuração ``[control_plane]`` opt-in e desabilitada por padrão;
* adaptador ``RustApprovalManager`` sobre o IPC (incluindo fail-closed);
* ``ExecutionGate`` autoritativo delegando política, aprovação e ciclo
  ``CLAIMED → CONSUMED`` ao daemon, sem criar aprovações locais;
* bootstrap degradado quando o daemon está indisponível: o runtime sobe, o
  health fica ``degraded`` e nenhuma execução é autorizada.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus_core.approval import ApprovalDecision, ApprovalManager, ApprovalRequest
from nexus_core.automation import (
    AccessLevel,
    ApprovalRequired,
    AutomationEngine,
    RiskLevel,
    Tool,
)
from nexus_core.config import ConfigError, load_config
from nexus_core.contracts import validate_contract
from nexus_core.control_plane import (
    ControlPlaneClient,
    ControlPlaneError,
    ControlPlaneUnavailable,
    RustApprovalManager,
    approval_request_to_wire,
)
from nexus_core.execution_gate import (
    ExecutionGate,
    ExecutionGateError,
    GateDecision,
)
from nexus_core.policy import PolicyEngine
from nexus_core.recorder import EventRecorder

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write_project_config(root: Path, body: str) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "nexus.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def approval_record(
    approval_id: str = "ap-1",
    status: str = "PENDING",
    **overrides: object,
) -> dict[str, object]:
    """Registro completo no contrato ``approval_record`` v1."""
    record: dict[str, object] = {
        "approval_id": approval_id,
        "tool_id": "sys.dangerous",
        "params_hash": "a" * 64,
        "task_id": "task-1",
        "plan_id": "plan-1",
        "execution_mode": "HOST",
        "access_level": "WRITE",
        "fs_scope": "DEFAULT",
        "net_scope": "NONE",
        "resource_limits": "default",
        "policy_version": "1.0",
        "justification": "test",
        "status": status,
        "created_at": 100.0,
        "expires_at": 200.0,
        "decided_at": None,
        "decided_by": None,
        "consumed_at": None,
        "risk_level": "HIGH",
    }
    record.update(overrides)
    validate_contract("approval_record", record)
    return record


def normalized_record(
    approval_id: str = "ap-1",
    status: str = "PENDING",
    **overrides: object,
) -> dict[str, object]:
    """Registro como o ``ControlPlaneClient`` entrega ao adaptador (``id``)."""
    record = approval_record(approval_id, status, **overrides)
    record["id"] = record.pop("approval_id")
    return record


def approval_request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_id="sys.dangerous",
        params={"args": ()},
        task_id="task-1",
        plan_id="plan-1",
        execution_mode="HOST",
        access_level="WRITE",
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        policy_version="1.0",
        justification="manual review",
    )


class StubControlPlane:
    """Daemon control plane em memória com o mesmo contrato observável.

    O gate só consome ``authorize``, ``approval_claim``, ``execution_start`` e
    ``execution_complete`` no caminho autoritativo. O stub mantém o estado
    mínimo para provar a ordem do ciclo e que ``CONSUMED`` é produzido antes
    do runner.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.requires_approval = True
        self.approved = False
        self.fail_authorize: Exception | None = None
        self.fail_start: Exception | None = None
        self.approval_id = "ap-1"
        self.gate_request_id = "req-1"
        self.execution_id = "exec-1"
        self.approval_status = "PENDING"
        self.execution_status = "PREPARED"

    def authorize(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(("authorize", dict(request)))
        validate_contract("authorization_request", dict(request))
        if self.fail_authorize is not None:
            raise self.fail_authorize

        if self.requires_approval and not request.get("approval_id"):
            self.approval_status = "PENDING"
            return {
                "decision": "REQUIRES_APPROVAL",
                "request_id": self.gate_request_id,
                "reason": "high risk requires approval",
                "approved": False,
                "execution_id": None,
                "metadata": {
                    "approval_id": self.approval_id,
                    "approval_status": "PENDING",
                    "requires_human": True,
                },
            }
        if not self.approved:
            return {
                "decision": "DENIED",
                "request_id": self.gate_request_id,
                "reason": "approval not granted",
                "approved": False,
                "execution_id": None,
                "metadata": {"approval_id": self.approval_id},
            }
        self.approval_status = "CLAIMED"
        return {
            "decision": "ALLOWED",
            "request_id": self.gate_request_id,
            "reason": "approval granted",
            "approved": True,
            "execution_id": self.execution_id,
            "metadata": {
                "approval_id": self.approval_id,
                "approval_status": "CLAIMED",
            },
        }

    def approval_claim(
        self,
        approval_id: str,
        request: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append(("approval_claim", approval_id))
        if not self.approved:
            raise ControlPlaneError("approval not granted")
        self.approval_status = "CLAIMED"
        return approval_record(
            approval_id,
            "CLAIMED",
            decided_at=110.0,
            decided_by="operator",
        )

    def execution_start(self, approval_id: str) -> dict[str, object]:
        self.calls.append(("execution_start", approval_id))
        if self.fail_start is not None:
            raise self.fail_start
        if self.approval_status not in ("CLAIMED", "APPROVED"):
            raise ControlPlaneError("approval must be CLAIMED before execution")
        self.approval_status = "CONSUMED"
        self.execution_status = "STARTING"
        return {
            "approval": approval_record(approval_id, "CONSUMED", consumed_at=120.0),
            "execution": {
                "approval_id": approval_id,
                "status": "STARTING",
            },
        }

    def execution_complete(
        self,
        approval_id: str,
        status: str,
        *,
        summary: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("execution_complete", (approval_id, status)))
        if self.execution_status != "STARTING":
            raise ControlPlaneError("execution is not STARTING")
        self.execution_status = status
        return {
            "approval": approval_record(
                approval_id, self.approval_status, consumed_at=120.0
            ),
            "execution": {
                "approval_id": approval_id,
                "status": status,
                "result_summary": summary,
            },
        }


def gate_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "component_id": "automation_engine",
        "tool_id": "sys.dangerous",
        "params": {"args": ()},
        "task_id": "task-1",
        "plan_id": "plan-1",
        "risk_level": RiskLevel.HIGH,
        "execution_mode": "HOST",
        "access_level": "WRITE",
        "fs_scope": "DEFAULT",
        "net_scope": "NONE",
        "resource_limits": "default",
        "justification": "manual review",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Configuração opt-in
# --------------------------------------------------------------------------- #

class TestControlPlaneConfig:
    def test_repository_config_keeps_control_plane_disabled(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "nexus.toml")

        assert config.control_plane.enabled is False
        assert config.control_plane.authoritative is False

    def test_section_is_disabled_by_default(self, tmp_path: Path) -> None:
        path = write_project_config(
            tmp_path,
            """
            [paths]
            data_dir = "data"

            [logging]
            level = "INFO"
            json = true
            """,
        )

        config = load_config(path)

        assert config.control_plane.enabled is False
        assert config.control_plane.timeout_s == 5.0
        assert config.control_plane.socket_path is not None
        assert config.control_plane.socket_path == (
            tmp_path / "data" / "run" / "nexus-daemon.sock"
        )

    def test_opt_in_parses_socket_and_timeout(self, tmp_path: Path) -> None:
        path = write_project_config(
            tmp_path,
            """
            [paths]
            data_dir = "var"

            [logging]
            level = "INFO"
            json = true

            [control_plane]
            enabled = true
            socket_path = "var/run/custom.sock"
            timeout_s = 2.5
            """,
        )

        config = load_config(path)

        assert config.control_plane.enabled is True
        assert config.control_plane.authoritative is True
        assert config.control_plane.socket_path == tmp_path / "var" / "run" / "custom.sock"
        assert config.control_plane.timeout_s == 2.5
        assert config.as_dict()["control_plane"]["enabled"] is True

    def test_invalid_timeout_is_rejected(self, tmp_path: Path) -> None:
        path = write_project_config(
            tmp_path,
            """
            [paths]
            data_dir = "data"

            [control_plane]
            enabled = true
            timeout_s = 0
            """,
        )

        with pytest.raises(ConfigError):
            load_config(path)


# --------------------------------------------------------------------------- #
# Adaptador RustApprovalManager
# --------------------------------------------------------------------------- #

class TestRustApprovalManagerAdapter:
    def test_request_approval_sends_wire_contract_and_ttl(self) -> None:
        client = MagicMock()
        client.approval_request.return_value = normalized_record("ap-1", "PENDING")
        manager = RustApprovalManager(client)
        request = approval_request()

        approval_id = manager.request_approval(request, ttl_s=42.0)

        assert approval_id == "ap-1"
        client.approval_request.assert_called_once_with(
            approval_request_to_wire(request), ttl_s=42.0
        )

    def test_decide_maps_decision_fields(self) -> None:
        client = MagicMock()
        client.approval_decide.return_value = approval_record("ap-1", "APPROVED")
        manager = RustApprovalManager(client)

        manager.decide(
            "ap-1",
            ApprovalDecision(
                decision="APPROVED",
                approver="operator",
                timestamp=123.0,
            ),
        )

        client.approval_decide.assert_called_once_with(
            {
                "approval_id": "ap-1",
                "decision": "APPROVED",
                "approver": "operator",
                "timestamp": 123.0,
            }
        )

    def test_list_pending_filters_pending_and_get_history_paginates(self) -> None:
        client = MagicMock()
        client.approval_list.return_value = [normalized_record("ap-1", "PENDING")]
        manager = RustApprovalManager(client)

        pending = manager.list_pending()
        history = manager.get_history(limit=5, status="CONSUMED", offset=2)

        assert pending[0]["id"] == "ap-1"
        assert history[0]["id"] == "ap-1"
        assert client.approval_list.call_args_list[0].kwargs == {
            "status": "PENDING",
            "limit": 1000,
        }
        assert client.approval_list.call_args_list[1].kwargs == {
            "status": "CONSUMED",
            "limit": 5,
            "offset": 2,
        }

    def test_get_request_normalizes_id_and_handles_not_found(self) -> None:
        client = MagicMock()
        client.approval_status.return_value = normalized_record("ap-1", "APPROVED")
        manager = RustApprovalManager(client)

        assert manager.get_request("ap-1")["id"] == "ap-1"

        client.approval_status.side_effect = ControlPlaneError(
            "approval_status: aprovação não encontrada"
        )
        assert manager.get_request("missing") is None

    def test_verify_and_claim_fail_closed(self) -> None:
        client = MagicMock()
        client.approval_verify.side_effect = ControlPlaneUnavailable("daemon down")
        client.approval_claim.side_effect = ControlPlaneUnavailable("daemon down")
        manager = RustApprovalManager(client)

        assert manager.verify_action("ap-1", approval_request()) is False
        assert manager.claim_approval("ap-1") is False

    def test_execution_status_is_terminal_only(self) -> None:
        client = MagicMock()
        client.execution_complete.return_value = {
            "approval": approval_record("ap-1", "CONSUMED"),
            "execution": {"status": "COMPLETED"},
        }
        manager = RustApprovalManager(client)

        manager.mark_starting("ap-1")
        client.execution_start.assert_called_once_with("ap-1")

        from nexus_core.approval import ExecutionStatus

        manager.update_execution_status(
            "ap-1", ExecutionStatus.COMPLETED, summary="ok"
        )
        client.execution_complete.assert_called_once_with(
            "ap-1", "COMPLETED", summary="ok"
        )

        with pytest.raises(ValueError):
            manager.update_execution_status("ap-1", ExecutionStatus.RUNNING)

    def test_approval_api_returns_false_when_control_plane_fails(self) -> None:
        from nexus_core.approval_api import APIUser, ApprovalAPI

        client = MagicMock()
        client.approval_decide.side_effect = ControlPlaneUnavailable("daemon down")
        client.approval_revoke.side_effect = ControlPlaneUnavailable("daemon down")
        api = ApprovalAPI(RustApprovalManager(client))
        user = APIUser("cli", "operador local", "OPERATOR")

        assert api.approve("ap-1", user) is False
        assert api.deny("ap-1", user) is False
        assert api.revoke("ap-1", user) is False


# --------------------------------------------------------------------------- #
# Execution Gate autoritativo
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_authoritative_gate_skips_local_policy_and_consumes_approval() -> None:
    control_plane = StubControlPlane()
    policy = MagicMock(spec=PolicyEngine)
    policy.evaluate.side_effect = AssertionError("política local não deve ser usada")
    gate = ExecutionGate(
        control_plane=control_plane,
        authoritative=True,
        policy_engine=policy,
    )

    pending = await gate.authorize(**gate_kwargs())

    assert gate.authoritative is True
    assert pending.decision == GateDecision.REQUIRES_APPROVAL
    assert pending.metadata["approval_id"] == "ap-1"
    policy.evaluate.assert_not_called()
    assert [name for name, _ in control_plane.calls] == ["authorize"]

    control_plane.approved = True
    runner_calls: list[dict[str, object]] = []

    async def runner(params: dict[str, object]) -> dict[str, object]:
        assert control_plane.approval_status == "CONSUMED"
        runner_calls.append(dict(params))
        return {"ran": True}

    outcome = await gate.execute(pending.request_id, runner=runner)

    assert outcome == {
        "ok": True,
        "request_id": pending.request_id,
        "result": {"ran": True},
    }
    assert runner_calls == [{"args": ()}]
    assert [name for name, _ in control_plane.calls] == [
        "authorize",
        "approval_claim",
        "execution_start",
        "execution_complete",
    ]
    assert control_plane.approval_status == "CONSUMED"
    assert control_plane.execution_status == "COMPLETED"


@pytest.mark.asyncio
async def test_authoritative_execution_refuses_when_start_fails() -> None:
    control_plane = StubControlPlane()
    control_plane.approved = True
    control_plane.fail_start = ControlPlaneUnavailable("daemon down")
    gate = ExecutionGate(control_plane=control_plane, authoritative=True)
    authorized = await gate.authorize(**gate_kwargs(approval_id="ap-1"))

    assert authorized.decision == GateDecision.ALLOWED
    assert authorized.metadata["approval_status"] == "CLAIMED"

    runner_called = False

    def runner(params: dict[str, object]) -> dict[str, object]:
        nonlocal runner_called
        runner_called = True
        return {}

    with pytest.raises(ExecutionGateError, match="control plane unavailable"):
        await gate.execute(authorized.request_id, runner=runner)

    assert runner_called is False
    assert control_plane.execution_status == "PREPARED"
    assert [name for name, _ in control_plane.calls] == [
        "authorize",
        "execution_start",
    ]


@pytest.mark.asyncio
async def test_authoritative_authorize_is_fail_closed() -> None:
    control_plane = StubControlPlane()
    control_plane.fail_authorize = ControlPlaneUnavailable("daemon down")
    gate = ExecutionGate(control_plane=control_plane, authoritative=True)

    denied = await gate.authorize(**gate_kwargs())

    assert denied.decision == GateDecision.DENIED
    assert denied.reason.startswith("control plane unavailable")
    assert gate.list_executions() == []


def test_authoritative_mode_requires_control_plane() -> None:
    with pytest.raises(ValueError):
        ExecutionGate(authoritative=True)


# --------------------------------------------------------------------------- #
# Daemon indisponível
# --------------------------------------------------------------------------- #

def test_missing_daemon_makes_adapter_fail_closed(tmp_path: Path) -> None:
    client = ControlPlaneClient(tmp_path / "missing.sock", timeout=0.2)
    manager = RustApprovalManager(client)

    with pytest.raises(ControlPlaneUnavailable):
        client.health()
    assert manager.verify_action("ap-1", approval_request()) is False
    assert manager.claim_approval("ap-1") is False
    with pytest.raises(ControlPlaneUnavailable):
        manager.list_pending()
    with pytest.raises(ControlPlaneUnavailable):
        manager.get_history()


@pytest.mark.asyncio
async def test_missing_daemon_denies_authoritative_gate(tmp_path: Path) -> None:
    client = ControlPlaneClient(tmp_path / "missing.sock", timeout=0.2)
    gate = ExecutionGate(control_plane=client, authoritative=True)

    denied = await gate.authorize(**gate_kwargs())

    assert denied.decision == GateDecision.DENIED
    assert "control plane unavailable" in denied.reason
    assert gate.list_executions() == []


def test_bootstrap_degrades_when_daemon_is_unavailable(tmp_path: Path) -> None:
    from nexus_core.app import bootstrap, health

    path = write_project_config(
        tmp_path,
        """
        [paths]
        data_dir = "data"

        [logging]
        level = "ERROR"
        json = true

        [events]
        dedup_window_hours = 0

        [control_plane]
        enabled = true
        socket_path = "data/run/missing.sock"
        timeout_s = 0.2
        """,
    )

    runtime = bootstrap(path, console_logs=False)
    try:
        assert isinstance(runtime.approvals, RustApprovalManager)
        assert runtime.control_plane is not None
        assert runtime.execution_gate is not None
        assert runtime.execution_gate.authoritative is True
        assert not (tmp_path / "data" / "databases" / "nexus_approvals.db").exists()

        report = health(runtime)

        assert report["status"] == "degraded"
        assert report["control_plane"]["status"] == "unavailable"
        assert report["control_plane"]["authoritative"] is True
        assert any(
            check["name"] == "control_plane" and check["ok"] is False
            for check in report["checks"]
        )
    finally:
        runtime.close()


# --------------------------------------------------------------------------- #
# Automação autoritativa
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_authoritative_automation_does_not_create_local_approval(
    tmp_path: Path,
) -> None:
    control_plane = StubControlPlane()
    gate = ExecutionGate(control_plane=control_plane, authoritative=True)
    local_approvals = ApprovalManager(db_path=str(tmp_path / "local.db"))
    runner_calls: list[tuple[tuple[str, ...], float]] = []

    async def runner(argv: tuple[str, ...], timeout: float):
        runner_calls.append((argv, timeout))
        return 0, b"up 1 day\n", b""

    recorder = MagicMock(spec=EventRecorder)

    async def record(event):  # noqa: ANN001 - mock assíncrono simples
        return None

    recorder.record.side_effect = record
    engine = AutomationEngine(
        autonomy_level=1,
        recorder=recorder,
        runner=runner,
        approval_manager=local_approvals,
        policy_engine=PolicyEngine(),
        execution_gate=gate,
    )
    engine._tools["sys.dangerous"] = Tool(
        "sys.dangerous",
        "1.0",
        "Dangerous tool",
        RiskLevel.HIGH,
        AccessLevel.WRITE,
        argv_template=("/usr/bin/uptime",),
    )

    with pytest.raises(ApprovalRequired) as excinfo:
        await engine.run("sys.dangerous", task_id="task-hitl")

    requested_id = excinfo.value.request_id
    assert requested_id == "ap-1"
    assert local_approvals.get_request(requested_id) is None
    assert local_approvals.list_pending() == []
    assert runner_calls == []

    # A decisão humana acontece no daemon; o Python apenas retoma com o id.
    control_plane.approved = True
    result = await engine.run(
        "sys.dangerous",
        task_id="task-hitl",
        approval_id=requested_id,
    )

    assert result.exit_code == 0
    assert result.stdout == "up 1 day\n"
    assert runner_calls == [(("/usr/bin/uptime",), 10.0)]
    assert local_approvals.get_request(requested_id) is None
    assert control_plane.approval_status == "CONSUMED"
    assert control_plane.execution_status == "COMPLETED"
    assert [name for name, _ in control_plane.calls] == [
        "authorize",
        "authorize",
        "execution_start",
        "execution_complete",
    ]


def test_sqlite_approval_schema_matches_rust_store(tmp_path: Path) -> None:
    """Contrato de schema compartilhado: o Rust grava, o Python lê read-only."""
    db_path = tmp_path / "approvals.db"
    manager = ApprovalManager(db_path=str(db_path))
    request_id = manager.request_approval(approval_request())

    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)")
        }
        execution_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(executions)")
        }
        row = connection.execute(
            "SELECT id, status FROM approvals WHERE id = ?", (request_id,)
        ).fetchone()

    assert {"id", "params_hash", "status", "consumed_at"} <= columns
    assert {"approval_id", "status", "started_at", "completed_at"} <= execution_columns
    assert row == (request_id, "PENDING")
