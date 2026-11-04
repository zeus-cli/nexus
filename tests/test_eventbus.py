from __future__ import annotations

import asyncio
import unittest

from nexus_core.eventbus import EventBus, pattern_matches
from nexus_core.events import Event


def make_event(kind: str = "perception.fs.created") -> Event:
    return Event(kind=kind, source="watcher_rs", payload={"path": "/tmp/a"})


class PatternTest(unittest.TestCase):
    def test_patterns(self) -> None:
        self.assertTrue(pattern_matches("*", "perception.fs.created"))
        self.assertTrue(pattern_matches("perception.*", "perception.fs.created"))
        self.assertTrue(pattern_matches("perception.**", "perception.fs.created"))
        self.assertTrue(pattern_matches("perception.fs.created", "perception.fs.created"))
        self.assertFalse(pattern_matches("perception.*", "agent.plan"))
        self.assertFalse(pattern_matches("perception.fs", "perception.fs.created"))
        self.assertFalse(pattern_matches("percep", "perception.fs.created"))


class BusTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_and_wildcard_delivery(self) -> None:
        bus = EventBus()
        received: list[str] = []

        async def handler(event: Event) -> None:
            received.append(event.kind)

        bus.subscribe("perception.fs.created", handler)
        bus.subscribe("perception.*", handler)
        bus.subscribe("*", handler)

        delivered = await bus.publish(make_event())
        self.assertEqual(delivered, 3)
        self.assertEqual(received, ["perception.fs.created"] * 3)
        self.assertEqual(bus.stats.delivered, 3)

    async def test_sync_handler_supported(self) -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe("perception.*", lambda event: seen.append(event.kind))
        await bus.publish(make_event())
        self.assertEqual(seen, ["perception.fs.created"])

    async def test_handler_failure_is_isolated(self) -> None:
        bus = EventBus()
        received: list[str] = []

        async def broken(event: Event) -> None:
            raise RuntimeError("falha proposital")

        async def healthy(event: Event) -> None:
            received.append(event.kind)

        bus.subscribe("perception.*", broken)
        bus.subscribe("perception.*", healthy)

        delivered = await bus.publish(make_event())
        self.assertEqual(delivered, 1)
        self.assertEqual(received, ["perception.fs.created"])
        self.assertEqual(bus.stats.failures, 1)

    async def test_timeout_is_isolated(self) -> None:
        bus = EventBus(handler_timeout=0.01)
        received: list[str] = []

        async def slow(event: Event) -> None:
            await asyncio.sleep(0.2)

        async def healthy(event: Event) -> None:
            received.append(event.kind)

        bus.subscribe("*", slow)
        bus.subscribe("*", healthy)

        delivered = await bus.publish(make_event())
        self.assertEqual(delivered, 1)
        self.assertEqual(received, ["perception.fs.created"])
        self.assertEqual(bus.stats.timeouts, 1)

    async def test_order_preserved_per_subscriber(self) -> None:
        bus = EventBus()
        order: list[int] = []

        async def handler(event: Event) -> None:
            order.append(int(dict(event.payload)["n"]))
            await asyncio.sleep(0)

        bus.subscribe("agent.step", handler)
        for index in range(5):
            await bus.publish(Event(kind="agent.step", source="s", payload={"n": index}))
        self.assertEqual(order, [0, 1, 2, 3, 4])

    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        received: list[str] = []

        async def handler(event: Event) -> None:
            received.append(event.kind)

        subscription = bus.subscribe("*", handler)
        await bus.publish(make_event())
        self.assertTrue(bus.unsubscribe(subscription))
        self.assertFalse(bus.unsubscribe(subscription))
        await bus.publish(make_event())
        self.assertEqual(len(received), 1)

    async def test_invalid_subscription_rejected(self) -> None:
        bus = EventBus()
        with self.assertRaises(ValueError):
            bus.subscribe("", lambda event: None)
        with self.assertRaises(TypeError):
            bus.subscribe("*", "não chamável")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            EventBus(handler_timeout=0)


if __name__ == "__main__":
    unittest.main()
