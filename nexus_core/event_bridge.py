"""Cliente UDS para a bridge de eventos Python ↔ Rust.

O protocolo é o mesmo do daemon (`core/nexus-daemon/src/ipc/protocol.rs`):
JSON por linha, com frames ``{"type": ..., "body": {...}}``. O evento em si
segue o contrato v1.0.0 de ``contracts/schemas/event.schema.json`` e nunca
transporta ``dedup_key`` (campo derivado).

O cliente é síncrono e destinado a testes de integração e a ferramentas
operacionais. O runtime Python continua usando o ``EventBus`` em processo; o
bridge é a fronteira explícita para o Control Plane.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

from .contracts import event_from_wire, event_to_wire, validate_contract
from .events import Event

_MAX_FRAME_BYTES = 1_048_576


class BridgeError(RuntimeError):
    """Erro de protocolo ou de entrega na bridge."""


class BridgeTimeout(BridgeError):
    """O daemon não respondeu dentro do prazo."""


class BridgeProtocolError(BridgeError):
    """Frame inválido ou conexão encerrada pelo daemon."""


class RustEventBridge:
    """Cliente newline-JSON sobre Unix Domain Socket.

    Cada instância mantém uma conexão. Para testar publicação e assinatura sem
    intercalar ``Ack`` e ``Delivered``, use conexões separadas.
    """

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 5.0,
        max_frame_bytes: int = _MAX_FRAME_BYTES,
        logger: logging.Logger | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout deve ser positivo")
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes deve ser positivo")
        self._socket_path = Path(socket_path)
        self._timeout = timeout
        self._max_frame_bytes = max_frame_bytes
        self._logger = logger or logging.getLogger("zeus.event_bridge")
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> RustEventBridge:
        with self._lock:
            if self._socket is not None:
                return self
            if not self._socket_path.exists():
                raise BridgeError(f"socket do daemon ausente: {self._socket_path}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._timeout)
            try:
                sock.connect(str(self._socket_path))
            except OSError as exc:
                sock.close()
                raise BridgeError(
                    f"falha ao conectar em {self._socket_path}: {exc}"
                ) from exc
            self._socket = sock
            self._buffer = b""
            self._pending = []
            return self

    def close(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
            self._buffer = b""
            self._pending = []
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise BridgeError("bridge não conectada; chame connect() primeiro")
        return self._socket

    def _send(self, frame: Mapping[str, Any]) -> None:
        sock = self._require_socket()
        payload = json.dumps(
            dict(frame), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        try:
            sock.sendall(payload)
        except OSError as exc:
            raise BridgeError(f"falha ao enviar frame: {exc}") from exc

    def _read_frame(self, timeout: float | None = None) -> dict[str, Any]:
        sock = self._require_socket()
        old_timeout = sock.gettimeout()
        if timeout is not None:
            if timeout <= 0:
                raise BridgeTimeout("timeout esgotado aguardando frame")
            sock.settimeout(timeout)
        try:
            while b"\n" not in self._buffer:
                chunk = sock.recv(65_536)
                if not chunk:
                    raise BridgeProtocolError("conexão encerrada pelo daemon")
                self._buffer += chunk
                if len(self._buffer) > self._max_frame_bytes:
                    raise BridgeProtocolError("frame excede o limite configurado")
            line, self._buffer = self._buffer.split(b"\n", 1)
            if len(line) > self._max_frame_bytes:
                raise BridgeProtocolError("frame excede o limite configurado")
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BridgeProtocolError(f"frame não é JSON válido: {exc}") from exc
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                raise BridgeProtocolError("frame sem campo 'type'")
            body = frame.get("body")
            if body is not None and not isinstance(body, dict):
                raise BridgeProtocolError("campo 'body' não é um objeto")
            return frame
        except TimeoutError as exc:
            raise BridgeTimeout(
                f"timeout de {timeout or old_timeout}s aguardando resposta do daemon"
            ) from exc
        except OSError as exc:
            raise BridgeError(f"falha ao ler frame: {exc}") from exc
        finally:
            if timeout is not None and self._socket is not None:
                self._socket.settimeout(old_timeout)

    def _read_until(
        self,
        expected: set[str],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # Frames recebidos fora de ordem (ex.: Delivered antes de Ack/Subscribed)
        # ficam pendentes e são devolvidos na chamada correspondente, em vez de
        # descartados.
        for index, frame in enumerate(self._pending):
            kind = frame["type"]
            if kind == "Error":
                del self._pending[index]
                body = frame.get("body") or {}
                raise BridgeError(str(body.get("error", "erro não especificado")))
            if kind in expected:
                del self._pending[index]
                return frame

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeout("timeout aguardando frame do daemon")
            frame = self._read_frame(remaining)
            kind = frame["type"]
            body = frame.get("body") or {}
            if kind == "Error":
                raise BridgeError(str(body.get("error", "erro não especificado")))
            if kind in expected:
                return frame
            self._pending.append(frame)

    def publish(self, event: Event, *, validate: bool = True) -> dict[str, Any]:
        """Publica um evento e aguarda o ``Ack`` do daemon."""
        wire = event_to_wire(event)
        if validate:
            validate_contract("event", wire)
        with self._lock:
            self._send({"type": "Event", "body": {"event": wire}})
            frame = self._read_until({"Ack"}, timeout=self._timeout)
            body = frame.get("body") or {}
            if not body.get("ok"):
                raise BridgeError(str(body.get("error") or "daemon rejeitou o evento"))
            if body.get("event_id") not in (None, event.event_id):
                raise BridgeProtocolError(
                    "Ack com event_id diferente do evento publicado"
                )
            return body

    def subscribe(
        self,
        patterns: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> list[str]:
        """Assina padrões e retorna os ids confirmados pelo daemon."""
        normalized = [str(pattern).strip() for pattern in patterns]
        if not normalized or any(not pattern for pattern in normalized):
            raise ValueError("patterns deve conter ao menos um padrão não vazio")
        with self._lock:
            self._send({"type": "Subscribe", "body": {"patterns": normalized}})
            ids: list[str] = []
            while len(ids) < len(normalized):
                frame = self._read_until({"Subscribed"}, timeout=timeout)
                subscription_id = (frame.get("body") or {}).get("id")
                if not isinstance(subscription_id, str) or not subscription_id:
                    raise BridgeProtocolError("Subscribed sem id válido")
                ids.append(subscription_id)
            return ids

    def unsubscribe(self, subscription_id: str) -> None:
        if not subscription_id:
            raise ValueError("subscription_id não pode ser vazio")
        with self._lock:
            self._send({"type": "Unsubscribe", "body": {"id": subscription_id}})

    def next_event(self, timeout: float | None = None) -> Event:
        """Aguarda o próximo frame ``Delivered`` e o converte em ``Event``."""
        with self._lock:
            frame = self._read_until({"Delivered"}, timeout=timeout)
            wire = (frame.get("body") or {}).get("event")
            if not isinstance(wire, dict):
                raise BridgeProtocolError("Delivered sem evento")
            return event_from_wire(wire)

    def health(self, timeout: float | None = None) -> dict[str, Any]:
        """Solicita o health check e retorna o corpo de status."""
        with self._lock:
            self._send({"type": "Health"})
            frame = self._read_until({"HealthResponse"}, timeout=timeout)
            status = (frame.get("body") or {}).get("status")
            if not isinstance(status, dict):
                raise BridgeProtocolError("HealthResponse sem status")
            return status


__all__ = [
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeout",
    "RustEventBridge",
]
