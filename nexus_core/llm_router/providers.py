"""Provedores de LLM sobre HTTP — sem SDK, sem telemetria embutida.

Dois tipos: ``ollama`` (local, padrão do projeto) e ``openai_compatible``, que
só é instanciado quando a configuração traz ``enabled = true`` **e**
``authorized = true``. Toda falha vira ``ProviderError`` com a informação de se
vale a pena tentar outro modelo/provedor.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..config import LlmConfig, ProviderConfig


class ProviderError(Exception):
    """Falha de provedor: rede, HTTP ou resposta inutilizável."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True, slots=True)
class RawCompletion:
    text: str
    model: str
    provider: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMProvider(Protocol):
    name: str
    kind: str
    models: tuple[str, ...]

    async def complete(
        self,
        model: str,
        *,
        system: str | None,
        prompt: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> RawCompletion: ...

    async def health(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


def _messages(system: str | None, prompt: str) -> list[dict[str, str]]:
    messages = [{"role": "user", "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


class OllamaProvider:
    kind = "ollama"

    def __init__(self, cfg: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.name = cfg.name
        self.models = cfg.models
        self._base = cfg.base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=False)
        self._owns_client = client is None

    async def complete(
        self,
        model: str,
        *,
        system: str | None,
        prompt: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> RawCompletion:
        body: dict[str, Any] = {
            "model": model,
            "messages": _messages(system, prompt),
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            body["format"] = "json"

        started = time.monotonic()
        try:
            response = await self._client.post(f"{self._base}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: falha de rede ({exc.__class__.__name__})") from exc
        latency = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name}: HTTP {response.status_code}",
                status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: resposta não é JSON", retryable=False) from exc

        message = data.get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise ProviderError(f"{self.name}: resposta vazia do modelo {model}")
        return RawCompletion(
            text=text,
            model=str(data.get("model") or model),
            provider=self.name,
            latency_ms=latency,
            prompt_tokens=(int(data["prompt_eval_count"]) if data.get("prompt_eval_count") is not None else None),
            completion_tokens=(int(data["eval_count"]) if data.get("eval_count") is not None else None),
        )

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._base}/api/tags")
        except httpx.HTTPError as exc:
            return {"provider": self.name, "ok": False, "error": exc.__class__.__name__}
        if response.status_code >= 400:
            return {"provider": self.name, "ok": False, "error": f"HTTP {response.status_code}"}
        try:
            models = [item.get("name", "") for item in response.json().get("models", [])]
        except (ValueError, AttributeError):
            models = []
        return {"provider": self.name, "ok": True, "models": models}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class OpenAICompatibleProvider:
    kind = "openai_compatible"

    def __init__(self, cfg: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        if not cfg.authorized:
            raise ProviderError(
                f"{cfg.name}: provedor externo sem authorized = true", retryable=False
            )
        self.name = cfg.name
        self.models = cfg.models
        self._base = cfg.base_url.rstrip("/")
        self._key_env = cfg.api_key_env
        self._client = client or httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=False)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self._key_env or "", "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def complete(
        self,
        model: str,
        *,
        system: str | None,
        prompt: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> RawCompletion:
        body: dict[str, Any] = {
            "model": model,
            "messages": _messages(system, prompt),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self._base}/chat/completions", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: falha de rede ({exc.__class__.__name__})") from exc
        latency = int((time.monotonic() - started) * 1000)

        if response.status_code in (401, 403):
            raise ProviderError(
                f"{self.name}: credencial recusada (HTTP {response.status_code})",
                status=response.status_code,
                retryable=False,
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name}: HTTP {response.status_code}",
                status=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        try:
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            text = str((choice.get("message") or {}).get("content") or "").strip()
            usage = data.get("usage") or {}
        except (ValueError, AttributeError, IndexError) as exc:
            raise ProviderError(f"{self.name}: resposta inesperada", retryable=False) from exc
        if not text:
            raise ProviderError(f"{self.name}: resposta vazia do modelo {model}")
        return RawCompletion(
            text=text,
            model=str(data.get("model") or model),
            provider=self.name,
            latency_ms=latency,
            prompt_tokens=(int(usage["prompt_tokens"]) if usage.get("prompt_tokens") is not None else None),
            completion_tokens=(int(usage["completion_tokens"]) if usage.get("completion_tokens") is not None else None),
        )

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._base}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            return {"provider": self.name, "ok": False, "error": exc.__class__.__name__}
        if response.status_code >= 400:
            return {"provider": self.name, "ok": False, "error": f"HTTP {response.status_code}"}
        try:
            models = [
                str(item.get("id"))
                for item in response.json().get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        except (ValueError, AttributeError):
            models = []
        return {"provider": self.name, "ok": True, "models": models}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_providers(cfg: LlmConfig) -> dict[str, LLMProvider]:
    """Instancia somente provedores habilitados e autorizados."""
    providers: dict[str, LLMProvider] = {}
    for item in cfg.providers:
        if not item.enabled:
            continue
        try:
            if item.kind == "ollama":
                providers[item.name] = OllamaProvider(item)
            elif item.kind == "openai_compatible":
                providers[item.name] = OpenAICompatibleProvider(item)
        except ProviderError:
            continue
    return providers
