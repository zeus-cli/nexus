"""Roteamento LLM com fallback, limite de contexto e contabilidade local."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..config import LLM_TASKS, LlmConfig, ProviderConfig
from .providers import LLMProvider, ProviderError, RawCompletion, build_providers


class GatewayError(RuntimeError):
    """Nenhum modelo configurado conseguiu concluir a solicitação."""


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    model: str
    provider: str
    task: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost_usd: float | None
    attempts: int
    context_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "task": self.task,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "attempts": self.attempts,
            "context_truncated": self.context_truncated,
        }


class LLMGateway:
    """Resolve tarefas pela cadeia configurada sem executar conteúdo do modelo."""

    def __init__(
        self,
        config: LlmConfig,
        *,
        providers: Mapping[str, LLMProvider] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.config = config
        self._providers: dict[str, LLMProvider] | None = (
            dict(providers) if providers is not None else None
        )
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._usage: dict[str, dict[str, int | float]] = {}
        self._closed = False

    def _instances(self) -> dict[str, LLMProvider]:
        if self._closed:
            raise GatewayError("gateway LLM já foi encerrado")
        if self._providers is None:
            self._providers = build_providers(self.config)
        return self._providers

    async def models(self) -> dict[str, Any]:
        """Retorna estado, modelos descobertos e capacidades do protocolo."""
        instances = self._instances()
        results: list[dict[str, Any]] = []
        for item in self.config.providers:
            provider = instances.get(item.name)
            health: dict[str, Any]
            if not item.enabled:
                health = {"ok": False, "state": "disabled"}
            elif provider is None:
                health = {"ok": False, "state": "not_authorized_or_unavailable"}
            else:
                try:
                    health = await provider.health()
                except Exception as exc:  # provedores não podem derrubar o painel
                    health = {"ok": False, "error": type(exc).__name__}
            remaining = max(0.0, self._cooldown_until.get(item.name, 0.0) - self._clock())
            discovered = health.get("models", [])
            results.append(
                {
                    "name": item.name,
                    "kind": item.kind,
                    "enabled": item.enabled,
                    "authorized": item.authorized,
                    "configured_models": list(item.models),
                    "discovered_models": discovered,
                    "health": health,
                    "circuit": {
                        "consecutive_failures": self._failures.get(item.name, 0),
                        "cooldown_remaining_s": round(remaining, 3),
                    },
                    "capabilities": _protocol_capabilities(item.kind),
                }
            )
        return {
            "providers": results,
            "routes": [
                {"provider": link.provider, "model": link.model, "task": link.task}
                for link in self.config.chain
            ],
            "usage": self.usage(),
        }

    async def complete(
        self,
        prompt: str,
        *,
        task: str = "reasoning",
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> CompletionResult:
        if task not in LLM_TASKS:
            raise ValueError(f"task inválida: {task!r}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt deve ser uma string não vazia")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("max_tokens deve ser um inteiro positivo")
        if max_tokens >= self.config.max_context_tokens:
            raise ValueError("max_tokens precisa ser menor que max_context_tokens")
        if not math.isfinite(temperature) or not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature deve estar entre 0 e 2")

        system_text, prompt_text, truncated = _fit_context(
            system, prompt, self.config.max_context_tokens - max_tokens
        )
        links = [link for link in self.config.chain if link.task in (None, task)]
        if provider_name is not None or model_name is not None:
            links = [
                link for link in links
                if (provider_name is None or link.provider == provider_name)
                and (model_name is None or link.model == model_name)
            ]
            if not links:
                raise ValueError("provedor/modelo não está configurado para esta tarefa")
        if not links:
            raise GatewayError(f"nenhum modelo configurado para a tarefa {task}")

        instances = self._instances()
        attempts = 0
        errors: list[str] = []
        for link in links:
            provider_config = self.config.provider(link.provider)
            provider = instances.get(link.provider)
            if provider_config is None or not provider_config.enabled or provider is None:
                errors.append(f"{link.provider}/{link.model}: provedor desativado ou indisponível")
                continue
            if self._cooldown_until.get(link.provider, 0.0) > self._clock():
                errors.append(f"{link.provider}/{link.model}: circuito em cooldown")
                continue

            for retry in range(self.config.max_retries + 1):
                attempts += 1
                try:
                    raw = await provider.complete(
                        link.model,
                        system=system_text,
                        prompt=prompt_text,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                    )
                except ProviderError as exc:
                    self._record_failure(link.provider)
                    errors.append(f"{link.provider}/{link.model}: {exc}")
                    circuit_open = self._cooldown_until.get(link.provider, 0.0) > self._clock()
                    if not exc.retryable or circuit_open or retry >= self.config.max_retries:
                        break
                    continue
                except Exception as exc:
                    self._record_failure(link.provider)
                    errors.append(f"{link.provider}/{link.model}: falha {type(exc).__name__}")
                    break

                self._failures[link.provider] = 0
                self._cooldown_until.pop(link.provider, None)
                cost = _estimate_cost(provider_config, raw)
                self._record_usage(raw, cost)
                return CompletionResult(
                    text=raw.text,
                    model=raw.model,
                    provider=raw.provider,
                    task=task,
                    latency_ms=raw.latency_ms,
                    prompt_tokens=raw.prompt_tokens,
                    completion_tokens=raw.completion_tokens,
                    estimated_cost_usd=cost,
                    attempts=attempts,
                    context_truncated=truncated,
                )

        details = "; ".join(errors) if errors else "nenhum provedor utilizável"
        raise GatewayError(f"todos os modelos falharam para {task}: {details}")

    def _record_failure(self, provider: str) -> None:
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        if count >= self.config.failure_threshold:
            self._cooldown_until[provider] = self._clock() + self.config.cooldown_s

    def _record_usage(self, result: RawCompletion, cost: float | None) -> None:
        bucket = self._usage.setdefault(
            f"{result.provider}/{result.model}",
            {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "unknown_token_requests": 0,
                "known_cost_requests": 0,
                "estimated_cost_usd": 0.0,
            },
        )
        bucket["requests"] += 1
        if result.prompt_tokens is None or result.completion_tokens is None:
            bucket["unknown_token_requests"] += 1
        else:
            bucket["prompt_tokens"] += result.prompt_tokens
            bucket["completion_tokens"] += result.completion_tokens
        if cost is not None:
            bucket["known_cost_requests"] += 1
            bucket["estimated_cost_usd"] += cost

    def usage(self) -> dict[str, dict[str, int | float]]:
        return {key: dict(value) for key, value in self._usage.items()}

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._providers is not None:
            await asyncio.gather(
                *(provider.aclose() for provider in self._providers.values()),
                return_exceptions=True,
            )


def _fit_context(
    system: str | None,
    prompt: str,
    input_token_budget: int,
) -> tuple[str | None, str, bool]:
    """Bound input bytes using an approximate four-bytes-per-token budget."""
    if input_token_budget <= 0:
        raise ValueError("não há orçamento para contexto de entrada")
    byte_budget = input_token_budget * 4
    system_text = system or ""
    system_bytes = system_text.encode("utf-8")
    if len(system_bytes) > byte_budget:
        raise ValueError("instrução system excede o orçamento de contexto")
    prompt_bytes = prompt.encode("utf-8")
    remaining = byte_budget - len(system_bytes)
    if len(prompt_bytes) <= remaining:
        return system_text or None, prompt, False
    trimmed = prompt_bytes[:remaining].decode("utf-8", errors="ignore").rstrip()
    return system_text or None, trimmed, True


def _estimate_cost(config: ProviderConfig, result: RawCompletion) -> float | None:
    if (
        config.input_cost_per_million is None
        or config.output_cost_per_million is None
        or result.prompt_tokens is None
        or result.completion_tokens is None
    ):
        return None
    return (
        result.prompt_tokens * config.input_cost_per_million
        + result.completion_tokens * config.output_cost_per_million
    ) / 1_000_000


def _protocol_capabilities(kind: str) -> dict[str, bool]:
    return {
        "chat_completions": kind in {"ollama", "openai_compatible"},
        "json_mode": kind in {"ollama", "openai_compatible"},
        "streaming": False,
        "model_discovery": kind in {"ollama", "openai_compatible"},
        "reported_token_usage": kind in {"ollama", "openai_compatible"},
    }