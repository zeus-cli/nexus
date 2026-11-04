"""Realtime Hub: ponte entre o EventBus interno e clientes WebSocket.

Cada cliente recebe uma fila limitada. Se o cliente não consumir, os eventos
mais antigos são descartados e o descarte é informado explicitamente — o HUD
nunca deve acreditar que a stream está completa quando não está.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..eventbus import EventBus, Subscription, pattern_matches
from ..events import Event

MAX_CLIENTS = 32
QUEUE_SIZE = 256


@dataclass
class HubClient:
    id: str
    patterns: tuple[str, ...]
    queue: "asyncio.Queue[dict[str, Any]]"
    connected_at: datetime
    delivered: int = 0
    dropped: int = 0

    def matches(self, kind: str) -> bool:
        if not self.patterns:
            return True
        return any(pattern_matches(pattern, kind) for pattern in self.patterns)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patterns": list(self.patterns),
            "connected_at": self.connected_at.isoformat(),
            "delivered": self.delivered,
            "dropped": self.dropped,
            "pending": self.queue.qsize(),
        }


@dataclass(slots=True)
class HubStats:
    clients: int = 0
    accepted: int = 0
    rejected: int = 0
    dispatched: int = 0
    dropped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "clients": self.clients,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "dispatched": self.dispatched,
            "dropped": self.dropped,
        }


class RealtimeHub:
    def __init__(
        self,
        bus: EventBus,
        *,
        queue_size: int = QUEUE_SIZE,
        max_clients: int = MAX_CLIENTS,
        logger: logging.Logger | None = None,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size deve ser positivo")
        if max_clients <= 0:
            raise ValueError("max_clients deve ser positivo")
        self._bus = bus
        self._queue_size = queue_size
        self._max_clients = max_clients
        self._logger = logger or logging.getLogger("zeus.api.realtime")
        self._clients: dict[str, HubClient] = {}
        self._subscription: Subscription | None = None
        self._stats = HubStats()

    # -- ciclo de vida -------------------------------------------------------
    def start(self) -> None:
        if self._subscription is None:
            self._subscription = self._bus.subscribe("*", self._dispatch)

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        for client in list(self._clients.values()):
            self.unregister(client)

    # -- clientes ------------------------------------------------------------
    def register(self, patterns: Iterable[str] = ()) -> HubClient | None:
        if len(self._clients) >= self._max_clients:
            self._stats.rejected += 1
            self._logger.warning(
                "limite de clientes de tempo real atingido",
                extra={"component": "realtime", "max_clients": self._max_clients},
            )
            return None
        client = HubClient(
            id=uuid.uuid4().hex,
            patterns=tuple(patterns),
            queue=asyncio.Queue(maxsize=self._queue_size),
            connected_at=datetime.now(timezone.utc),
        )
        self._clients[client.id] = client
        self._stats.accepted += 1
        self._stats.clients = len(self._clients)
        return client

    def unregister(self, client: HubClient) -> None:
        self._clients.pop(client.id, None)
        self._stats.clients = len(self._clients)

    def snapshot(self) -> list[dict[str, Any]]:
        return [client.snapshot() for client in self._clients.values()]

    @property
    def stats(self) -> dict[str, int]:
        return self._stats.as_dict()

    # -- despacho ------------------------------------------------------------
    def _dispatch(self, event: Event) -> None:
        """Handler síncrono do bus: nunca bloqueia, nunca levanta."""
        if not self._clients:
            return
        frame: dict[str, Any] | None = None
        for client in self._clients.values():
            if not client.matches(event.kind):
                continue
            if frame is None:
                frame = {"type": "event", "event": event.to_dict()}
            try:
                client.queue.put_nowait(frame)
                client.delivered += 1
                self._stats.dispatched += 1
            except asyncio.QueueFull:
                self._drop_oldest(client, frame)

    def _drop_oldest(self, client: HubClient, frame: dict[str, Any]) -> None:
        try:
            client.queue.get_nowait()
            client.dropped += 1
            self._stats.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - corrida improvável
            pass
        try:
            client.queue.put_nowait(frame)
            client.delivered += 1
            self._stats.dispatched += 1
        except asyncio.QueueFull:  # pragma: no cover
            client.dropped += 1
            self._stats.dropped += 1
