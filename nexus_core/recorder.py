"""Ponte única entre produção de eventos, persistência e despacho.

Regra arquitetural: nada emite evento diretamente no bus sem antes passar pelo
EventStore, para que toda decisão relevante permaneça auditável.
"""

from __future__ import annotations

import logging

from .events import Event
from .eventbus import EventBus
from .memory.eventstore import EventStore


class EventRecorder:
    def __init__(self, store: EventStore, bus: EventBus | None = None,
                 logger: logging.Logger | None = None) -> None:
        self._store = store
        self._bus = bus
        self._logger = logger or logging.getLogger("zeus.recorder")

    @property
    def store(self) -> EventStore:
        return self._store

    async def record(self, event: Event) -> Event:
        """Persiste (idempotente por dedup) e então publica no bus.

        Em caso de duplicidade dentro da janela, o evento já persistido não é
        republicado — consumidores nunca veem dois fatos iguais.
        """
        stored, inserted = self._store.append(event)
        if not inserted:
            self._logger.debug(
                "evento duplicado descartado",
                extra={
                    "component": "recorder",
                    "event_id": stored.event_id,
                    "kind": stored.kind,
                    "dedup_key": stored.dedup_key(),
                },
            )
            return stored
        if self._bus is not None:
            await self._bus.publish(stored)
        return stored
