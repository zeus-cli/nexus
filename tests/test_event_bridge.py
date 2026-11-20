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

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import pytest

from nexus_core.contracts import (
    ContractValidationError,
    authorization_request_contract,
    event_to_wire,
    load_fixture,
)
from nexus_core.approval import ApprovalDecision, ApprovalRequest
from nexus_core.automation_types import RiskLevel
from nexus_core.control_plane import ControlPlaneClient, RustApprovalManager
from nexus_core.event_bridge import (
    IPC_PROTOCOL_VERSION,
    BridgeError,
    BridgeProtocolError,
    BridgeTimeout,
    BridgeVersionMismatch,
    RustEventBridge,
)
from nexus_core.events import Event
from nexus_core.execution_gate import ExecutionGate, GateDecision

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
    revoked_record = dict(approval_record)
    revoked_record["status"] = "REVOKED"

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
            "ApprovalRevoke",
        ):
            record = (
                revoked_record
                if frame["type"] == "ApprovalRevoke"
                else approval_record
            )
            return [
                {
                    "type": "ApprovalResponse",
                    "body": {"approval": record},
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

        revoked = bridge.approval_revoke(approval_record["approval_id"])
        assert revoked["status"] == "REVOKED"

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == [
        "Hello",
        "Authorize",
        "ApprovalRequest",
        "ApprovalDecision",
        "ApprovalStatus",
        "ApprovalRevoke",
    ]
    assert frames[1]["body"]["request"] == authorization
    assert frames[2]["body"]["request"] == approval_request
    assert frames[3]["body"]["decision"] == approval_decision
    assert frames[4]["body"]["approval_id"] == approval_record["approval_id"]
    assert frames[5]["body"]["approval_id"] == approval_record["approval_id"]


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


def test_fake_daemon_identity_commands(tmp_path):
    owner = {
        "id": "prn_owner",
        "kind": "user",
        "display_name": "Owner",
        "role_id": "owner",
        "status": "active",
        "max_autonomy": 4,
    }
    summary = {
        "principal_ids": ["prn_guest"],
        "principals": 1,
        "credentials": 0,
        "devices": 0,
        "approvals": 0,
        "pairing_sessions": 0,
    }

    def responder(frame: dict) -> list[dict]:
        kind = frame["type"]
        if kind == "IdentityList":
            return [
                {
                    "type": "IdentityPrincipalsResponse",
                    "body": {"principals": [owner]},
                }
            ]
        if kind == "IdentityGet":
            return [
                {
                    "type": "IdentityPrincipalResponse",
                    "body": {"principal": owner},
                }
            ]
        if kind == "IdentityCreatePrincipal":
            guest = dict(owner, id="prn_guest", display_name="Guest", role_id="guest")
            return [
                {
                    "type": "IdentityPrincipalResponse",
                    "body": {"principal": guest},
                }
            ]
        if kind in ("IdentityRevokePrincipal", "IdentityDeviceRevoke"):
            return [
                {"type": "IdentityRevocationResponse", "body": {"summary": summary}}
            ]
        if kind == "IdentityEffectiveScopes":
            return [
                {
                    "type": "IdentityScopesResponse",
                    "body": {"principal_id": "prn_owner", "scopes": ["tools.read"]},
                }
            ]
        if kind == "IdentityPermissionCheck":
            allowed = frame["body"]["scope"] == "tools.read"
            return [
                {
                    "type": "IdentityPermissionResponse",
                    "body": {
                        "principal_id": "prn_guest",
                        "scope": frame["body"]["scope"],
                        "allowed": allowed,
                        "reason": None if allowed else "escopo ausente",
                    },
                }
            ]
        if kind == "IdentityDeviceList":
            return [
                {
                    "type": "IdentityDevicesResponse",
                    "body": {"devices": [{"id": "dev_1", "principal_id": "prn_guest"}]},
                }
            ]
        if kind == "IdentityPairStart":
            return [
                {
                    "type": "IdentityPairingResponse",
                    "body": {
                        "session": {
                            "id": "pair_1",
                            "role_id": frame["body"]["role_id"],
                            "created_by": frame["body"]["actor_id"],
                            "status": "pending",
                        }
                    },
                }
            ]
        return []

    socket_path = tmp_path / "fake-identity.sock"
    with FakeDaemon(socket_path, responder) as fake, RustEventBridge(
        socket_path, timeout=2
    ) as bridge:
        principals = bridge.identity_list("prn_owner")
        assert principals[0]["id"] == "prn_owner"
        assert bridge.identity_get("prn_owner", "prn_owner")["role_id"] == "owner"
        guest = bridge.identity_create_principal(
            "prn_owner",
            kind="user",
            display_name="Guest",
            role_id="guest",
            max_autonomy=0,
        )
        assert guest["id"] == "prn_guest"
        assert bridge.identity_effective_scopes("prn_owner") == ["tools.read"]
        assert bridge.identity_permission_check("prn_guest", "tools.read") == {
            "allowed": True,
            "reason": None,
        }
        denied = bridge.identity_permission_check("prn_guest", "tools.write")
        assert denied["allowed"] is False
        assert bridge.identity_device_list("prn_owner")[0]["id"] == "dev_1"
        assert bridge.identity_device_revoke(
            "prn_owner", "dev_1", "perdido"
        )["principals"] == 1
        session = bridge.identity_pair_start(
            "prn_owner",
            "guest",
            "a" * 64,
            code_hash="b" * 64,
            constraints={"max_autonomy": 0},
            ttl_s=60,
        )
        assert session["status"] == "pending"
        assert bridge.identity_revoke_principal(
            "prn_owner", "prn_guest", "teste"
        )["principals"] == 1

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == [
        "Hello",
        "IdentityList",
        "IdentityGet",
        "IdentityCreatePrincipal",
        "IdentityEffectiveScopes",
        "IdentityPermissionCheck",
        "IdentityPermissionCheck",
        "IdentityDeviceList",
        "IdentityDeviceRevoke",
        "IdentityPairStart",
        "IdentityRevokePrincipal",
    ]
    create = frames[3]["body"]
    assert create["actor_id"] == "prn_owner"
    assert create["kind"] == "user"
    assert create["role_id"] == "guest"
    assert "parent_id" not in create
    pair = frames[9]["body"]
    assert pair["secret_hash"] == "a" * 64
    assert pair["code_hash"] == "b" * 64
    assert pair["constraints"] == {"max_autonomy": 0}
    assert pair["ttl_s"] == 60.0


def test_control_plane_pair_start_never_sends_raw_secret(tmp_path):
    def responder(frame: dict) -> list[dict]:
        if frame["type"] == "IdentityPairStart":
            return [
                {
                    "type": "IdentityPairingResponse",
                    "body": {
                        "session": {
                            "id": "pair_1",
                            "role_id": frame["body"]["role_id"],
                            "status": "pending",
                        }
                    },
                }
            ]
        return []

    socket_path = tmp_path / "fake-pair.sock"
    with FakeDaemon(socket_path, responder) as fake, ControlPlaneClient(
        socket_path, timeout=2
    ) as client:
        result = client.identity_pair_start("prn_owner", "family")
        assert result["secret"]
        assert len(result["code"]) == 8
        assert result["session"]["id"] == "pair_1"

    pair = fake.frames()[-1]
    assert pair["type"] == "IdentityPairStart"
    body = pair["body"]
    assert body["secret_hash"] == hashlib.sha256(
        result["secret"].encode("utf-8")
    ).hexdigest()
    assert body["code_hash"] == hashlib.sha256(
        result["code"].encode("utf-8")
    ).hexdigest()
    assert result["secret"] not in json.dumps(body)
    assert result["code"] not in json.dumps(body)


def test_fake_daemon_owner_bootstrap_never_sends_raw_token(tmp_path):
    principal = {
        "id": "prn_owner",
        "kind": "user",
        "display_name": "Owner",
        "role_id": "owner",
        "status": "active",
        "max_autonomy": 4,
    }

    def responder(frame: dict) -> list[dict]:
        kind = frame["type"]
        if kind == "IdentityInitOwner":
            return [
                {
                    "type": "IdentityOwnerResponse",
                    "body": {
                        "initialized": True,
                        "principal": principal,
                        "credential_id": "cred_local",
                        "created_principal": True,
                        "created_credential": True,
                    },
                }
            ]
        if kind == "IdentityOwnerStatus":
            return [
                {
                    "type": "IdentityOwnerResponse",
                    "body": {
                        "initialized": True,
                        "principal": principal,
                        "credential_id": "cred_local",
                        "created_principal": False,
                        "created_credential": False,
                    },
                }
            ]
        return []

    socket_path = tmp_path / "fake-owner.sock"
    token = "nexus_token_local_secreto"
    with FakeDaemon(socket_path, responder) as fake, ControlPlaneClient(
        socket_path, timeout=2
    ) as client:
        created = client.identity_init_owner(token, display_name="Owner")
        assert created["created_principal"] is True
        assert created["principal"]["role_id"] == "owner"
        status = client.identity_owner_status(token)
        assert status["initialized"] is True
        assert status["created_credential"] is False

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == [
        "Hello",
        "IdentityInitOwner",
        "IdentityOwnerStatus",
    ]
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert frames[1]["body"]["token_hash"] == expected
    assert frames[1]["body"]["display_name"] == "Owner"
    assert frames[2]["body"]["token_hash"] == expected
    wire = json.dumps([frame for frame in frames if frame["type"] != "Hello"])
    assert token not in wire
    assert "secreto" not in wire


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


@dataclass(frozen=True)
class RustDaemonHandle:
    """Handle do daemon real; iterável para manter o unpacking dos testes."""

    socket_path: Path
    events_db: Path
    approvals_db: Path
    config_path: Path
    process: subprocess.Popen

    def __iter__(self):
        yield self.socket_path
        yield self.events_db


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
        yield RustDaemonHandle(
            socket_path=socket_path,
            events_db=tmp_path / "data" / "databases" / "nexus_daemon_events.db",
            approvals_db=tmp_path / "data" / "databases" / "nexus_approvals.db",
            config_path=config_path,
            process=process,
        )
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

        # Revogação: uma aprovação revogada não pode ser reivindicada.
        revoked_pending = bridge.authorize(request("HIGH"))
        revoked_id = revoked_pending["metadata"]["approval_id"]
        revoked = bridge.approval_revoke(revoked_id)
        assert revoked["status"] == "REVOKED"
        assert revoked["decided_at"] is None

        blocked = bridge.authorize(request("HIGH", approval_id=revoked_id))
        assert blocked["decision"] == "DENIED"
        assert "approval verification failed" in blocked["reason"]

        with pytest.raises(BridgeError):
            bridge.approval_revoke(revoked_id)


# --------------------------------------------------------------------------- #
# T6 — autoridade final no daemon e ciclo de execução
# --------------------------------------------------------------------------- #

async def _authorize_high_risk(gate: ExecutionGate):
    return await gate.authorize(
        component_id="automation_engine",
        tool_id="sys.dangerous",
        params={"target": "integration"},
        task_id="task-t6",
        plan_id="plan-t6",
        risk_level=RiskLevel.HIGH,
        autonomy_level=1,
        required_autonomy=1,
        execution_mode="HOST",
        access_level="WRITE",
        policy_version="1.0",
        fs_scope="DEFAULT",
        net_scope="NONE",
        resource_limits="default",
        justification="T6 integration test",
    )


def test_real_daemon_authoritative_execution_cycle_persists_history(rust_daemon):
    client = ControlPlaneClient(rust_daemon.socket_path, timeout=5)
    manager = RustApprovalManager(client)
    gate = ExecutionGate(control_plane=client, authoritative=True)

    try:
        async def exercise():
            pending = await _authorize_high_risk(gate)
            assert pending.decision == GateDecision.REQUIRES_APPROVAL
            approval_id = pending.metadata["approval_id"]
            assert manager.get_request(approval_id)["status"] == "PENDING"

            manager.decide(
                approval_id,
                ApprovalDecision(decision="APPROVED", approver="operator"),
            )
            assert manager.get_request(approval_id)["status"] == "APPROVED"

            runner_calls = []

            def runner(params):
                runner_calls.append(dict(params))
                return {"ran": True}

            outcome = await gate.execute(pending.request_id, runner=runner)
            assert outcome["ok"] is True
            assert outcome["result"] == {"ran": True}
            assert runner_calls == [{"target": "integration"}]
            return approval_id, outcome

        approval_id, outcome = asyncio.run(exercise())

        record = manager.get_request(approval_id)
        assert record["status"] == "CONSUMED"
        assert record["consumed_at"] is not None
        assert record["decided_by"] == "operator"

        history = manager.get_history(status="CONSUMED")
        assert [item["id"] for item in history] == [approval_id]

        execution = manager.get_execution(approval_id)
        assert execution["status"] == "COMPLETED"
        assert execution["result_summary"]
    finally:
        client.close()

    with sqlite3.connect(
        f"file:{rust_daemon.approvals_db}?mode=ro", uri=True
    ) as connection:
        approval_row = connection.execute(
            "SELECT status, consumed_at FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        execution_row = connection.execute(
            "SELECT status, completed_at FROM executions WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()

    assert approval_row is not None
    assert approval_row[0] == "CONSUMED"
    assert approval_row[1] is not None
    assert execution_row is not None
    assert execution_row[0] == "COMPLETED"
    assert execution_row[1] is not None


def test_real_daemon_owner_bootstrap_and_audit(rust_daemon):
    token = "nexus_owner_token_de_teste"
    client = ControlPlaneClient(rust_daemon.socket_path, timeout=5)
    try:
        assert client.identity_owner_status(token)["initialized"] is False
        first = client.identity_init_owner(token, display_name="Owner Local")
        assert first["initialized"] is True
        assert first["created_principal"] is True
        assert first["created_credential"] is True
        assert first["principal"]["role_id"] == "owner"

        second = client.identity_init_owner(token, display_name="Ignorado")
        assert second["created_principal"] is False
        assert second["created_credential"] is False
        assert second["principal"]["id"] == first["principal"]["id"]
        assert second["credential_id"] == first["credential_id"]

        status = client.identity_owner_status(token)
        assert status["initialized"] is True
        assert status["principal"]["id"] == first["principal"]["id"]

        principals = client.identity_list(first["principal"]["id"])
        assert [item["id"] for item in principals] == [first["principal"]["id"]]
    finally:
        client.close()

    with sqlite3.connect(
        f"file:{rust_daemon.events_db}?mode=ro", uri=True
    ) as connection:
        rows = connection.execute(
            "SELECT kind, payload FROM events "
            "WHERE kind LIKE 'identity.%' ORDER BY id"
        ).fetchall()
    assert [kind for kind, _ in rows] == [
        "identity.principal.created",
        "identity.credential.issued",
    ]
    payloads = [json.loads(payload) for _, payload in rows]
    assert payloads[0]["actor_id"] == "local_bootstrap"
    assert payloads[0]["role"] == "owner"
    assert payloads[1]["kind"] == "local_owner"
    assert token not in json.dumps(payloads)
    assert hashlib.sha256(token.encode("utf-8")).hexdigest() not in json.dumps(
        payloads
    )


def test_real_daemon_identity_cli_init_status_list_revoke(rust_daemon):
    data_dir = rust_daemon.approvals_db.parent.parent
    python_config = rust_daemon.config_path.with_name("nexus-cli.toml")
    python_config.write_text(
        f"""
[nexus]
name = "NEXUS"
environment = "test"

[paths]
data_dir = "{data_dir.as_posix()}"

[logging]
level = "WARNING"
json = true

[control_plane]
socket_path = "{rust_daemon.socket_path.as_posix()}"
timeout_s = 5.0
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["NEXUS_CONFIG"] = str(python_config)
    env.pop("NEXUS_API_TOKEN", None)

    def run_cli(*arguments: str) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "nexus_core.cli", *arguments],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-3000:]
        return json.loads(result.stdout)

    first = run_cli("identity", "init", "--name", "Owner CLI", "--json")
    assert first["created_principal"] is True
    assert first["principal"]["display_name"] == "Owner CLI"
    assert first["token_source"] == "file"

    second = run_cli("identity", "init", "--name", "Outro", "--json")
    assert second["created_principal"] is False
    assert second["credential_id"] == first["credential_id"]
    assert second["principal"]["id"] == first["principal"]["id"]

    status = run_cli("identity", "status", "--json")
    assert status["initialized"] is True
    assert status["principal"]["id"] == first["principal"]["id"]

    listed = run_cli("identity", "list", "--json")
    assert [item["id"] for item in listed] == [first["principal"]["id"]]

    client = ControlPlaneClient(rust_daemon.socket_path, timeout=5)
    try:
        guest = client.identity_create_principal(
            first["principal"]["id"],
            kind="user",
            display_name="Guest CLI",
            role_id="guest",
            max_autonomy=0,
        )
    finally:
        client.close()

    revoked = run_cli(
        "identity",
        "revoke",
        guest["id"],
        "--reason",
        "teste da CLI",
        "--json",
    )
    assert revoked["principals"] == 1
    with sqlite3.connect(
        f"file:{rust_daemon.approvals_db}?mode=ro", uri=True
    ) as connection:
        status_row = connection.execute(
            "SELECT status FROM principals WHERE id = ?", (guest["id"],)
        ).fetchone()
    assert status_row == ("revoked",)


def test_real_daemon_identity_ipc_roundtrip(rust_daemon):
    owner_id = "prn_test_owner"
    with sqlite3.connect(rust_daemon.approvals_db, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        now = time.time()
        connection.execute(
            "INSERT INTO principals (\n"
            "  id, kind, display_name, role_id, parent_id, status, max_autonomy,\n"
            "  created_at, updated_at, revoked_at, metadata\n"
            ") VALUES (?, 'user', 'Owner', 'owner', NULL, 'active', 4, ?, ?, NULL, '{}')",
            (owner_id, now, now),
        )
        connection.commit()

    client = ControlPlaneClient(rust_daemon.socket_path, timeout=5)
    try:
        principals = client.identity_list(owner_id)
        assert [principal["id"] for principal in principals] == [owner_id]
        assert client.identity_effective_scopes(owner_id)[0:1] == ["agents.manage"]

        guest = client.identity_create_principal(
            owner_id,
            kind="user",
            display_name="Guest",
            role_id="guest",
            max_autonomy=0,
        )
        assert guest["role_id"] == "guest"
        assert client.identity_permission_check(guest["id"], "tools.read") == {
            "allowed": True,
            "reason": None,
        }
        assert client.identity_permission_check(guest["id"], "tools.write")[
            "allowed"
        ] is False

        pairing = client.identity_pair_start(
            owner_id, "guest", constraints={"max_autonomy": 0}, ttl_s=60
        )
        assert pairing["session"]["status"] == "pending"
        assert client.identity_get(owner_id, guest["id"])["status"] == "active"

        summary = client.identity_revoke_principal(
            owner_id, guest["id"], "teste de integração"
        )
        assert summary["principals"] == 1
        assert client.identity_get(owner_id, guest["id"])["status"] == "revoked"
        assert client.identity_permission_check(guest["id"], "tools.read")[
            "allowed"
        ] is False
    finally:
        client.close()


def test_real_daemon_dead_denies_execution_and_approvals(rust_daemon):
    client = ControlPlaneClient(rust_daemon.socket_path, timeout=0.5)
    manager = RustApprovalManager(client)
    gate = ExecutionGate(control_plane=client, authoritative=True)

    try:
        assert client.health()["status"] == "ok"

        process = rust_daemon.process
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

        denied = asyncio.run(_authorize_high_risk(gate))

        assert denied.decision == GateDecision.DENIED
        assert "control plane unavailable" in denied.reason
        assert manager.verify_action("ap-dead", ApprovalRequest(
            tool_id="sys.dangerous",
            params={"target": "integration"},
            task_id="task-t6",
            plan_id="plan-t6",
            execution_mode="HOST",
            access_level="WRITE",
            fs_scope="DEFAULT",
            net_scope="NONE",
            resource_limits="default",
            policy_version="1.0",
            justification="T6 integration test",
        )) is False
        assert manager.claim_approval("ap-dead") is False
    finally:
        client.close()
