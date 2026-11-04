"""Inicialização do NEXUS e ciclo de vida do processo.

Ordem obrigatória de bootstrap (Fase 1):

1. carregar e validar configuração;
2. configurar logging estruturado;
3. abrir SQLite, aplicar migrações;
4. instanciar EventStore, EventBus e EventRecorder;
5. registrar início no event store.

Qualquer falha nesses passos impede a subida do serviço: um runtime meio
inicializado é mais perigoso que um serviço parado.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .automation import AutomationEngine
from .reflection import ReflectionEngine
from .task_store import TaskStore
from .config import AUTONOMY_LEVELS, Config, load_config
from .cognitive import CognitiveCore
from .eventbus import EventBus
from .events import Event
from .logging_setup import configure_logging, get_logger
from .llm_router.gateway import LLMGateway
from .memory.eventstore import EventStore
from .memory.records import MemoryStore
from .memory.store import Database
from .privatefs import secure_runtime_data_directory
from .recorder import EventRecorder
from .sentinel import SentinelReceiver
from .resources import ResourceManager

SERVICE_SOURCE = "nexus_core.app"


@dataclass
class NexusRuntime:
    config: Config
    database: Database
    store: EventStore
    memory: MemoryStore
    bus: EventBus
    recorder: EventRecorder
    llm_gateway: LLMGateway
    cognitive: CognitiveCore
    automation: AutomationEngine
    logger: Any
    started_at: datetime
    resources: ResourceManager
    sentinel: SentinelReceiver | None = None

    async def aclose(self) -> None:
        """Close asynchronous provider clients and the SQLite connection."""
        try:
            await self.llm_gateway.aclose()
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.database.close()
        finally:
            self.logger.info("runtime encerrado", extra={"component": "app"})


def bootstrap(config_path: str | Path | None = None, *, console_logs: bool = True) -> NexusRuntime:
    config = load_config(config_path)
    secure_runtime_data_directory(config.paths.data_dir)
    configure_logging(config.logging, config.paths.log_file, console=console_logs)
    logger = get_logger("app")

    database = Database(config.paths.database)
    database.connect()
    applied = database.migrate()

    store = EventStore(
        database,
        dedup_window_hours=config.events.dedup_window_hours,
        max_payload_bytes=config.events.max_payload_bytes,
        retention_days=config.events.retention_days,
        query_limit=config.events.query_limit,
    )
    memory = MemoryStore(database, vector_dir=str(config.paths.data_dir / "vectors"))
    bus = EventBus(logger=get_logger("eventbus"))
    recorder = EventRecorder(store, bus, logger=get_logger("recorder"))
    llm_gateway = LLMGateway(config.llm)
    automation = AutomationEngine(config.safety.autonomy_level, recorder)
    reflection = ReflectionEngine(llm_gateway)
    task_store = TaskStore()
    cognitive = CognitiveCore(llm_gateway, memory, recorder, automation, reflection, task_store)

    resources = ResourceManager()
    runtime = NexusRuntime(
        config=config,
        database=database,
        store=store,
        memory=memory,
        bus=bus,
        recorder=recorder,
        llm_gateway=llm_gateway,
        cognitive=cognitive,
        automation=automation,
        logger=logger,
        started_at=datetime.now(timezone.utc),
        resources=resources,
    )
    logger.info(
        "runtime inicializado",
        extra={
            "component": "bootstrap",
            "version": __version__,
            "environment": config.environment,
            "autonomy_level": config.safety.autonomy_level,
            "database": str(config.paths.database),
            "schema_version": database.schema_version(),
            "migrations_applied": applied,
        },
    )
    return runtime


def health(runtime: NexusRuntime) -> dict[str, Any]:
    """Estado operacional consumível por CLI, HUD e futura API."""
    config = runtime.config
    integrity = runtime.database.integrity_check(quick=True)
    stats = runtime.store.stats()
    checks = [
        {"name": "configuracao", "ok": True},
        {"name": "banco", "ok": integrity == "ok", "detail": integrity},
        {"name": "schema", "ok": runtime.database.pending_migrations() == []},
    ]
    status = "ok" if all(check["ok"] for check in checks) else "degraded"
    return {
        "status": status,
        "name": "NEXUS",
        "version": __version__,
        "environment": config.environment,
        "uptime_s": round((datetime.now(timezone.utc) - runtime.started_at).total_seconds(), 3),
        "autonomy": {
            "level": config.safety.autonomy_level,
            "name": AUTONOMY_LEVELS[config.safety.autonomy_level],
        },
        "database": {
            "path": str(config.paths.database),
            "schema_version": runtime.database.schema_version(),
            "integrity": integrity,
            "events": stats["total"],
            "size_bytes": stats["size_bytes"],
        },
        "retention": {
            "enabled": config.events.retention_days > 0,
            "days": config.events.retention_days,
        },
        "events": stats,
        "memory": runtime.memory.stats(),
        "sentinel": runtime.sentinel.health() if runtime.sentinel else {
            "connected": False, "last_seen": None, "age_s": None, "sample": None,
        },
        "resources": runtime.resources.get_stats(),
        "llm": {
            "enabled_providers": sum(item.enabled for item in config.llm.providers),
            "configured_routes": len(config.llm.chain),
            "usage": runtime.llm_gateway.usage(),
        },
        "bus": runtime.bus.stats.as_dict(),
        "log_file": str(config.paths.log_file),
        "checks": checks,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def serve(
    runtime: NexusRuntime,
    *,
    interval: float = 60.0,
    iterations: int | None = None,
) -> None:
    """Heartbeat, retenção e socket IPC do Sentinel para processos residentes."""
    if interval <= 0:
        raise ValueError("interval deve ser positivo")
    if iterations is not None and iterations <= 0:
        raise ValueError("iterations deve ser positivo ou None")

    logger = runtime.logger
    started = time.monotonic()
    logger.info(
        "serviço iniciado",
        extra={"component": "service", "pid": os.getpid(), "interval_s": interval},
    )
    await runtime.recorder.record(
        Event(
            kind="system.started",
            source=SERVICE_SOURCE,
            payload={
                "pid": os.getpid(),
                "version": __version__,
                "autonomy_level": runtime.config.safety.autonomy_level,
            },
            dedup=False,
        )
    )

    count = 0
    sentinel = SentinelReceiver(
        runtime.config.paths.data_dir / "run" / "sentinel.sock", runtime.recorder
    )
    try:
        await sentinel.start()
        runtime.sentinel = sentinel
        while iterations is None or count < iterations:
            purged = runtime.store.apply_retention()
            purged_memories = runtime.memory.purge_expired()
            await runtime.recorder.record(
                Event(
                    kind="system.heartbeat",
                    source=SERVICE_SOURCE,
                    payload={
                        "uptime_s": round(time.monotonic() - started, 3),
                        "events": runtime.store.count(),
                        "purged": purged,
                        "purged_memories": purged_memories,
                    },
                )
            )
            count += 1
            if iterations is not None and count >= iterations:
                break
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("serviço interrompido por sinal", extra={"component": "service"})
        raise
    finally:
        await sentinel.close()
        runtime.sentinel = None
        uptime = round(time.monotonic() - started, 3)
        await runtime.recorder.record(
            Event(
                kind="system.stopped",
                source=SERVICE_SOURCE,
                payload={"uptime_s": uptime, "cycles": count},
                dedup=False,
            )
        )
        logger.info(
            "serviço encerrado",
            extra={"component": "service", "uptime_s": uptime, "cycles": count},
        )