from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from nexus_core.app import bootstrap

try:  # dependências da Fase 2 podem não estar instaladas no Python do sistema
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from nexus_core.api.app import create_app
    from nexus_core.api.auth import Authenticator, resolve_token
    from nexus_core.api.realtime import RealtimeHub
    from nexus_core.llm_router.gateway import CompletionResult, GatewayError

    API_AVAILABLE = True
except ImportError:  # pragma: no cover
    API_AVAILABLE = False

CONFIG_TEMPLATE = """
[paths]
data_dir = "data"

[logging]
level = "WARNING"
json = true

[events]
dedup_window_hours = 0
query_limit = 500

[api]
port = 8765

[safety]
autonomy_level = {level}
"""


@unittest.skipUnless(API_AVAILABLE, "fastapi/httpx ausentes — rode com .venv/bin/python")
class ApiTestCase(unittest.TestCase):
    autonomy_level = 1

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "config").mkdir(parents=True)
        self.config_path = root / "config" / "nexus.toml"
        self.config_path.write_text(
            textwrap.dedent(CONFIG_TEMPLATE.format(level=self.autonomy_level)), encoding="utf-8"
        )

        self.runtime = bootstrap(self.config_path, console_logs=False)
        self.addCleanup(self.runtime.close)

        info = resolve_token(self.runtime.config)
        self.token = info.token
        self.auth = Authenticator(self.token, self.autonomy_level, source=info.source)
        self.hub = RealtimeHub(self.runtime.bus)
        self.app = create_app(self.runtime, auth=self.auth, hub=self.hub)

        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()
        self.addCleanup(self._client_cm.__exit__, None, None, None)
        self.headers = {"Authorization": f"Bearer {self.token}"}


class AuthenticationTest(ApiTestCase):
    def test_static_dashboard_shell_is_served(self) -> None:
        page = self.client.get("/")
        asset = self.client.get("/assets/app.js")
        self.assertEqual(page.status_code, 200)
        self.assertIn("NEXUS", page.text)
        self.assertEqual(asset.status_code, 200)
        self.assertIn("sessionStorage", asset.text)

    def test_health_is_open(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_status_requires_token(self) -> None:
        response = self.client.get("/api/v1/status")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers.get("www-authenticate", ""))

    def test_status_rejects_wrong_token(self) -> None:
        response = self.client.get(
            "/api/v1/status", headers={"Authorization": "Bearer token-errado"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.runtime.store.count(kind="security.api.denied"), 1)

    def test_status_with_token(self) -> None:
        response = self.client.get("/api/v1/status", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["api"]["autonomy_level"], self.autonomy_level)
        self.assertEqual(body["database"]["integrity"], "ok")

    def test_config_never_exposes_token(self) -> None:
        response = self.client.get("/api/v1/config", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.token, json.dumps(response.json()))


class EventsApiTest(ApiTestCase):
    def test_llm_models_and_completion_routes(self) -> None:
        class StubGateway:
            def usage(self):
                return {}

            async def models(self):
                return {"providers": [], "usage": {}}

            async def complete(self, prompt: str, **kwargs):
                return CompletionResult(
                    text="answer",
                    model="fake",
                    provider="test",
                    task=kwargs["task"],
                    latency_ms=2,
                    prompt_tokens=2,
                    completion_tokens=1,
                    estimated_cost_usd=0.0,
                    attempts=1,
                    context_truncated=False,
                )

            async def aclose(self):
                return None

        self.runtime.llm_gateway = StubGateway()
        models = self.client.get("/api/v1/models", headers=self.headers)
        self.assertEqual(models.status_code, 200)

        response = self.client.post(
            "/api/v1/llm/completions",
            headers=self.headers,
            json={"task": "summary", "prompt": "private input"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "answer")
        event = self.runtime.store.query(kind="llm.completion.completed")[-1]
        self.assertNotIn("text", event.payload)
        self.assertNotIn("prompt", event.payload)

    def test_llm_failure_is_audited_without_prompt(self) -> None:
        class FailingGateway:
            def usage(self):
                return {}

            async def models(self):
                return {"providers": [], "usage": {}}

            async def complete(self, prompt: str, **kwargs):
                raise GatewayError("local provider unavailable")

            async def aclose(self):
                return None

        self.runtime.llm_gateway = FailingGateway()
        response = self.client.post(
            "/api/v1/llm/completions",
            headers=self.headers,
            json={"prompt": "private input"},
        )
        self.assertEqual(response.status_code, 503)
        event = self.runtime.store.query(kind="llm.completion.failed")[-1]
        self.assertNotIn("private input", str(dict(event.payload)))

    def test_memory_create_search_and_explicit_delete(self) -> None:
        created = self.client.post(
            "/api/v1/memories",
            headers=self.headers,
            json={
                "kind": "long_term",
                "title": "Language preference",
                "content": "Use Portuguese for summaries.",
                "metadata": {"source": "user"},
            },
        )
        self.assertEqual(created.status_code, 201)
        memory_id = created.json()["memory_id"]

        search = self.client.get(
            "/api/v1/memories/search", headers=self.headers, params={"q": "Portuguese"}
        )
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["items"][0]["memory_id"], memory_id)

        deletion = self.client.delete(f"/api/v1/memories/{memory_id}", headers=self.headers)
        self.assertEqual(deletion.status_code, 200)
        self.assertTrue(deletion.json()["deleted"])
        self.assertEqual(self.runtime.memory.search("Portuguese"), [])
        created_event = self.runtime.store.query(kind="memory.created")[-1]
        self.assertNotIn("content", created_event.payload)

    def test_task_route_uses_cognitive_core(self) -> None:
        class StubTaskResult:
            def as_dict(self):
                return {"task_id": "task-1", "completion": {"text": "result"}}

        class StubCore:
            async def run(self, prompt: str, **kwargs):
                self.prompt = prompt
                self.kwargs = kwargs
                return StubTaskResult()

        self.runtime.cognitive = StubCore()
        response = self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={"task": "summary", "prompt": "input", "provider": "local", "model": "small"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_id"], "task-1")
        self.assertEqual(self.runtime.cognitive.kwargs["provider_name"], "local")
        self.assertEqual(self.runtime.cognitive.kwargs["model_name"], "small")

    def test_logs_route_is_authenticated_and_bounded(self) -> None:
        denied = self.client.get("/api/v1/logs")
        allowed = self.client.get("/api/v1/logs?lines=5", headers=self.headers)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertLessEqual(allowed.json()["count"], 5)

    def test_automation_requires_execute_scope(self) -> None:
        actions = self.client.get("/api/v1/automation/actions", headers=self.headers)
        self.assertEqual(actions.status_code, 200)
        # Autonomia 1 (assistência) permite leitura, mas não o escopo 'execute'.
        self.assertTrue(
            all(
                item["allowed"] == (self.autonomy_level >= item["required_autonomy"])
                for item in actions.json()["items"]
            )
        )

        response = self.client.post(
            "/api/v1/automation/actions/sys.uptime", headers=self.headers
        )
        self.assertEqual(response.status_code, 403)

    def test_post_and_read_event(self) -> None:
        created = self.client.post(
            "/api/v1/events",
            headers=self.headers,
            json={
                "kind": "agent.task.completed",
                "source": "test",
                "payload": {"task": "demo", "ok": True},
                "severity": "warning",
            },
        )
        self.assertEqual(created.status_code, 201)
        event_id = created.json()["event_id"]

        single = self.client.get(f"/api/v1/events/{event_id}", headers=self.headers)
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json()["kind"], "agent.task.completed")
        self.assertEqual(single.json()["severity"], "warning")

        listing = self.client.get(
            "/api/v1/events", headers=self.headers, params={"kind": "agent.task.completed"}
        )
        self.assertEqual(listing.status_code, 200)
        body = listing.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["payload"], {"task": "demo", "ok": True})

    def test_unknown_event_returns_404(self) -> None:
        response = self.client.get("/api/v1/events/inexistente", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_invalid_event_is_rejected(self) -> None:
        bad_kind = self.client.post(
            "/api/v1/events",
            headers=self.headers,
            json={"kind": "Agent Inválido", "payload": {}},
        )
        self.assertEqual(bad_kind.status_code, 422)

        bad_severity = self.client.post(
            "/api/v1/events",
            headers=self.headers,
            json={"kind": "agent.task", "severity": "urgente"},
        )
        self.assertEqual(bad_severity.status_code, 422)

    def test_invalid_queries_are_rejected(self) -> None:
        self.assertEqual(
            self.client.get(
                "/api/v1/events", headers=self.headers, params={"severity": "ruidoso"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/events", headers=self.headers, params={"since": "não-é-data"}
            ).status_code,
            422,
        )

    def test_limit_is_capped_by_config(self) -> None:
        response = self.client.get(
            "/api/v1/events", headers=self.headers, params={"limit": 1000}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 500)

    def test_approvals_history_route_is_not_shadowed_by_request_id(self) -> None:
        response = self.client.get("/api/v1/approvals/history", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_simulation_scenario_accepts_lowercase_paradigm(self) -> None:
        response = self.client.post(
            "/api/v1/simulations/scenarios",
            headers=self.headers,
            json={"name": "cenário", "world_id": "w-1", "paradigm": "agent_based"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["parameters"]["paradigm"], "AGENT_BASED")

    def test_telemetry_endpoints(self) -> None:
        self.client.post(
            "/api/v1/events", headers=self.headers, json={"kind": "agent.step", "payload": {"n": 1}}
        )
        kinds = self.client.get("/api/v1/kinds", headers=self.headers)
        self.assertEqual(kinds.status_code, 200)
        self.assertIn("agent.step", [item["kind"] for item in kinds.json()["items"]])

        stats = self.client.get("/api/v1/stats", headers=self.headers)
        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["store"]["total"], self.runtime.store.count())
        self.assertIn("realtime", stats.json())

        sessions = self.client.get("/api/v1/sessions", headers=self.headers)
        self.assertEqual(sessions.json()["count"], 0)


class WebSocketTest(ApiTestCase):
    def test_server_rejects_non_loopback_override(self) -> None:
        from nexus_core.api.server import run_server

        with self.assertRaisesRegex(ValueError, "loopback"):
            run_server(self.runtime, host="0.0.0.0")

    def test_requires_auth_frame(self) -> None:
        with self.client.websocket_connect("/api/v1/ws/events") as socket:
            socket.send_json({"type": "auth", "token": "token-errado"})
            frame = socket.receive_json()
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["code"], 4401)
            with self.assertRaises(WebSocketDisconnect) as ctx:
                socket.receive_json()
            self.assertEqual(ctx.exception.code, 4401)

    def test_streams_events_after_auth(self) -> None:
        with self.client.websocket_connect("/api/v1/ws/events?kinds=agent.*") as socket:
            socket.send_json({"type": "auth", "token": self.token})
            hello = socket.receive_json()
            self.assertEqual(hello["type"], "auth.ok")
            self.assertEqual(hello["patterns"], ["agent.*"])

            self.client.post(
                "/api/v1/events",
                headers=self.headers,
                json={"kind": "agent.step", "payload": {"n": 7}},
            )
            self.client.post(
                "/api/v1/events",
                headers=self.headers,
                json={"kind": "system.unrelated", "payload": {}},
            )

            frame = socket.receive_json()
            self.assertEqual(frame["type"], "event")
            self.assertEqual(frame["event"]["kind"], "agent.step")
            self.assertEqual(frame["event"]["payload"], {"n": 7})

    def test_replay_sends_latest_events_in_chronological_order(self) -> None:
        event_ids: list[str] = []
        for index in range(5):
            response = self.client.post(
                "/api/v1/events",
                headers=self.headers,
                json={"kind": "agent.replay", "payload": {"index": index}},
            )
            self.assertEqual(response.status_code, 201)
            event_ids.append(response.json()["event_id"])

        with self.client.websocket_connect(
            "/api/v1/ws/events?kinds=agent.replay&replay=3"
        ) as socket:
            socket.send_json({"type": "auth", "token": self.token})
            replayed_ids: list[str] = []
            while True:
                frame = socket.receive_json()
                if frame["type"] != "event":
                    break
                replayed_ids.append(frame["event"]["event_id"])
            self.assertEqual(frame["type"], "auth.ok")
        self.assertEqual(replayed_ids, event_ids[-3:])


class ReadOnlyAutonomyTest(ApiTestCase):
    autonomy_level = 0

    def test_write_is_denied_and_audited(self) -> None:
        response = self.client.post(
            "/api/v1/events", headers=self.headers, json={"kind": "agent.step", "payload": {}}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("autonomia nível 1", response.json()["detail"])
        self.assertEqual(self.runtime.store.count(kind="security.api.denied"), 1)

    def test_read_still_allowed(self) -> None:
        self.assertEqual(self.client.get("/api/v1/status", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/events", headers=self.headers).status_code, 200)

    def test_llm_completion_requires_write_scope(self) -> None:
        response = self.client.post(
            "/api/v1/llm/completions",
            headers=self.headers,
            json={"prompt": "question"},
        )
        self.assertEqual(response.status_code, 403)

    def test_memory_write_requires_write_scope(self) -> None:
        response = self.client.post(
            "/api/v1/memories",
            headers=self.headers,
            json={"kind": "long_term", "content": "private"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
