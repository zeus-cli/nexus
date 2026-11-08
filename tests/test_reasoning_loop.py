from unittest.mock import MagicMock

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from nexus_core.cognitive import CognitiveCore, TaskResult
from nexus_core.llm_router.gateway import CompletionResult, LLMGateway
from nexus_core.memory.records import MemoryStore
from nexus_core.recorder import EventRecorder
from nexus_core.automation import AutomationEngine
from nexus_core.reflection import ReflectionDecision, ReflectionResult

@dataclass
class MockCompletion:
    text: str
    provider: str = "mock"
    model: str = "mock-model"
    latency_ms: int = 10
    prompt_tokens: int = 10
    completion_tokens: int = 10
    estimated_cost_usd: float = 0.0

    def as_dict(self):
        return {"text": self.text}

class MockGateway(LLMGateway):
    def __init__(self):
        self.call_count = 0
        self.results = []

    async def complete(self, prompt, **kwargs):
        self.call_count += 1
        task_name = kwargs.get("task", "reasoning")
        if "planning" in task_name:
            res = MockCompletion(text='{"steps": [{"type": "automation", "description": "Check uptime", "action_id": "sys.uptime"}, {"type": "reason", "description": "Analyze uptime"}]}')
        elif "synthesis" in task_name:
            res = MockCompletion(text="The system is healthy and has been up for 10 minutes.")
        else:
            res = MockCompletion(text="Generic response")
        
        return CompletionResult(
            text=res.text, provider=res.provider, model=res.model,
            latency_ms=res.latency_ms, prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens, estimated_cost_usd=res.estimated_cost_usd,
            task=task_name, attempts=1, context_truncated=False
        )

    async def aclose(self): pass
    async def models(self): return []
    def usage(self): return {}

async def test_reasoning_loop():
    # Setup
    mock_gateway = MockGateway()
    mock_memory = MagicMock(spec=MemoryStore)
    mock_memory.search.return_value = []
    mock_memory.list.return_value = []
    mock_recorder = AsyncMock(spec=EventRecorder)
    mock_automation = AsyncMock(spec=AutomationEngine)
    # Mock ActionResult to have required fields
    mock_action_res = MagicMock()
    mock_action_res.exit_code = 0
    mock_action_res.stdout = "up 10 mins"
    mock_action_res.stderr = ""
    mock_action_res.duration_ms = 10
    mock_action_res.truncated = False
    mock_automation.run.return_value = mock_action_res

    mock_reflection = AsyncMock()
    mock_reflection.critique.return_value = ReflectionResult(
        ReflectionDecision.SUCCESS, "ok"
    )
    mock_task_store = MagicMock()
    core = CognitiveCore(
        mock_gateway,
        mock_memory,
        mock_recorder,
        mock_automation,
        mock_reflection,
        mock_task_store,
    )
    
    print("Running reasoning loop test...")
    result = await core.run("Check system status")
    
    print(f"Plan generated: {len(result.plan)} steps")
    print(f"Steps executed: {len(result.step_results)}")
    print(f"Final answer: {result.final_completion.text}")
    
    assert len(result.plan) == 2
    assert result.step_results[0].step.type == "automation"
    assert "up 10 mins" in result.step_results[0].output
    assert "healthy" in result.final_completion.text
    print("Test PASSED")

if __name__ == "__main__":
    asyncio.run(test_reasoning_loop())
