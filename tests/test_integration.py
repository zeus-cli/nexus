"""Testes da camada de integração do NEXUS (Fase 7 - Integração final)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus_core.agent_manifest import AgentManifest, AgentRole
from nexus_core.agent_orchestrator import AgentOrchestrator
from nexus_core.agent_registry import AgentRegistry
from nexus_core.agent_runtime import AgentRuntime
from nexus_core.cognitive import CognitiveCore
from nexus_core.decision_engine import DecisionEngine
from nexus_core.experience_consolidation import ExperienceConsolidationEngine
from nexus_core.execution_gate import ExecutionGate
from nexus_core.integration import NEXUSIntegration
from nexus_core.intent_engine import IntentEngine
from nexus_core.knowledge_engine import KnowledgeEngine
from nexus_core.planner_engine import PlannerEngine
from nexus_core.reflection import ReflectionEngine


@pytest.fixture
def integration():
    """Cria uma instância integrada de todos os componentes."""
    # Componentes base.
    gateway = MagicMock()
    memory = MagicMock()
    recorder = MagicMock(spec=MagicMock)
    automation = MagicMock()
    reflection = ReflectionEngine(gateway)
    task_store = MagicMock()

    cognitive = CognitiveCore(
        gateway, memory, recorder, automation, reflection, task_store,
        timeout_s=5.0,
    )

    intent_engine = IntentEngine()
    planner = PlannerEngine()
    decision_engine = DecisionEngine()
    agent_registry = AgentRegistry()
    agent_runtime = AgentRuntime(agent_registry)
    agent_orchestrator = AgentOrchestrator(agent_registry, agent_runtime)
    execution_gate = ExecutionGate()
    knowledge_engine = KnowledgeEngine()
    experience_engine = ExperienceConsolidationEngine()

    return NEXUSIntegration(
        cognitive=cognitive,
        intent_engine=intent_engine,
        planner=planner,
        decision_engine=decision_engine,
        agent_registry=agent_registry,
        agent_runtime=agent_runtime,
        agent_orchestrator=agent_orchestrator,
        execution_gate=execution_gate,
        knowledge_engine=knowledge_engine,
        experience_engine=experience_engine,
        reflection=reflection,
    )


def test_integration_registers_agents(integration):
    """Agentes podem ser registrados no sistema integrado."""
    integration.agent_registry.register(AgentManifest(
        agent_id="researcher-1",
        name="Researcher",
        role=AgentRole.RESEARCHER,
        capabilities=("web.search",),
    ))
    agents = integration.agent_registry.list_agents()
    assert len(agents) == 1
    assert agents[0][0].agent_id == "researcher-1"


@pytest.mark.asyncio
async def test_integration_process_intent(integration):
    """O pipeline cognitivo completo deve funcionar end-to-end."""
    result = await integration.process_intent(
        prompt="analyze system logs",
        task_id="task-1",
    )
    assert result["task_id"] == "task-1"
    assert result["intent"]["intent_type"] in ("ANALYSIS", "UNKNOWN")
    assert "plan" in result
    assert "decision" in result
    assert result["plan_id"] is not None


@pytest.mark.asyncio
async def test_integration_execute_plan_via_agents(integration):
    """Um plano pode ser executado por agentes através do orquestrador."""
    # Registra um agente com capacidade de automação.
    integration.agent_registry.register(AgentManifest(
        agent_id="engineer-1",
        name="Engineer",
        role=AgentRole.ENGINEER,
        capabilities=("automation",),
        tool_access=("sys.uptime",),
    ))
    # Registra um handler para o agente.
    async def echo_handler(task):
        return {"result": "done", "agent": task.agent_id}
    integration.agent_runtime.register_handler("engineer-1", echo_handler)

    # Cria um plano.
    plan = integration.planner.generate_plan(
        objective="test objective",
        intent_capabilities=("automation",),
    )
    integration.planner.start_plan(plan.plan_id)

    # Executa via agentes.
    result = await integration.execute_plan_via_agents(
        plan.plan_id,
        task_descriptions=[
            {"description": "test task", "input": {}, "required_capabilities": ("automation",)},
        ],
    )
    assert result["plan_id"] == plan.plan_id
    assert len(result["task_results"]) == 1


def test_integration_experience_to_improvement(integration):
    """Experiências validadas devem gerar propostas de melhoria."""
    # Adiciona uma experiência de sucesso.
    rid = integration.experience_engine.add_experience(
        experience_type=__import__("nexus_core.experience_consolidation", fromlist=["ExperienceType"]).ExperienceType.TASK_SUCCESS,
        title="uptime check",
        description="successful uptime check",
        source_node_id="local",
        confidence=0.9,
        tags=("uptime",),
    )
    integration.experience_engine.validate_experience(rid, validated_by="op")
    integration.experience_engine.reuse_experience(rid)

    # Propõe melhorias a partir da experiência.
    proposal_ids = integration.propose_improvements_from_experience(
        min_reuse_count=1,
    )
    assert len(proposal_ids) >= 1


def test_integration_system_status(integration):
    """O status integrado deve refletir todos os componentes."""
    status = integration.system_status()
    assert "agents" in status
    assert "plans" in status
    assert "experience_stats" in status
    assert "improvement_stats" in status


__all__ = []
