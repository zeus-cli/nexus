from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock

import asyncio
import tempfile
import unittest
from pathlib import Path

from nexus_core.cognitive import CognitiveCore
from nexus_core.config import ChainLink, LlmConfig, ProviderConfig
from nexus_core.eventbus import EventBus
from nexus_core.llm_router.gateway import GatewayError, LLMGateway
from nexus_core.llm_router.providers import RawCompletion
from nexus_core.memory.eventstore import EventStore
from nexus_core.memory.records import MemoryStore
from nexus_core.memory.store import Database
from nexus_core.recorder import EventRecorder
from nexus_core.reflection import ReflectionDecision, ReflectionResult


class FakeProvider:
    kind = "ollama"
    models = ("fake",)

    def __init__(self, *, slow: bool = False) -> None:
        self.slow = slow
        self.system: str | None = None

    async def complete(self, model: str, **kwargs):
        self.system = kwargs["system"]
        if self.slow:
            await asyncio.sleep(0.05)
        return RawCompletion(
            text="draft output",
            model=model,
            provider="local",
            latency_ms=10,
            prompt_tokens=10,
            completion_tokens=3,
        )

    async def health(self):
        return {"ok": True, "models": ["fake"]}

    async def aclose(self):
        return None


class CognitiveCoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "data" / "memory.db")
        self.database.connect()
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.memory = MemoryStore(self.database, vector_dir='/tmp/vectors')
        self.events = EventStore(self.database)
        self.recorder = EventRecorder(self.events, EventBus())

    def build_core(self, provider: FakeProvider | None) -> CognitiveCore:
        provider_config = ProviderConfig(
            name="local",
            kind="ollama",
            base_url="http://127.0.0.1:11434",
            models=("fake",),
            enabled=True,
            authorized=True,
        )
        config = LlmConfig(
            providers=(provider_config,),
            chain=(ChainLink("local", "fake"),),
        )
        gateway = LLMGateway(config, providers={"local": provider} if provider else {})
        self.addAsyncCleanup(gateway.aclose)
        reflection = AsyncMock()
        reflection.critique.return_value = ReflectionResult(
            ReflectionDecision.SUCCESS, "ok"
        )
        return CognitiveCore(
            gateway,
            self.memory,
            self.recorder,
            MagicMock(),
            reflection,
            MagicMock(),
            timeout_s=0.01,
        )

    async def test_task_uses_memory_and_records_lifecycle_without_content(self) -> None:
        self.memory.create(
            kind="long_term",
            title="Language preference",
            content="Use Portuguese for Python project status updates.",
        )
        fake = FakeProvider()
        result = await self.build_core(fake).run(
            "Prepare a Python project update.", task="summary", correlation_id="run-7"
        )

        self.assertEqual(result.final_completion.text, "draft output")
        self.assertEqual(result.correlation_id, "run-7")
        self.assertEqual(result.memory_ids, (self.memory.search("Python")[0].memory_id,))
        self.assertIn("untrusted data", fake.system or "")
        self.assertIn("Portuguese", fake.system or "")
        self.assertEqual(
            [event.kind for event in self.events.query(correlation_id="run-7")],
            [
                "agent.task.completed",
                "agent.reflection.decision",
                "agent.reflection.triggered",
                "agent.task.started",
            ],
        )
        for event in self.events.query(correlation_id="run-7"):
            self.assertNotIn("Prepare a Python", str(dict(event.payload)))
            self.assertNotIn("draft output", str(dict(event.payload)))

    async def test_failed_task_is_recorded(self) -> None:
        core = self.build_core(None)
        with self.assertRaises(GatewayError):
            await core.run("question")
        kinds = {event.kind for event in self.events.query()}
        self.assertIn("agent.task.started", kinds)
        self.assertIn("agent.task.failed", kinds)

    async def test_slow_task_times_out_and_is_recorded(self) -> None:
        core = self.build_core(FakeProvider(slow=True))
        with self.assertRaises(TimeoutError):
            await core.run("question")
        self.assertEqual(self.events.count(kind="agent.task.timed_out"), 1)


if __name__ == "__main__":
    unittest.main()