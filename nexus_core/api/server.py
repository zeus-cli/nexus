"""Execução do servidor HTTP/WebSocket local.

O token **nunca** é registrado em log: apenas sua origem (arquivo ou env).
"""

from __future__ import annotations

import logging

from ..app import NexusRuntime
from ..config import is_loopback_host
from .app import create_app
from .auth import Authenticator, resolve_token
from .realtime import RealtimeHub

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def bridge_uvicorn_logging() -> None:
    """Faz o uvicorn escrever nos mesmos handlers do NEXUS (arquivo + console)."""
    root = logging.getLogger("zeus")
    for name in UVICORN_LOGGERS:
        target = logging.getLogger(name)
        target.handlers = list(root.handlers)
        target.setLevel(root.level)
        target.propagate = False


def run_server(
    runtime: NexusRuntime,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    bind_host = host or runtime.config.api.host
    if not is_loopback_host(bind_host):
        raise ValueError(
            "a API aceita somente endereços IP de loopback; TLS não está disponível"
        )

    import uvicorn

    config = runtime.config
    info = resolve_token(config)
    if info.created:
        runtime.logger.warning(
            "token local da API criado; use 'nexus token show' para consultá-lo",
            extra={"component": "api", "token_file": str(info.path)},
        )

    auth = Authenticator(
        info.token, config.safety.autonomy_level, source=info.source
    )
    hub = RealtimeHub(runtime.bus, logger=logging.getLogger("zeus.api.realtime"))
    app = create_app(runtime, auth=auth, hub=hub)

    bridge_uvicorn_logging()
    bind_port = port or config.api.port
    runtime.logger.info(
        "API local subindo",
        extra={
            "component": "api",
            "host": bind_host,
            "port": bind_port,
            "autonomy_level": config.safety.autonomy_level,
            "token_source": info.source,
            "auth_required": True,
        },
    )
    uvicorn.run(
        app,
        host=bind_host,
        port=bind_port,
        log_config=None,
        access_log=True,
        server_header=False,
    )
