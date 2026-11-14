"""Testes da bridge UDS Python ↔ Rust (protocolo newline-JSON).

A cobertura tem duas camadas:

* ``FakeDaemon`` — servidor Unix Domain Socket controlado, para exercitar
  caminhos de erro (timeout, Ack divergente, frame inválido) sem depender do
  binário Rust;
* daemon Rust real — construído em ``core/nexus-daemon/target/debug``, iniciado
  em modo ``NEXUS_DAEMON_ONCE`` com hold estendido, validando publish,
  subscribe, dedup e health pela mesma porta IPC usada em produção.
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

from nexus_core.contracts import event_to_wire
from nexus_core.event_bridge import (
    BridgeError,
    BridgeProtocolError,
    BridgeTimeout,
    RustEventBridge,
)
from nexus_core.events import Event

ROOT = Path(__file__).resolve().parents[1]
DAEMON_BINARY = ROOT / "core" / "nexus-daemon" / "target" / "debug" / "nexus-daemon"


class FakeDaemon:
    """Servidor UDS mínimo que responde frames por linha via callback."""

    def __init__(self, path: Path, responder) -> None:
        self.path = path
        self.responder = responder
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
                    for response in self.responder(frame):
                        stream.write(
                            json.dumps(response, separators=(",", ":")).encode("utf-8")
                            + b"\n"
                        )
                        stream.flush()
            except (OSError, ValueError):
                return
            finally:
                stream.close()


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
                        "status": {
                            "status": "ok",
                            "version": "test",
                            "uptime_ms": 1,
                            "checks": [],
                            "timestamp": "2026-11-14T12:00:00Z",
                        }
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
        status = bridge.health()
        assert status["status"] == "ok"
        assert status["version"] == "test"

        ack = bridge.publish(event)
        assert ack["ok"] is True
        assert ack["event_id"] == event.event_id

    frames = fake.frames()
    assert [frame["type"] for frame in frames] == ["Health", "Event"]
    assert frames[1]["body"]["event"] == event_to_wire(event)
    assert "dedup_key" not in frames[1]["body"]["event"]


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
