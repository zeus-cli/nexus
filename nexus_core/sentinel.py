"""Private Unix-stream event receiver for the independent Rust sentinel."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import Event, Severity
from .privatefs import ensure_private_directory
from .recorder import EventRecorder

MAX_FRAME_BYTES = 128 * 1024


class SentinelReceiver:
    def __init__(self, socket_path: str | Path, recorder: EventRecorder) -> None:
        self.path = Path(socket_path)
        self._recorder = recorder
        self._server: asyncio.AbstractServer | None = None
        self._last_seen_monotonic: float | None = None
        self._last_seen_at: datetime | None = None
        self._last_payload: dict[str, Any] | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        ensure_private_directory(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            metadata = self.path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode):
                raise PermissionError(f"sentinel IPC path is not a socket: {self.path}")
            try:
                _reader, writer = await asyncio.open_unix_connection(self.path)
            except OSError:
                self.path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError(f"sentinel receiver already active: {self.path}")
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.path), limit=MAX_FRAME_BYTES
        )
        os.chmod(self.path, 0o600, follow_symlinks=False)

    async def close(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._server = None
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(metadata.st_mode):
            self.path.unlink()

    def health(self) -> dict[str, Any]:
        age = (
            max(0.0, time.monotonic() - self._last_seen_monotonic)
            if self._last_seen_monotonic is not None
            else None
        )
        return {
            "connected": age is not None and age <= 30,
            "last_seen": self._last_seen_at.isoformat() if self._last_seen_at else None,
            "age_s": round(age, 3) if age is not None else None,
            "sample": dict(self._last_payload) if self._last_payload else None,
        }

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # ``StreamReader.readline`` sinaliza limite excedido com
                    # ValueError; responda antes de fechar para o cliente
                    # distinguir rejeição de queda de conexão.
                    await self._reply(writer, {"ok": False, "error": "frame too large"})
                    return
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self._reply(writer, {"ok": False, "error": "frame too large"})
                    return
                try:
                    frame = json.loads(line)
                    event = self._event_from_frame(frame)
                    await self._recorder.record(event)
                    self._last_seen_monotonic = time.monotonic()
                    self._last_seen_at = datetime.now(timezone.utc)
                    self._last_payload = dict(event.payload)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    await self._reply(writer, {"ok": False, "error": str(exc)[:160]})
                    continue
                except Exception as exc:
                    await self._reply(writer, {"ok": False, "error": type(exc).__name__})
                    continue
                await self._reply(writer, {"ok": True, "event_id": event.event_id})
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    def _event_from_frame(frame: Any) -> Event:
        if not isinstance(frame, dict):
            raise ValueError("frame deve ser objeto JSON")
        kind = frame.get("kind")
        payload = frame.get("payload", {})
        if frame.get("source") != "sentinel_rs":
            raise ValueError("source deve ser sentinel_rs")
        if not isinstance(frame.get("kind"), str):
            raise ValueError("kind deve ser string")
        if not isinstance(payload, dict):
            raise ValueError("payload deve ser objeto JSON")
        dedup = frame.get("dedup", False)
        if not isinstance(dedup, bool):
            raise ValueError("dedup deve ser booleano")
        try:
            severity = Severity(frame.get("severity", "info"))
        except ValueError as exc:
            raise ValueError("severity inválida") from exc
        return Event(
            kind=kind,
            source="sentinel_rs",
            severity=severity,
            payload=payload,
            dedup=dedup,
        )

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()
