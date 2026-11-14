"""Ponte única entre produção de eventos, persistência e despacho.

Regra arquitetural: nada emite evento diretamente no bus sem antes passar pelo
EventStore, para que toda decisão relevante permaneça auditável.

Com um :class:`~nexus_core.event_fabric.EventFabric` injetado, o recorder usa o
outbox transacional (persistência + entrega + retry). Sem fabric, mantém o
caminho legado ``append → publish`` para bibliotecas e testes que constroem o
recorder isoladamente.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .eventbus import EventBus
from .events import Event
from .memory.eventstore import EventStore

if TYPE_CHECKING:
    from .event_fabric import EventFabric


class EventRecorder:
    def __init__(
        self,
        store: EventStore,
        bus: EventBus | None = None,
        logger: logging.Logger | None = None,
        *,
        fabric: EventFabric | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._fabric = fabric
        self._logger = logger or logging.getLogger("zeus.recorder")

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def fabric(self) -> EventFabric | None:
        return self._fabric

    async def record(self, event: Event) -> Event:
        """Persiste (idempotente por dedup) e então publica no bus.

        Em caso de duplicidade dentro da janela, o evento já persistido não é
        republicado — consumidores nunca veem dois fatos iguais.

        Com outbox, a publicação é uma tentativa imediata; falhas ficam
        registradas e são retryadas pelo pump do fabric.
        """
        if self._fabric is not None:
            return await self._fabric.emit(event)

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
