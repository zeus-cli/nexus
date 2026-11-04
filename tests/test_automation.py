from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from nexus_core.automation import (
    AutomationDenied,
    ActionResult,
    AutomationEngine,
)
from nexus_core.eventbus import EventBus
from nexus_core.memory.eventstore import EventStore
from nexus_core.memory.store import Database
from nexus_core.recorder import EventRecorder


class AutomationEngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "data" / "events.db")
        self.database.connect()
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.events = EventStore(self.database)
        self.recorder = EventRecorder(self.events, EventBus())

    async def test_requires_controlled_execution_autonomy(self) -> None:
        called = False

        async def runner(argv, timeout):
            nonlocal called
            called = True
            return 0, b"", b""

        engine = AutomationEngine(1, self.recorder, runner=runner)
        with self.assertRaises(AutomationDenied):
            await engine.run("system.uptime")
        self.assertFalse(called)

    async def test_runs_only_registered_argv_and_audits_metadata(self) -> None:
        calls = []

        async def runner(argv, timeout):
            calls.append((argv, timeout))
            return 0, b"up 4 hours\n", b""

        engine = AutomationEngine(2, self.recorder, runner=runner)
        result = await engine.run("system.uptime")
        self.assertEqual(result.stdout, "up 4 hours\n")
        self.assertEqual(calls[0][0], ("/usr/bin/uptime",))
        self.assertEqual(
            {event.kind for event in self.events.query()},
            {"automation.action.started", "automation.action.completed"},
        )

    async def test_rejects_unknown_action_before_runner(self) -> None:
        async def runner(argv, timeout):
            raise AssertionError("unregistered command must never run")

        engine = AutomationEngine(2, self.recorder, runner=runner)
        with self.assertRaises(ValueError):
            await engine.run("shell.execute")

    async def test_timeout_is_bounded_and_recorded(self) -> None:
        async def runner(argv, timeout):
            await asyncio.sleep(timeout + 0.6)
            return 0, b"", b""

        engine = AutomationEngine(2, self.recorder, timeout_s=0.01, runner=runner)
        with self.assertRaisesRegex(Exception, "excedeu"):
            await engine.run("system.uptime")
        self.assertEqual(self.events.count(kind="automation.action.failed"), 1)

    async def test_output_is_capped(self) -> None:
        async def runner(argv, timeout):
            return 0, b"x" * 9000, b""

        engine = AutomationEngine(
            2,
            self.recorder,
            actions=(ActionResult("test.output", "bounded output", ("/bin/echo",)),),
            runner=runner,
        )
        result = await engine.run("test.output")
        self.assertEqual(len(result.stdout.encode("utf-8")), 8192)
        self.assertTrue(result.truncated)


if __name__ == "__main__":
    unittest.main()
