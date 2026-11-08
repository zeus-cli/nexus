"""API local do NEXUS (Camada 2).

Superfície mínima e auditável sobre o runtime existente:

* ``GET  /health``                 — liveness sem autenticação, sem tocar o banco;
* ``GET  /api/v1/status``          — health completo (escopo ``read``);
* ``GET  /api/v1/config``          — configuração efetiva (``read``);
* ``GET  /api/v1/events``          — consulta filtrada (``read``);
* ``GET  /api/v1/memories``        — consulta a memória lexical estruturada (``read``);
* ``POST /api/v1/tasks``           — orquestra tarefas sem executar a saída (``write``);
* ``POST /api/v1/automation/actions/{id}`` — allowlist de diagnóstico (``execute``);
* ``GET  /api/v1/events/{id}``     — evento individual (``read``);
* ``POST /api/v1/events``          — registro de evento (``write``);
* ``GET  /api/v1/kinds``           — agregação por kind (``read``);
* ``GET  /api/v1/stats``           — telemetria de store, bus e tempo real (``read``);
* ``GET  /api/v1/sessions``        — sessões de tempo real abertas (``read``);
* ``WS   /api/v1/ws/events``       — stream de eventos com autenticação por frame.

Nenhuma rota executa saída arbitrária do modelo ou ignora a camada de
segurança: execução está limitada à allowlist de diagnósticos somente leitura.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..approval_api import APIUser, ApprovalAPI
from ..automation import ApprovalRequired, AutomationDenied, AutomationError
from ..app import NexusRuntime, health, serve
from ..events import Event, Severity, parse_timestamp
from ..llm_router.gateway import GatewayError
from ..agent_manifest import AgentManifest, AgentRole
from ..world_builder import WorldBuilder
from ..simulation_types import SimulationParadigm, SimulationParameters
from .auth import Authenticator
from .realtime import HubClient, RealtimeHub

API_PREFIX = "/api/v1"
SOURCE = "api"
WS_AUTH_TIMEOUT = 5.0
WS_PING_INTERVAL = 20.0
WS_REPLAY_MAX = 200
DENIAL_THROTTLE_SECONDS = 2.0
HEARTBEAT_INTERVAL = 60.0

SEVERITIES = tuple(item.value for item in Severity)


class EventIn(BaseModel):
    kind: str = Field(min_length=1, max_length=120, examples=["agent.task.completed"])
    source: str = Field(default="api", min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["debug", "info", "warning", "error", "critical"] = "info"
    correlation_id: str | None = Field(default=None, max_length=64)


class CompletionIn(BaseModel):
    task: Literal["reasoning", "planning", "summary"] = "reasoning"
    prompt: str = Field(min_length=1, max_length=1_000_000)
    system: str | None = Field(default=None, max_length=32_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32_768)
    json_mode: bool = False
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=200)


class MemoryIn(BaseModel):
    kind: Literal["short_term", "long_term", "episodic", "semantic"]
    title: str = Field(default="", max_length=4096)
    content: str = Field(min_length=1, max_length=1_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None


class TaskIn(BaseModel):
    task: Literal["reasoning", "planning", "summary"] = "reasoning"
    prompt: str = Field(min_length=1, max_length=1_000_000)
    system: str | None = Field(default=None, max_length=32_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32_768)
    json_mode: bool = False
    correlation_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=200)


class ActionRunIn(BaseModel):
    approval_id: str | None = Field(default=None, max_length=64)


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approve", "deny", "revoke"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _ws_token(raw: str) -> str | None:
    """Aceita ``{"type":"auth","token":"..."}`` ou o token em texto puro."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None
    if isinstance(message, dict):
        token = message.get("token")
        return token.strip() if isinstance(token, str) else None
    return None


async def _reject_socket(websocket: WebSocket, code: int, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
        await websocket.close(code=code, reason=message[:120])
    except (WebSocketDisconnect, RuntimeError):  # cliente já saiu
        pass


def create_app(
    runtime: NexusRuntime,
    *,
    auth: Authenticator,
    hub: RealtimeHub | None = None,
) -> FastAPI:
    logger = logging.getLogger("zeus.api")
    denial = {"last": 0.0, "suppressed": 0}

    async def audit_denial(target: Request | WebSocket, reason: str) -> None:
        """Registra tentativa negada (com throttle) sem derrubar a resposta."""
        now = asyncio.get_running_loop().time()
        if now - denial["last"] < DENIAL_THROTTLE_SECONDS:
            denial["suppressed"] += 1
            return
        denial["last"] = now
        suppressed = denial["suppressed"]
        denial["suppressed"] = 0
        client = getattr(target, "client", None)
        try:
            await runtime.recorder.record(
                Event(
                    kind="security.api.denied",
                    source=SOURCE,
                    severity=Severity.WARNING,
                    payload={
                        "reason": reason,
                        "path": str(target.url.path),
                        "client": getattr(client, "host", None),
                        "suppressed_since_last": suppressed,
                    },
                )
            )
        except Exception:  # auditoria jamais deve quebrar a resposta HTTP
            logger.exception("falha ao registrar tentativa negada", extra={"component": "api"})

    def require_scope(scope: str):
        async def dependency(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> None:
            if not auth.verify(_bearer(authorization)):
                await audit_denial(request, "credencial ausente ou inválida")
                raise HTTPException(
                    status_code=401,
                    detail="token local ausente ou inválido",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if not auth.allows(scope):
                reason = auth.denial_reason(scope)
                await audit_denial(request, reason)
                raise HTTPException(status_code=403, detail=reason)

        return dependency

    read = Depends(require_scope("read"))
    write = Depends(require_scope("write"))
    execute = Depends(require_scope("execute"))

    async def maintenance() -> None:
        """Mantém heartbeat e retenção dentro do processo da API."""
        try:
            await serve(runtime, interval=HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            raise

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if hub is not None:
            hub.start()
        await runtime.recorder.record(
            Event(
                kind="system.api.started",
                source=SOURCE,
                payload={
                    "version": __version__,
                    "autonomy_level": auth.autonomy_level,
                    "token_source": auth.source,
                    "host": runtime.config.api.host,
                    "port": runtime.config.api.port,
                },
                dedup=False,
            )
        )
        worker = asyncio.create_task(maintenance(), name="nexus-maintenance")
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            sessions = len(hub.snapshot()) if hub is not None else 0
            if hub is not None:
                await hub.stop()
            await runtime.recorder.record(
                Event(
                    kind="system.api.stopped",
                    source=SOURCE,
                    payload={"sessions_closed": sessions},
                    dedup=False,
                )
            )
            await runtime.llm_gateway.aclose()

    app = FastAPI(
        title="NEXUS",
        version=__version__,
        description="API local do NEXUS.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.hub = hub

    web_root = Path(__file__).resolve().parent.parent / "web"
    app.mount("/assets", StaticFiles(directory=web_root), name="assets")

    @app.get("/", include_in_schema=False)
    async def web_dashboard() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    # -- liveness ------------------------------------------------------------
    @app.get("/health")
    async def get_health() -> dict[str, Any]:
        """Sinal de vida barato: não consulta o banco nem exige credencial."""
        uptime = (datetime.now(timezone.utc) - runtime.started_at).total_seconds()
        return {
            "status": "ok",
            "name": "NEXUS",
            "version": __version__,
            "uptime_s": round(uptime, 3),
            "generated_at": _now(),
        }

    # -- estado e configuração ----------------------------------------------
    @app.get(f"{API_PREFIX}/status", dependencies=[read])
    async def get_status() -> dict[str, Any]:
        data = health(runtime)
        data["api"] = {
            "host": runtime.config.api.host,
            "port": runtime.config.api.port,
            "token_source": auth.source,
            "autonomy_level": auth.autonomy_level,
        }
        if hub is not None:
            data["realtime"] = hub.stats
        return data

    @app.get(f"{API_PREFIX}/config", dependencies=[read])
    async def get_config() -> dict[str, Any]:
        return runtime.config.as_dict()

    @app.get(f"{API_PREFIX}/logs", dependencies=[read])
    async def get_logs(lines: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        path = runtime.config.paths.log_file
        if not path.is_file():
            return {"items": [], "count": 0}
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 1_000_000))
            content = handle.read().decode("utf-8", errors="replace").splitlines()
        items = content[-lines:]
        return {"items": items, "count": len(items)}

    # -- eventos -------------------------------------------------------------
    @app.get(f"{API_PREFIX}/events", dependencies=[read])
    async def list_events(
        kind: str | None = None,
        source: str | None = None,
        severity: str | None = Query(default=None),
        since: str | None = None,
        until: str | None = None,
        correlation_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        if severity is not None and severity.lower() not in SEVERITIES:
            raise HTTPException(status_code=422, detail=f"severity inválida: {severity!r}")
        try:
            parsed_since = parse_timestamp(since, "since") if since else None
            parsed_until = parse_timestamp(until, "until") if until else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        effective_limit = min(limit, runtime.config.events.query_limit)
        filters: dict[str, Any] = {
            "kind": kind,
            "source": source,
            "severity": severity.lower() if severity else None,
            "since": parsed_since,
            "until": parsed_until,
            "correlation_id": correlation_id,
        }
        items = runtime.store.query(
            **filters, limit=effective_limit, offset=offset, oldest_first=oldest_first
        )
        return {
            "items": [event.to_dict() for event in items],
            "count": len(items),
            "total": runtime.store.count(**filters),
            "limit": effective_limit,
            "offset": offset,
            "oldest_first": oldest_first,
        }

    @app.get(f"{API_PREFIX}/events/{{event_id}}", dependencies=[read])
    async def get_event(event_id: str) -> dict[str, Any]:
        event = runtime.store.get(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="evento não encontrado")
        return event.to_dict()

    @app.post(f"{API_PREFIX}/events", status_code=201, dependencies=[write])
    async def create_event(body: EventIn) -> dict[str, Any]:
        try:
            event = Event(
                kind=body.kind,
                source=body.source,
                payload=body.payload,
                severity=Severity(body.severity),
                correlation_id=body.correlation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stored = await runtime.recorder.record(event)
        return stored.to_dict()

    @app.get(f"{API_PREFIX}/memories", dependencies=[read])
    async def list_memories(
        kind: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        include_expired: bool = False,
    ) -> dict[str, Any]:
        try:
            records = runtime.memory.list(
                kind=kind, limit=limit, offset=offset, include_expired=include_expired
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"items": [record.as_dict() for record in records], "count": len(records)}

    @app.get(f"{API_PREFIX}/memories/search", dependencies=[read])
    async def search_memories(
        q: str = Query(min_length=1, max_length=4096),
        kind: str | None = None,
        limit: int = Query(default=10, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            records = runtime.memory.search(q, kind=kind, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"items": [record.as_dict() for record in records], "count": len(records)}

    @app.post(f"{API_PREFIX}/memories", status_code=201, dependencies=[write])
    async def create_memory(body: MemoryIn) -> dict[str, Any]:
        try:
            record = runtime.memory.create(
                kind=body.kind,
                title=body.title,
                content=body.content,
                metadata=body.metadata,
                source_event_id=body.source_event_id,
                expires_at=body.expires_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await runtime.recorder.record(
            Event(
                kind="memory.created",
                source=SOURCE,
                payload={"memory_id": record.memory_id, "kind": record.kind},
                dedup=False,
            )
        )
        return record.as_dict()

    @app.delete(f"{API_PREFIX}/memories/{{memory_id}}", dependencies=[write])
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        record = runtime.memory.get(memory_id)
        if record is None:
            raise HTTPException(status_code=404, detail="memória não encontrada")
        runtime.memory.delete(memory_id)
        await runtime.recorder.record(
            Event(
                kind="memory.deleted",
                source=SOURCE,
                payload={"memory_id": record.memory_id, "kind": record.kind},
                dedup=False,
            )
        )
        return {"deleted": True, "memory_id": memory_id}

    # -- telemetria ----------------------------------------------------------
    @app.get(f"{API_PREFIX}/kinds", dependencies=[read])
    async def list_kinds(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        return {"items": runtime.store.list_kinds(limit=limit), "generated_at": _now()}

    @app.get(f"{API_PREFIX}/stats", dependencies=[read])
    async def get_stats() -> dict[str, Any]:
        return {
            "store": runtime.store.stats(),
            "bus": runtime.bus.stats.as_dict(),
            "realtime": hub.stats if hub is not None else None,
            "uptime_s": round(
                (datetime.now(timezone.utc) - runtime.started_at).total_seconds(), 3
            ),
            "generated_at": _now(),
        }

    @app.get(f"{API_PREFIX}/sessions", dependencies=[read])
    async def list_sessions() -> dict[str, Any]:
        sessions = hub.snapshot() if hub is not None else []
        return {"items": sessions, "count": len(sessions), "generated_at": _now()}

    @app.get(f"{API_PREFIX}/models", dependencies=[read])
    async def list_models() -> dict[str, Any]:
        return await runtime.llm_gateway.models()

    @app.post(f"{API_PREFIX}/llm/completions", dependencies=[write])
    async def complete(body: CompletionIn) -> dict[str, Any]:
        try:
            result = await runtime.llm_gateway.complete(
                body.prompt,
                task=body.task,
                system=body.system,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                json_mode=body.json_mode,
                provider_name=body.provider,
                model_name=body.model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except GatewayError as exc:
            await runtime.recorder.record(
                Event(
                    kind="llm.completion.failed",
                    source="llm_gateway",
                    severity=Severity.WARNING,
                    payload={"task": body.task, "error": str(exc)[:512]},
                    dedup=False,
                )
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        result_data = result.as_dict()
        await runtime.recorder.record(
            Event(
                kind="llm.completion.completed",
                source="llm_gateway",
                payload={key: value for key, value in result_data.items() if key != "text"},
                dedup=False,
            )
        )
        return result_data

    @app.post(f"{API_PREFIX}/tasks", dependencies=[write])
    async def run_task(body: TaskIn) -> dict[str, Any]:
        try:
            result = await runtime.cognitive.run(
                body.prompt,
                task=body.task,
                system=body.system,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                json_mode=body.json_mode,
                correlation_id=body.correlation_id,
                provider_name=body.provider,
                model_name=body.model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ApprovalRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "approval_required",
                    "request_id": exc.request_id,
                    "justification": exc.justification,
                    "task_id": exc.task_id,
                },
            ) from exc
        except GatewayError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="task excedeu o tempo limite") from exc
        return result.as_dict()

    @app.get(f"{API_PREFIX}/automation/actions", dependencies=[read])
    async def list_actions() -> dict[str, Any]:
        return {"items": runtime.automation.actions()}

    @app.post(f"{API_PREFIX}/automation/actions/{{action_id}}", dependencies=[execute])
    async def run_action(action_id: str, body: ActionRunIn | None = None) -> dict[str, Any]:
        approval_id = body.approval_id if body else None
        try:
            result = await runtime.automation.run(action_id, approval_id=approval_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "approval_required",
                    "request_id": exc.request_id,
                    "justification": exc.justification,
                    "task_id": exc.task_id,
                },
            ) from exc
        except AutomationDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.as_dict()

    # -- aprovações HITL ------------------------------------------------------
    @app.get(f"{API_PREFIX}/approvals/pending", dependencies=[read])
    async def list_pending_approvals() -> dict[str, Any]:
        api = ApprovalAPI(runtime.approvals)
        pending = api.list_pending()
        return {"items": pending, "count": len(pending)}

    @app.get(f"{API_PREFIX}/approvals/{{request_id}}", dependencies=[read])
    async def get_approval(request_id: str) -> dict[str, Any]:
        api = ApprovalAPI(runtime.approvals)
        details = api.get_details(request_id)
        if details is None:
            raise HTTPException(status_code=404, detail="solicitação de aprovação não encontrada")
        return details

    @app.post(f"{API_PREFIX}/approvals/{{request_id}}/decide", dependencies=[write])
    async def decide_approval(request_id: str, body: ApprovalDecisionIn) -> dict[str, Any]:
        api = ApprovalAPI(runtime.approvals)
        user = APIUser(user_id="api-client", name="cliente da API", role="OPERATOR")

        if body.decision == "approve":
            ok = api.approve(request_id, user)
            action = "aprovada"
        elif body.decision == "deny":
            ok = api.deny(request_id, user)
            action = "negada"
        else:  # revoke
            ok = api.revoke(request_id, user)
            action = "revogada"

        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"solicitação {request_id} não encontrada ou já decidida",
            )
        await runtime.recorder.record(
            Event(
                kind="approval.decided",
                source="api",
                severity=Severity.INFO,
                payload={"request_id": request_id, "decision": body.decision, "decided_by": user.user_id},
                dedup=False,
            )
        )
        return {"request_id": request_id, "decision": body.decision, "status": action}

    @app.get(f"{API_PREFIX}/approvals/history", dependencies=[read])
    async def approval_history(
        limit: int = Query(default=100, ge=1, le=1000),
        status: str | None = None,
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        api = ApprovalAPI(runtime.approvals)
        items = api.get_history(limit=limit, status=status, offset=offset)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    # -- agentes (Fase 4) ---------------------------------------------------
    @app.get(f"{API_PREFIX}/agents", dependencies=[read])
    async def list_agents() -> dict[str, Any]:
        registry = runtime.agent_registry
        if registry is None:
            return {"items": [], "count": 0}
        return {"items": registry.list_dict(), "count": len(registry.list_dict())}

    @app.get(f"{API_PREFIX}/agents/{{agent_id}}", dependencies=[read])
    async def get_agent(agent_id: str) -> dict[str, Any]:
        registry = runtime.agent_registry
        if registry is None:
            raise HTTPException(status_code=404, detail="agent registry not available")
        manifest = registry.get_manifest(agent_id)
        state = registry.get_state(agent_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return {
            "manifest": manifest.as_dict(),
            "state": state.name if state else None,
        }

    # -- simulações (Fase 5) ------------------------------------------------
    @app.get(f"{API_PREFIX}/simulations/worlds", dependencies=[read])
    async def list_worlds() -> dict[str, Any]:
        registry = runtime.world_registry
        if registry is None:
            return {"items": [], "count": 0}
        return {"items": registry.list_dict(), "count": len(registry.list_dict())}

    @app.post(f"{API_PREFIX}/simulations/worlds", dependencies=[write])
    async def create_world(body: dict[str, Any]) -> dict[str, Any]:
        registry = runtime.world_registry
        if registry is None:
            raise HTTPException(status_code=404, detail="world registry not available")
        try:
            builder = WorldBuilder(
                world_id=body.get("world_id"),
                name=body.get("name", "Untitled World"),
            )
            paradigm = body.get("paradigm", "agent_based")
            try:
                builder.paradigm(SimulationParadigm[paradigm.upper()])
            except KeyError:
                raise HTTPException(status_code=422, detail=f"paradigm inválido: {paradigm}")
            if "initial_state" in body:
                builder.initial_state(body["initial_state"])
            for ent in body.get("entities", []):
                builder.add_entity(
                    ent["entity_id"],
                    ent["entity_type"],
                    ent["name"],
                    state=ent.get("state", {}),
                    properties=ent.get("properties", {}),
                )
            definition = builder.build()
            registry.register(definition)
            return definition.as_dict()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(f"{API_PREFIX}/simulations/scenarios", dependencies=[read])
    async def list_scenarios() -> dict[str, Any]:
        mgr = runtime.scenario_manager
        if mgr is None:
            return {"items": [], "count": 0}
        return {
            "items": [s.as_dict() for s in mgr.list_scenarios()],
            "count": len(mgr.list_scenarios()),
        }

    @app.post(f"{API_PREFIX}/simulations/scenarios", dependencies=[write])
    async def create_scenario(body: dict[str, Any]) -> dict[str, Any]:
        mgr = runtime.scenario_manager
        if mgr is None:
            raise HTTPException(status_code=404, detail="scenario manager not available")
        try:
            params = SimulationParameters(
                paradigm=SimulationParadigm[body.get("paradigm", "AGENT_BASED")],
                random_seed=body.get("random_seed"),
                max_steps=body.get("max_steps"),
            )
            scenario = mgr.create_scenario(
                name=body.get("name", "Untitled Scenario"),
                world_id=body.get("world_id", "default"),
                description=body.get("description", ""),
                parameters=params,
                is_baseline=body.get("is_baseline", False),
            )
            return scenario.as_dict()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- tempo real ----------------------------------------------------------
    @app.websocket(f"{API_PREFIX}/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await _reject_socket(websocket, 4401, "autenticação não recebida no prazo")
            return

        if not auth.verify(_ws_token(raw)):
            await audit_denial(websocket, "autenticação WebSocket inválida")
            await _reject_socket(websocket, 4401, "token local ausente ou inválido")
            return

        if hub is None:
            await _reject_socket(websocket, 1011, "tempo real indisponível")
            return

        patterns = [
            item.strip()
            for item in (websocket.query_params.get("kinds") or "").split(",")
            if item.strip()
        ]
        replay_query = websocket.query_params.get("replay", "0") or "0"
        try:
            replay = max(0, min(int(replay_query), WS_REPLAY_MAX))
        except ValueError:
            replay = 0

        client = hub.register(patterns)
        if client is None:
            await _reject_socket(websocket, 1013, "limite de sessões de tempo real atingido")
            return

        try:
            if replay:
                # O store retorna primeiro os eventos mais novos; reverta a
                # página limitada para reproduzi-la em ordem cronológica.
                history = runtime.store.query(limit=replay)
                for event in reversed(history):
                    if client.matches(event.kind):
                        await websocket.send_json({"type": "event", "event": event.to_dict()})
            await websocket.send_json(
                {
                    "type": "auth.ok",
                    "client_id": client.id,
                    "patterns": patterns,
                    "replay": replay,
                    "ping_interval_s": WS_PING_INTERVAL,
                }
            )
            sender = asyncio.create_task(_stream(websocket, client))
            reader = asyncio.create_task(_drain(websocket))
            done, pending = await asyncio.wait(
                {sender, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None and not isinstance(error, WebSocketDisconnect):
                    logger.warning(
                        "sessão de tempo real encerrada com erro",
                        extra={"component": "api", "client_id": client.id, "error": str(error)},
                    )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(client)

    return app


async def _stream(websocket: WebSocket, client: HubClient) -> None:
    """Envia eventos do cliente e pings periódicos de liveness."""
    while True:
        try:
            frame = await asyncio.wait_for(client.queue.get(), timeout=WS_PING_INTERVAL)
        except asyncio.TimeoutError:
            await websocket.send_json({"type": "ping", "ts": _now()})
            continue
        await websocket.send_json(frame)
        if client.dropped:
            dropped, client.dropped = client.dropped, 0
            await websocket.send_json({"type": "events.dropped", "count": dropped})


async def _drain(websocket: WebSocket) -> None:
    """Consome frames do cliente apenas para detectar desconexão."""
    while True:
        await websocket.receive_text()
