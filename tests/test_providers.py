"""Testes de robustez dos provedores HTTP de LLM."""

from __future__ import annotations

import httpx
import pytest

from nexus_core.config import ProviderConfig
from nexus_core.llm_router.providers import (
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
)


def _config(kind: str, name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind=kind,
        base_url="http://provider.test",
        models=("model",),
        enabled=True,
        authorized=True,
        timeout_s=5.0,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _complete(provider, client) -> object:
    try:
        return await provider.complete(
            "model",
            system=None,
            prompt="hello",
            temperature=0.0,
            max_tokens=16,
            json_mode=False,
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ollama_invalid_token_counts_degrade_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "model",
                "message": {"content": "ok"},
                "prompt_eval_count": "not-a-number",
                "eval_count": -1,
            },
        )

    client = _client(handler)
    provider = OllamaProvider(_config("ollama", "local"), client=client)
    result = await _complete(provider, client)

    assert result.text == "ok"
    assert result.prompt_tokens is None
    assert result.completion_tokens is None


@pytest.mark.asyncio
async def test_ollama_non_object_message_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "texto inesperado"})

    client = _client(handler)
    provider = OllamaProvider(_config("ollama", "local"), client=client)
    with pytest.raises(ProviderError):
        await _complete(provider, client)


@pytest.mark.asyncio
async def test_openai_choices_as_object_is_provider_error_not_keyerror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": {"0": {"message": {"content": "ok"}}}},
        )

    client = _client(handler)
    provider = OpenAICompatibleProvider(_config("openai_compatible", "remote"), client=client)
    with pytest.raises(ProviderError):
        await _complete(provider, client)


@pytest.mark.asyncio
async def test_openai_invalid_usage_does_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "model",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": "oops", "completion_tokens": "42"},
            },
        )

    client = _client(handler)
    provider = OpenAICompatibleProvider(_config("openai_compatible", "remote"), client=client)
    result = await _complete(provider, client)

    assert result.text == "ok"
    assert result.prompt_tokens is None
    assert result.completion_tokens == 42


__all__: list[str] = []
