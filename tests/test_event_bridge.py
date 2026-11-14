"""Testes da bridge UDS Python ↔ Rust (protocolo newline-JSON).

A cobertura tem duas camadas:

* ``FakeDaemon`` — servidor Unix Domain Socket controlado, para exercitar
  handshake, ``VersionMismatch``, timeout, Ack divergente, frame inválido e
  comandos de controle sem depender do binário Rust;
* daemon Rust real — construído em ``core/nexus-daemon/target/debug``, iniciado
  em modo ``NEXUS_DAEMON_ONCE`` com hold estendido, validando publish,
  subscribe, dedup, health, autorização e o ciclo de aprovação pela mesma
  porta IPC usada em produção.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import pytest

from nexus_core.contracts import (
    ContractValidationError,
    authorization_request_contract,
    event_to_wire,
    load_fixture,
)
from nexus_core.event_bridge import (
    IPC_PROTOCOL_VERSION,
    BridgeError,
    BridgeProtocolError,
    BridgeTimeout,
    BridgeVersionMismatch,
    RustEventBridge,
)
from nexus_core.events import Event

ROOT = Path(__file__).resolve().parents[1]
DAEMON_BINARY = ROOT / "core" / "nexus-daemon" / "target" / "debug" / "nexus-daemon"


class FakeDaemon:
    """Servidor UDS mínimo que responde frames por linha via callback.

    ``handshake`` controla a resposta ao primeiro frame ``Hello``:
    ``"welcome"`` (padrão), ``"mismatch"`` ou ``"error"``. O callback só é
    chamado para frames posteriores ao handshake.
    """

    def __init__(self, path: Path, responder, *, handshake: str = "welcome") -> None:
        if handshake not in ("welcome", "mismatch", "error"):
            raise ValueError(f"handshake inválido: {handshake!r}")
        self.path = path
        self.responder = responder
        self.handshake = handshake
        self.received: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def frames(self) -> list[dict]:
        with self._lock:
            return list(self.received)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            thread = threading.Thread(
                target=self._serve_client, args=(conn,), daemon=True
            )
            thread.start()

    def _serve_client(self, conn: socket.socket) -> None:
        with conn:
            stream = conn.makefile("rwb")
            try:
                for raw in stream:
                    frame = json.loads(raw)
                    with self._lock:
                        self.received.append(frame)
                    if frame["type"] == "Hello":
                        self._write_handshake(stream, frame)
                        continue
                    for response in self.responder(frame):
                        self._write_frame(stream, response)
            except (OSError, ValueError):
                return
            finally:
                stream.close()

    def _write_handshake(self, stream, frame: dict) -> None:
        body = frame.get("body") or {}
        if self.handshake == "mismatch":
            response = {
                "type": "VersionMismatch",
                "body": {
                    "supported": "2.0.0",
                    "received": body.get("protocol_version", ""),
                },
            }
        elif self.handshake == "error":
            response = {
                "type": "Error",
                "body": {"error": "handshake recusado"},
            }
        else:
            response = {
                "type": "Welcome",
                "body": {
                    "protocol_version": IPC_PROTOCOL_VERSION,
                    "server_version": "fake-daemon",
                    "component_id": body.get("component_id", ""),
                    "peer_uid": os.getuid(),
                },
            }
        self._write_frame(stream, response)

    @staticmethod
    def _write_frame(stream, frame: dict) -> None:
        stream.write(
            json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        stream.flush()


def make_event(**overrides) -> Event:
    base = {
        "kind": "bridge.ping",
        "source": "test",
        "payload": {"text": "ação", "n": 1},
        "created_at": datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Event(**base)


def ack_for(frame: dict, *, event_id: str | None = None) -> dict:
    body = frame.get("body") or {}
    wire = body.get("event") or {}
    return {
        "type": "Ack",
        "body": {
            "ok": True,
            "event_id": event_id or wire.get("event_id"),
            "error": None,
        },
    }


# --------------------------------------------------------------------------- #
# Fake daemon: protocolo e caminhos de erro
# --------------------------------------------------------------------------- #

def test_publish_and_health_with_fake_daemon(tmp_path):
    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Health":
            return [
                {
                    "type": "HealthResponse",
                    "body": {
                        "protocol_version": IPC_PROTOCOL_VERSION,
                        "status": {
                            "status": "ok",
                            "version": "test",
                            "uptime_ms": 1,
                            "checks": [],
                            "timestamp": "2026-11-14T12:00:00Z",
                        },
                    },
                }
            ]
        if frame["type"] == "Event":
            return [ack_for(frame)]
        return []

    socket_path = tmp_path / "fake.sock"
    event = make_event()
    with FakeDaemon(socket_path, responder) as fake, RustEventBridge(
        socket_path, timeout=2
    ) as bridge:
        assert bridge.connected is True
        assert bridge.welcome is not None
        assert bridge.welcome["server_version"] == "fake-daemon"
        assert bridge.welcome["protocol_version"] == IPC_PROTOCOL_VERSION
        status = bridge.health()
        assert status["status"] == "ok"
        assert status["version"] == "test"

        ack = bridge.publish(event)
        assert ack["ok"] is True
        assert ack["event_id"] == event.event_id

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == ["Hello", "Health", "Event"]
    assert frames[0]["body"] == {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "component_id": "python-runtime",
    }
    assert frames[2]["body"]["event"] == event_to_wire(event)
    assert "dedup_key" not in frames[2]["body"]["event"]


def test_subscribe_and_receive_event_with_fake_daemon(tmp_path):
    event = make_event()

    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Subscribe":
            return [
                {"type": "Subscribed", "body": {"id": "sub-1"}},
                {"type": "Delivered", "body": {"event": event_to_wire(event)}},
            ]
        return []

    socket_path = tmp_path / "fake-sub.sock"
    with FakeDaemon(socket_path, responder), RustEventBridge(
        socket_path, timeout=2
    ) as bridge:
        assert bridge.subscribe(["bridge.*"]) == ["sub-1"]
        delivered = bridge.next_event(timeout=2)
        assert delivered.event_id == event.event_id
        assert delivered.kind == "bridge.ping"
        assert delivered.payload == {
            "text": "ação",
            "n": 1,
        }
        bridge.unsubscribe("sub-1")


def test_publish_buffers_delivered_frame_for_next_event(tmp_path):
    """Delivered antes de Ack não pode ser descartado pelo cliente."""
    event = make_event()

    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Event":
            return [
                {"type": "Delivered", "body": {"event": event_to_wire(event)}},
                ack_for(frame),
            ]
        return []

    socket_path = tmp_path / "fake-out-of-order.sock"
    with FakeDaemon(socket_path, responder), RustEventBridge(
        socket_path, timeout=2
    ) as bridge:
        ack = bridge.publish(event)
        assert ack["event_id"] == event.event_id
        delivered = bridge.next_event(timeout=0.5)
        assert delivered.event_id == event.event_id


def test_publish_rejects_divergent_ack_event_id(tmp_path):
    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Event":
            return [ack_for(frame, event_id="outro-evento")]
        return []

    socket_path = tmp_path / "fake-ack.sock"
    with FakeDaemon(socket_path, responder), RustEventBridge(
        socket_path, timeout=2
    ) as bridge, pytest.raises(BridgeProtocolError, match="event_id"):
        bridge.publish(make_event())


def test_publish_reports_daemon_error_frame(tmp_path):
    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Event":
            return [{"type": "Error", "body": {"error": "evento rejeitado"}}]
        return []

    socket_path = tmp_path / "fake-error.sock"
    with FakeDaemon(socket_path, responder), RustEventBridge(
        socket_path, timeout=2
    ) as bridge, pytest.raises(BridgeError, match="evento rejeitado"):
        bridge.publish(make_event())


def test_next_event_times_out_without_delivery(tmp_path):
    socket_path = tmp_path / "fake-timeout.sock"
    with FakeDaemon(socket_path, lambda frame: []), RustEventBridge(
        socket_path, timeout=0.2
    ) as bridge, pytest.raises(BridgeTimeout):
        bridge.next_event(timeout=0.2)


def test_connect_fails_without_socket(tmp_path):
    with pytest.raises(BridgeError, match="socket do daemon ausente"):
        RustEventBridge(tmp_path / "missing.sock").connect()


def test_bridge_requires_connection_before_publish(tmp_path):
    bridge = RustEventBridge(tmp_path / "unused.sock")
    with pytest.raises(BridgeError, match="não conectada"):
        bridge.publish(make_event())


def test_bridge_rejects_incompatible_protocol_version_before_connect(tmp_path):
    with pytest.raises(ValueError, match="protocol_version incompatível"):
        RustEventBridge(tmp_path / "unused.sock", protocol_version="2.0.0")


def test_connect_reports_version_mismatch_and_closes(tmp_path):
    socket_path = tmp_path / "fake-mismatch.sock"
    with FakeDaemon(socket_path, lambda frame: [], handshake="mismatch") as fake:
        bridge = RustEventBridge(socket_path, timeout=2)
        with pytest.raises(BridgeVersionMismatch, match="2.0.0"):
            bridge.connect()
        assert bridge.connected is False
        assert fake.frames()[0]["type"] == "Hello"


def test_connect_reports_handshake_error_and_closes(tmp_path):
    socket_path = tmp_path / "fake-handshake-error.sock"
    with FakeDaemon(socket_path, lambda frame: [], handshake="error"):
        bridge = RustEventBridge(socket_path, timeout=2)
        with pytest.raises(BridgeError, match="handshake recusado"):
            bridge.connect()
        assert bridge.connected is False


def test_fake_daemon_control_commands(tmp_path):
    authorization = load_fixture("valid/authorization_request.json")
    authorization_result = load_fixture("valid/authorization_result.json")
    approval_request = load_fixture("valid/approval_request.json")
    approval_record = load_fixture("valid/approval_record.json")
    approval_decision = load_fixture("valid/approval_decision.json")

    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "Authorize":
            return [
                {
                    "type": "AuthorizationResponse",
                    "body": {"result": authorization_result},
                }
            ]
        if frame["type"] in (
            "ApprovalRequest",
            "ApprovalDecision",
            "ApprovalStatus",
        ):
            return [
                {
                    "type": "ApprovalResponse",
                    "body": {"approval": approval_record},
                }
            ]
        return []

    socket_path = tmp_path / "fake-control.sock"
    with FakeDaemon(socket_path, responder) as fake, RustEventBridge(
        socket_path, timeout=2
    ) as bridge:
        result = bridge.authorize(authorization)
        assert result == authorization_result

        record = bridge.approval_request(approval_request)
        assert record["approval_id"] == approval_record["approval_id"]
        assert record["params_hash"] == approval_record["params_hash"]

        decided = bridge.approval_decide(approval_decision)
        assert decided["status"] == approval_record["status"]

        status = bridge.approval_status(approval_record["approval_id"])
        assert status["approval_id"] == approval_record["approval_id"]

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == [
        "Hello",
        "Authorize",
        "ApprovalRequest",
        "ApprovalDecision",
        "ApprovalStatus",
    ]
    assert frames[1]["body"]["request"] == authorization
    assert frames[2]["body"]["request"] == approval_request
    assert frames[3]["body"]["decision"] == approval_decision
    assert frames[4]["body"]["approval_id"] == approval_record["approval_id"]


def test_control_command_validates_contract_before_sending(tmp_path):
    invalid = dict(load_fixture("valid/authorization_request.json"))
    invalid["autonomy_level"] = 9
    socket_path = tmp_path / "fake-invalid-contract.sock"
    with FakeDaemon(socket_path, lambda frame: []) as fake, RustEventBridge(
        socket_path, timeout=2
    ) as bridge, pytest.raises(ContractValidationError):
        bridge.authorize(invalid)
    assert [frame["type"] for frame in fake.frames()] == ["Hello"]


def test_control_response_is_validated(tmp_path):
    authorization_result = dict(load_fixture("valid/authorization_result.json"))
    authorization_result["decision"] = "MAYBE"

    def responder(frame: dict) -> list[dict]:
        return [
            {
                "type": "AuthorizationResponse",
                "body": {"result": authorization_result},
            }
        ]

    socket_path = tmp_path / "fake-invalid-response.sock"
    with FakeDaemon(socket_path, responder), RustEventBridge(
        socket_path, timeout=2
    ) as bridge, pytest.raises(ContractValidationError):
        bridge.authorize(load_fixture("valid/authorization_request.json"))


# --------------------------------------------------------------------------- #
# Daemon Rust real
# --------------------------------------------------------------------------- #

def _wait_for_socket(socket_path: Path, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        code = process.poll()
        if code is not None:
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise AssertionError(
                f"daemon encerrou antes de abrir o socket (exit {code}): {stderr[-2000:]}"
            )
        time.sleep(0.02)
    raise AssertionError(f"socket não apareceu em {timeout}s: {socket_path}")


def _daemon_binary_is_stale() -> bool:
    if not DAEMON_BINARY.is_file():
        return True
    built_at = DAEMON_BINARY.stat().st_mtime
    sources = list((ROOT / "core" / "nexus-daemon" / "src").rglob("*.rs"))
    sources.append(ROOT / "core" / "nexus-daemon" / "Cargo.toml")
    return any(path.stat().st_mtime > built_at for path in sources)


@pytest.fixture
def rust_daemon(tmp_path):
    if _daemon_binary_is_stale():
        if shutil.which("cargo") is None:
            pytest.skip(
                "binário Rust ausente/desatualizado e cargo indisponível; "
                "rode `cargo build` em core/nexus-daemon"
            )
        subprocess.run(
            ["cargo", "build"],
            cwd=str(ROOT / "core" / "nexus-daemon"),
            check=True,
            timeout=600,
        )
    run_dir = tmp_path / "data" / "run"
    run_dir.mkdir(parents=True)
    socket_path = run_dir / "nexus-daemon.sock"
    config_path = tmp_path / "nexus.toml"
    config_path.write_text(
        f"""
[nexus]
name = "NEXUS"
environment = "test"

[paths]
data_dir = "{(tmp_path / 'data').as_posix()}"

[logging]
level = "WARNING"
json = true

[ipc]
socket_path = "{socket_path.as_posix()}"
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        NEXUS_CONFIG=str(config_path),
        NEXUS_DAEMON_ONCE="1",
        NEXUS_DAEMON_HOLD_MS="15000",
    )
    process = subprocess.Popen(
        [str(DAEMON_BINARY)],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(socket_path, process, 10)
        yield socket_path, tmp_path / "data" / "databases" / "nexus_daemon_events.db"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


def test_real_daemon_publish_subscribe_dedup_and_health(rust_daemon):
    socket_path, database_path = rust_daemon
    event = make_event(event_id="fedcba9876543210fedcba9876543210")

    subscriber = RustEventBridge(socket_path, timeout=5).connect()
    publisher = RustEventBridge(socket_path, timeout=5).connect()
    try:
        status = publisher.health(timeout=5)
        assert status["status"] == "ok"
        assert status["version"]

        subscription_ids = subscriber.subscribe(["bridge.*"], timeout=5)
        assert len(subscription_ids) == 1
        assert subscription_ids[0]

        ack = publisher.publish(event)
        assert ack["ok"] is True
        assert ack["event_id"] == event.event_id

        delivered = subscriber.next_event(timeout=5)
        assert delivered.event_id == event.event_id
        assert delivered.kind == event.kind
        assert delivered.source == event.source
        assert delivered.payload == event.payload
        assert delivered.correlation_id == event.correlation_id
        assert delivered.dedup is True

        # Duplicata exata: o store deduplica e o barramento não republica.
        duplicate_ack = publisher.publish(event)
        assert duplicate_ack["ok"] is True
        with pytest.raises(BridgeTimeout):
            subscriber.next_event(timeout=0.5)

        subscriber.unsubscribe(subscription_ids[0])
    finally:
        subscriber.close()
        publisher.close()

    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ?", (event.kind,)
        ).fetchone()[0]
        started = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'system.started'"
        ).fetchone()[0]
    assert stored == 1
    assert started == 1


def test_real_daemon_authorize_approval_flow(rust_daemon):
    socket_path, _database_path = rust_daemon

    def request(risk_level: str, *, params=None, approval_id=None):
        return authorization_request_contract(
            component_id="python-runtime",
            tool_id="sys.dangerous",
            params=params or {"args": ["--safe"]},
            task_id="task-t4",
            plan_id="plan-t4",
            risk_level=risk_level,
            autonomy_level=1,
            required_autonomy=1,
            approval_id=approval_id,
            execution_mode="HOST",
            access_level="READ",
            fs_scope="DEFAULT",
            net_scope="NONE",
            resource_limits="default",
            justification="integration test",
        )

    with RustEventBridge(socket_path, timeout=5) as bridge:
        low = bridge.authorize(request("LOW"))
        assert low["decision"] == "ALLOWED"
        assert low["approved"] is True
        assert low["execution_id"] == low["request_id"]
        assert low["metadata"] == {}

        pending = bridge.authorize(request("HIGH"))
        assert pending["decision"] == "REQUIRES_APPROVAL"
        assert pending["approved"] is False
        assert pending["execution_id"] is None
        approval_id = pending["metadata"]["approval_id"]

        status = bridge.approval_status(approval_id)
        assert status["status"] == "PENDING"
        assert status["risk_level"] == "HIGH"

        decided = bridge.approval_decide(
            {
                "approval_id": approval_id,
                "decision": "APPROVED",
                "approver": "operator",
                "timestamp": time.time(),
            }
        )
        assert decided["status"] == "APPROVED"
        assert decided["decided_by"] == "operator"

        allowed = bridge.authorize(request("HIGH", approval_id=approval_id))
        assert allowed["decision"] == "ALLOWED"
        assert allowed["approved"] is True
        assert allowed["metadata"]["approval_status"] == "CLAIMED"

        # Uso único: o mesmo approval_id não autoriza outra execução.
        replay = bridge.authorize(request("HIGH", approval_id=approval_id))
        assert replay["decision"] == "DENIED"
        assert "approval verification failed" in replay["reason"]

        # Aprovação vinculada aos params exatos: hash divergente não consome.
        bound = bridge.authorize(
            request("HIGH", params={"args": ["--safe"]})
        )
        bound_id = bound["metadata"]["approval_id"]
        bridge.approval_decide(
            {
                "approval_id": bound_id,
                "decision": "APPROVED",
                "approver": "operator",
                "timestamp": time.time(),
            }
        )
        divergent = bridge.authorize(
            request(
                "HIGH",
                params={"args": ["--danger"]},
                approval_id=bound_id,
            )
        )
        assert divergent["decision"] == "DENIED"
        assert "params divergem" in divergent["reason"]
        assert bridge.approval_status(bound_id)["status"] == "APPROVED"

        exact = bridge.authorize(
            request("HIGH", params={"args": ["--safe"]}, approval_id=bound_id)
        )
        assert exact["decision"] == "ALLOWED"
