"""LLM gateway and provider adapters for NEXUS."""

from .gateway import CompletionResult, GatewayError, LLMGateway
from .providers import (
    LLMProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    RawCompletion,
    build_providers,
)

__all__ = [
    "CompletionResult",
    "GatewayError",
    "LLMGateway",
    "LLMProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "RawCompletion",
    "build_providers",
]
