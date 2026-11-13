
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone
from dataclasses import dataclass

from nexus_core.cognitive import CognitiveCore, PlanStep, StepResult, TaskResult
from nexus_core.reflection import ReflectionEngine, ReflectionDecision, ReflectionResult
from nexus_core.llm_router.gateway import CompletionResult
from nexus_core.events import Event
from nexus_core.automation import ApprovalRequired

# Mocks
class MockGateway:
    def __init__(self):
        self.responses = []
        self.call_count = 0
        self.delay = 0

    async def complete(self, prompt, task=None, system=None, json_mode=False, provider_name=None, model_name=None):
        self.call_count += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.responses:
            return self.responses.pop(0)
        return CompletionResult(
            text="{}", provider="mock", model="mock", task="reasoning", 
            latency_ms=10, prompt_tokens=1, completion_tokens=1, estimated_cost_usd=0.0,
            attempts=1, context_truncated=False
        )

class MockMemory:
    def search(self, query, limit=4):
        return []

    def list(self, *, limit=4, **kwargs):
        return []

class MockRecorder:
    def __init__(self):
        self.events = []
    async def record(self, event):
        self.events.append(event)

class MockAutomation:
    def __init__(self):
        self.action_results = {}
        self.approval_requests = []
    async def run(self, action_id, **kwargs):
        from nexus_core.automation import ActionResult
        res = self.action_results.get(action_id, ActionResult(exit_code=0, stdout="Success", stderr=""))
        return res
    def create_approval_request(self, req):
        from nexus_core.approval import ApprovalRequest
        self.approval_requests.append(req)
        return "req-" + req.task_id[:8]

@pytest.fixture
def setup_core():
    gateway = MockGateway()
    memory = MockMemory()
    recorder = MockRecorder()
    automation = MockAutomation()
    reflection_engine = ReflectionEngine(gateway)
    
    core = CognitiveCore(
        gateway=gateway,
        memory=memory,
        recorder=recorder,
        automation=automation,
        reflection=reflection_engine, task_store=MagicMock(),
        timeout_s=1.0,
        max_attempts=3
    )
    return core, gateway, memory, recorder, automation

def make_completion(text, task="reasoning"):
    return CompletionResult(
        text=text, provider="mock", model="mock", task=task, 
        latency_ms=1, prompt_tokens=1, completion_tokens=1, estimated_cost_usd=0,
        attempts=1, context_truncated=False
    )

@pytest.mark.asyncio
async def test_reflection_uses_task_accepted_by_gateway():
    """O gateway real rejeita tarefas fora de LLM_TASKS; a crítica deve usar uma válida."""
    from nexus_core.config import LLM_TASKS

    class StrictGateway:
        def __init__(self):
            self.tasks = []

        async def complete(self, prompt, *, task="reasoning", system=None, json_mode=False):
            assert task in LLM_TASKS, f"tarefa inválida: {task!r}"
            self.tasks.append(task)
            return make_completion('{"decision": "SUCCESS", "justification": "ok", "suggested_action": null}')

    gateway = StrictGateway()
    result = await ReflectionEngine(gateway).critique("goal", [], [])

    assert result.decision is ReflectionDecision.SUCCESS
    assert gateway.tasks == ["reasoning"]


@pytest.mark.asyncio
async def test_execution_success(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Plan it", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "SUCCESS", "justification": "Goal met", "suggested_action": null}', "critique"))
    gateway.responses.append(make_completion("Final answer: Success", "synthesis"))
    
    result = await core.run("Do something")
    assert result.final_completion.text == "Final answer: Success"

@pytest.mark.asyncio
async def test_recoverable_failure_retry(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Attempt 1", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "RETRY", "justification": "Retry safe op", "suggested_action": "redo"}', "critique"))
    gateway.responses.append(make_completion('{"decision": "SUCCESS", "justification": "Now it works", "suggested_action": null}', "critique"))
    gateway.responses.append(make_completion("Final answer: Recovered", "synthesis"))
    
    result = await core.run("Do something")
    assert result.final_completion.text == "Final answer: Recovered"

@pytest.mark.asyncio
async def test_irrecoverable_failure_abort(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Do it", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "ABORT", "justification": "Impossible", "suggested_action": null}', "critique"))
    
    with pytest.raises(RuntimeError, match="Task aborted by reflection"):
        await core.run("Do impossible")

@pytest.mark.asyncio
async def test_replanning(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Wrong way", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "REPLAN", "justification": "Strategy wrong", "suggested_action": "new plan"}', "critique"))
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Right way", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "SUCCESS", "justification": "Goal met", "suggested_action": null}', "critique"))
    gateway.responses.append(make_completion("Final answer: Replanned success", "synthesis"))
    
    result = await core.run("Do something")
    assert result.final_completion.text == "Final answer: Replanned success"

@pytest.mark.asyncio
async def test_attempt_limit(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Try", "query": null, "action_id": null}]}', "planning"))
    
    for _ in range(3):
        gateway.responses.append(make_completion('{"decision": "REPLAN", "justification": "fail", "suggested_action": null}', "critique"))
        gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Retry", "query": null, "action_id": null}]}', "planning"))
    
    gateway.responses.append(make_completion("Final answer: gave up", "synthesis"))
    
    result = await core.run("Infinite fail")
    assert result.final_completion.text == "Final answer: gave up"

@pytest.mark.asyncio
async def test_timeout(setup_core):
    core, gateway, _, _, _ = setup_core
    core._timeout_s = 0.01 
    
    async def slow_complete(*args, **kwargs):
        await asyncio.sleep(0.1)
        return make_completion("{}", "reasoning")
    
    gateway.complete = slow_complete
    
    with pytest.raises(asyncio.TimeoutError):
        await core.run("Slow task")

@pytest.mark.asyncio
async def test_cancellation(setup_core):
    core, gateway, _, _, _ = setup_core
    gateway.delay = 0.1 # Ensure the task is actually awaiting
    
    task = asyncio.create_task(core.run("Long task"))
    await asyncio.sleep(0.02)
    task.cancel()
    
    with pytest.raises(asyncio.CancelledError):
        await task

@pytest.mark.asyncio
async def test_approval_request(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Dangerous op", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "REQUEST_APPROVAL", "justification": "Sensitive", "suggested_action": null}', "critique"))
    
    with pytest.raises(ApprovalRequired, match="Reflexão exige aprovação"):
        await core.run("Do dangerous")

@pytest.mark.asyncio
async def test_false_success(setup_core):
    core, gateway, _, _, _ = setup_core
    
    gateway.responses.append(make_completion('{"steps": [{"type": "diagnostic", "description": "Check files", "query": null, "action_id": "sys.files"}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "REPLAN", "justification": "Exit 0 but no data found", "suggested_action": "search deeper"}', "critique"))
    gateway.responses.append(make_completion('{"steps": [{"type": "reason", "description": "Deeper search", "query": null, "action_id": null}]}', "planning"))
    gateway.responses.append(make_completion('{"decision": "SUCCESS", "justification": "Found data", "suggested_action": null}', "critique"))
    gateway.responses.append(make_completion("Final answer: Validated success", "synthesis"))
    
    result = await core.run("Check files")
    assert result.final_completion.text == "Final answer: Validated success"
