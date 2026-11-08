"""Event Scheduler — agendamento de eventos na simulação.

Responsabilidade: agendar eventos para tempos futuros de simulação e
executá-los quando o Simulation Clock alcança o tempo marcado. Suporta
eventos únicos e recorrentes.

Princípios:
* Eventos são ordenados por timestamp de simulação.
* Eventos podem ser cancelados antes da execução.
* Cada evento tem correlação com eventos causadores (rastreabilidade).
"""

from __future__ import annotations

import heapq
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .simulation_types import EventType, SimulationEvent


@dataclass
class ScheduledEvent:
    """Evento agendado na fila de eventos do simulador."""

    timestamp: float
    priority: int
    event: SimulationEvent
    handler: Callable[[SimulationEvent], Any] | None = None
    cancelled: bool = False

    def __lt__(self, other: "ScheduledEvent") -> bool:
        if self.timestamp != other.timestamp:
            return self.timestamp < other.timestamp
        return self.priority < other.priority


class EventScheduler:
    """Fila de eventos discretos para simulação."""

    def __init__(self) -> None:
        self._heap: list[ScheduledEvent] = []
        self._event_index: dict[str, ScheduledEvent] = {}
        self._counter = 0  # para desempate estável no heap

    def schedule(
        self,
        *,
        world_id: str,
        timestamp: float,
        event_type: EventType,
        payload: Mapping[str, Any] | None = None,
        entity_id: str | None = None,
        caused_by: str | None = None,
        correlation_id: str | None = None,
        handler: Callable[[SimulationEvent], Any] | None = None,
        priority: int = 0,
    ) -> str:
        """Agenda um evento para execução no timestamp informado."""
        event_id = uuid.uuid4().hex
        event = SimulationEvent(
            event_id=event_id,
            world_id=world_id,
            event_type=event_type,
            entity_id=entity_id,
            timestamp=timestamp,
            payload=dict(payload or {}),
            caused_by=caused_by,
            correlation_id=correlation_id,
        )
        scheduled = ScheduledEvent(
            timestamp=timestamp,
            priority=priority,
            event=event,
            handler=handler,
        )
        heapq.heappush(self._heap, scheduled)
        self._event_index[event_id] = scheduled
        self._counter += 1
        return event_id

    def cancel(self, event_id: str) -> bool:
        """Cancela um evento agendado (não executado ainda)."""
        scheduled = self._event_index.get(event_id)
        if scheduled is None or scheduled.cancelled:
            return False
        scheduled.cancelled = True
        return True

    def pop_due(self, current_time: float) -> list[ScheduledEvent]:
        """Retorna todos os eventos com timestamp <= current_time, em ordem."""
        due: list[ScheduledEvent] = []
        while self._heap and self._heap[0].timestamp <= current_time:
            scheduled = heapq.heappop(self._heap)
            if not scheduled.cancelled:
                due.append(scheduled)
        return due

    def get_event(self, event_id: str) -> SimulationEvent | None:
        scheduled = self._event_index.get(event_id)
        if scheduled is None:
            return None
        return scheduled.event

    def list_pending(self) -> list[ScheduledEvent]:
        return [s for s in self._heap if not s.cancelled]

    def count_pending(self) -> int:
        return sum(1 for s in self._heap if not s.cancelled)

    def clear(self) -> None:
        self._heap.clear()
        self._event_index.clear()
        self._counter = 0


__all__ = [
    "EventScheduler",
    "ScheduledEvent",
]
