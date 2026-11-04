"""EventBus interno do NEXUS.

Comunicação desacoplada entre percepção, memória e agentes. Garantias:

* Ordem de entrega preservada por assinante (despacho sequencial).
* Falha de um assinante não interrompe os demais (resiliência).
* Timeout configurável por handler.
* Padrões de assinatura: ``kind`` exato, ``prefixo.*`` ou ``*`` global.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .events import Event

Handler = Callable[[Event], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    pattern: str
    handler: Handler


@dataclass(slots=True)
class BusStats:
    published: int = 0
    delivered: int = 0
    failures: int = 0
    timeouts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "published": self.published,
            "delivered": self.delivered,
            "failures": self.failures,
            "timeouts": self.timeouts,
        }


def pattern_matches(pattern: str, kind: str) -> bool:
    if pattern in ("*", "**"):
        return True
    if pattern.endswith(".**"):
        prefix = pattern[:-3]
        return kind == prefix or kind.startswith(f"{prefix}.")
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return kind == prefix or kind.startswith(f"{prefix}.")
    return pattern == kind


class EventBus:
    def __init__(self, *, handler_timeout: float | None = 5.0,
                 logger: logging.Logger | None = None) -> None:
        if handler_timeout is not None and handler_timeout <= 0:
            raise ValueError("handler_timeout deve ser positivo ou None")
        self._handler_timeout = handler_timeout
        self._subscriptions: list[Subscription] = []
        self._stats = BusStats()
        self._logger = logger or logging.getLogger("zeus.eventbus")

    # -- assinaturas ---------------------------------------------------------
    def subscribe(self, pattern: str, handler: Handler) -> Subscription:
        if not callable(handler):
            raise TypeError("handler deve ser chamável")
        normalized = (pattern or "").strip()
        if not normalized:
            raise ValueError("pattern de assinatura vazio")
        subscription = Subscription(id=uuid.uuid4().hex, pattern=normalized, handler=handler)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> bool:
        try:
            self._subscriptions.remove(subscription)
            return True
        except ValueError:
            return False

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions)

    @property
    def stats(self) -> BusStats:
        return self._stats

    # -- despacho ------------------------------------------------------------
    async def publish(self, event: Event) -> int:
        """Despacha ``event`` para todos os assinantes compatíveis.

        Nunca levanta exceção por falha de handler; retorna quantas entregas
        ocorreram com sucesso.
        """
        self._stats.published += 1
        delivered = 0
        for subscription in self._matching(event.kind):
            if await self._deliver(subscription, event):
                delivered += 1
        self._stats.delivered += delivered
        return delivered

    async def publish_many(self, events: Iterable[Event]) -> int:
        total = 0
        for event in events:
            total += await self.publish(event)
        return total

    def _matching(self, kind: str) -> tuple[Subscription, ...]:
        return tuple(
            subscription
            for subscription in self._subscriptions
            if pattern_matches(subscription.pattern, kind)
        )

    async def _deliver(self, subscription: Subscription, event: Event) -> bool:
        try:
            result = subscription.handler(event)
            if inspect.isawaitable(result):
                if self._handler_timeout is None:
                    await result
                else:
                    await asyncio.wait_for(result, timeout=self._handler_timeout)
            return True
        except asyncio.TimeoutError:
            self._stats.failures += 1
            self._stats.timeouts += 1
            self._logger.warning(
                "handler excedeu o timeout; evento descartado para este assinante",
                extra={
                    "component": "eventbus",
                    "subscription_id": subscription.id,
                    "pattern": subscription.pattern,
                    "event_id": event.event_id,
                    "kind": event.kind,
                },
            )
            return False
        except Exception:
            self._stats.failures += 1
            self._logger.exception(
                "falha ao processar evento no assinante",
                extra={
                    "component": "eventbus",
                    "subscription_id": subscription.id,
                    "pattern": subscription.pattern,
                    "event_id": event.event_id,
                    "kind": event.kind,
                },
            )
            return False
