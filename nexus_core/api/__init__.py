"""Camada 2 — API local do NEXUS.

Expõe o runtime (status, eventos, telemetria) via HTTP e WebSocket em
``127.0.0.1``, sempre autenticada por token local e limitada pelo nível de
autonomia configurado.
"""

from .auth import Authenticator, TokenInfo, resolve_token
from .realtime import RealtimeHub

__all__ = ["Authenticator", "RealtimeHub", "TokenInfo", "resolve_token"]
