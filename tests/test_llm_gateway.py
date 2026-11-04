from __future__ import annotations

import unittest
from collections.abc import Sequence

from nexus_core.config import ChainLink, LlmConfig, ProviderConfig
from nexus_core.llm_router.gateway import GatewayError, LLMGateway
from nexus_core.llm_router.providers import ProviderError, RawCompletion


class FakeProvider:
    kind = "ollama"
    models = ("fake-model",)

    def __init__(self, responses: Sequence[RawCompletion | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def complete(self, model: str, **kwargs: object) -> RawCompletion:
        self.calls.append({"model": model, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def health(self) -> dict[str, object]:
        return {"provider": "fake", "ok": True, "models": ["fake-model"]}

    async def aclose(self) -> None:
        self.closed = True


def completion(
    text: str = "answer", model: str = "fake-model", provider_name: str = "primary"
) -> RawCompletion:
    return RawCompletion(
        text=text,
        model=model,
        provider=provider_name,
        latency_ms=25,
        prompt_tokens=100,
        completion_tokens=50,
    )


def provider(name: str, *, cost: float | None = 0.0) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="ollama",
        base_url="http://127.0.0.1:11434",
        models=("fake-model",),
        enabled=True,
        authorized=True,
        input_cost_per_million=cost,
        output_cost_per_million=cost,
    )


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_model_selection_is_limited_to_configured_chain(self) -> None:
        fake = FakeProvider([completion()])
        config = LlmConfig(
            providers=(provider("primary"),),
            chain=(ChainLink("primary", "fake-model"),),
        )
        gateway = LLMGateway(config, providers={"primary": fake})
        result = await gateway.complete(
            "question", provider_name="primary", model_name="fake-model"
        )
        self.assertEqual(result.model, "fake-model")
        with self.assertRaises(ValueError):
            await gateway.complete("question", model_name="unconfigured")

    async def test_success_tracks_usage_and_estimated_cost(self) -> None:
        fake = FakeProvider([completion()])
        config = LlmConfig(
            providers=(provider("primary", cost=2.0),),
            chain=(ChainLink("primary", "fake-model"),),
        )
        gateway = LLMGateway(config, providers={"primary": fake})
        result = await gateway.complete("question")

        self.assertEqual(result.text, "answer")
        self.assertEqual(result.estimated_cost_usd, 0.0003)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(
            gateway.usage()["primary/fake-model"]["prompt_tokens"], 100
        )

    async def test_retries_transient_failure_then_succeeds(self) -> None:
        fake = FakeProvider([ProviderError("temporary"), completion()])
        config = LlmConfig(
            providers=(provider("primary"),),
            chain=(ChainLink("primary", "fake-model"),),
            max_retries=1,
        )
        gateway = LLMGateway(config, providers={"primary": fake})
        result = await gateway.complete("question")

        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(fake.calls), 2)

    async def test_falls_back_and_opens_circuit_after_threshold(self) -> None:
        first = FakeProvider([ProviderError("unavailable", retryable=False)])
        second = FakeProvider([completion(provider_name="backup")])
        config = LlmConfig(
            providers=(provider("primary"), provider("backup")),
            chain=(
                ChainLink("primary", "fake-model"),
                ChainLink("backup", "fake-model"),
            ),
            failure_threshold=1,
            cooldown_s=60,
            max_retries=2,
        )
        gateway = LLMGateway(config, providers={"primary": first, "backup": second})
        result = await gateway.complete("question")

        self.assertEqual(result.provider, "backup")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(first.calls), 1)

    async def test_task_specific_chain_and_context_budget(self) -> None:
        fake = FakeProvider([completion()])
        config = LlmConfig(
            providers=(provider("primary"),),
            chain=(ChainLink("primary", "fake-model", task="summary"),),
            max_context_tokens=256,
        )
        gateway = LLMGateway(config, providers={"primary": fake})
        result = await gateway.complete("x" * 1000, task="summary", max_tokens=128)

        self.assertTrue(result.context_truncated)
        self.assertLessEqual(len(str(fake.calls[0]["prompt"]).encode("utf-8")), 512)
        with self.assertRaises(GatewayError):
            await gateway.complete("question", task="planning", max_tokens=128)

    async def test_models_reports_discovery_and_disabled_providers(self) -> None:
        fake = FakeProvider([])
        disabled = ProviderConfig(
            name="remote", kind="openai_compatible", enabled=False, authorized=False
        )
        config = LlmConfig(providers=(provider("primary"), disabled))
        gateway = LLMGateway(config, providers={"primary": fake})
        result = await gateway.models()

        self.assertEqual(result["providers"][0]["discovered_models"], ["fake-model"])
        self.assertEqual(result["providers"][1]["health"]["state"], "disabled")
        await gateway.aclose()
        self.assertTrue(fake.closed)

    async def test_cost_is_unknown_without_explicit_rates(self) -> None:
        fake = FakeProvider([completion()])
        config = LlmConfig(
            providers=(provider("primary", cost=None),),
            chain=(ChainLink("primary", "fake-model"),),
        )
        result = await LLMGateway(config, providers={"primary": fake}).complete("question")
        self.assertIsNone(result.estimated_cost_usd)


if __name__ == "__main__":
    unittest.main()
