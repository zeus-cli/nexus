
import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from nexus_core.app import bootstrap, NexusRuntime
from nexus_core.automation_types import RiskLevel, AccessLevel, ExecutionMode, Tool
from nexus_core.automation import ApprovalRequired
from nexus_core.approval import ApprovalManager, ApprovalRequest, ApprovalDecision
from nexus_core.approval_api import ApprovalAPI, APIUser
from nexus_core.cognitive import PlanStep
from nexus_core.llm_router.gateway import CompletionResult
from nexus_core.reflection import ReflectionDecision, ReflectionResult

@pytest.mark.asyncio
async def test_full_hitl_cognitive_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        config_dir = root / "config"
        config_dir.mkdir()
        config_file = config_dir / "nexus.toml"
        config_text = (
            "[paths]\n"
            "data_dir = \"data\"\n"
            "database = \"data/nexus_events.db\"\n"
            "log_file = \"data/nexus.log\"\n\n"
            "[safety]\n"
            "autonomy_level = 1\n\n"
            "[events]\n"
            "dedup_window_hours = 24\n"
            "max_payload_bytes = 16384\n"
            "retention_days = 7\n"
            "query_limit = 100\n\n"
            "[llm]\n"
            "max_context_tokens = 4096\n"
            "failure_threshold = 3\n"
            "cooldown_s = 60\n"
            "max_retries = 2\n"
            "providers = []\n"
            "chain = []\n\n"
            "[logging]\n"
            "level = \"INFO\"\n"
        )
        config_file.write_text(config_text)
        (root / "data").mkdir()
        
        runtime = bootstrap(config_path=config_file)
        
        tool_id = "sys.dangerous_op"
        runtime.automation._tools[tool_id] = Tool(
            id=tool_id, version="1.0", description="Dangerous Op", 
            risk=RiskLevel.HIGH, access=AccessLevel.WRITE, 
            argv_template=("/usr/bin/uptime",)
        )
        
        runtime.cognitive._generate_plan = AsyncMock(return_value=(
            PlanStep(type="automation", description="Run dangerous op", action_id=tool_id),
        ))
        runtime.cognitive._reflection.critique = AsyncMock(
            return_value=ReflectionResult(ReflectionDecision.SUCCESS, "ok")
        )

        async def fake_complete(prompt, *, task="reasoning", **kwargs):
            return CompletionResult(
                text="final answer",
                provider="fake",
                model="fake",
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
                task=task,
                attempts=1,
                context_truncated=False,
            )

        runtime.cognitive._gateway.complete = fake_complete

        try:
            await runtime.cognitive.run("Perform dangerous op")
            pytest.fail("Should have raised ApprovalRequired")
        except ApprovalRequired as ae:
            rid = ae.request_id

        kinds = [event.kind for event in runtime.store.query(limit=200)]
        assert "agent.task.suspended" in kinds
        assert "agent.task.failed" not in kinds
            
        api = ApprovalAPI(runtime.automation._approval_manager)
        details = api.get_details(rid)
        assert details is not None
        assert details["tool_id"] == tool_id
        
        user = APIUser("admin", "Admin User", "SUPERUSER")
        assert api.approve(rid, user) is True
        
        # Find the task_id associated with the request
        # We can find it by looking at the ApprovalManager's database
        req_data = runtime.automation._approval_manager.get_request(rid)
        task_id = req_data["task_id"]
        
        result = await runtime.cognitive.resume(task_id, approval_id=rid)
        
        assert result.final_completion is not None
        
        history = api.get_history()
        assert any(h["id"] == rid for h in history)
        
        await runtime.aclose()

if __name__ == "__main__":
    import pytest
    pytest.main([__file__])