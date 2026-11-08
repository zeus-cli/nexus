"""Testes dos motores cognitivos da Fase 2."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from nexus_core.intent_engine import IntentEngine, IntentType, Intent, RiskLevel
from nexus_core.context_engine import ContextEngine, ContextualRepresentation
from nexus_core.reasoning_engine import ReasoningEngine, ReasoningResult
from nexus_core.decision_engine import DecisionEngine, DecisionOption, ApprovalRequirement
from nexus_core.planner_engine import PlannerEngine, Plan, PlanStep, PlanStatus, PlanStepStatus
from nexus_core.knowledge_engine import (
    KnowledgeEngine,
    KnowledgeRecord,
    KnowledgeKind,
    KnowledgeSource,
)
from nexus_core.memory.store import Database
from nexus_core.memory.records import MemoryStore


# --------------------------------------------------------------------------- #
# Intent Engine
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_intent_execution_classification():
    engine = IntentEngine()
    intent = await engine.interpret("delete the temporary file /tmp/x")
    assert intent.intent_type == IntentType.EXECUTION
    assert intent.confidence > 0
    assert "automation" in intent.required_capabilities


@pytest.mark.asyncio
async def test_intent_simulation_classification():
    engine = IntentEngine()
    intent = await engine.interpret("simulate what happens if we double the load")
    assert intent.intent_type == IntentType.SIMULATION


@pytest.mark.asyncio
async def test_intent_planning_classification():
    engine = IntentEngine()
    intent = await engine.interpret("plan a strategy to optimize system performance")
    assert intent.intent_type == IntentType.PLANNING


@pytest.mark.asyncio
async def test_intent_empty_prompt():
    engine = IntentEngine()
    intent = await engine.interpret("")
    assert intent.intent_type == IntentType.UNKNOWN
    assert intent.confidence == 0.0


# --------------------------------------------------------------------------- #
# Context Engine
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_context_engine_builds_blocks():
    engine = ContextEngine()
    intent = Intent(
        raw_prompt="check memory usage",
        intent_type=IntentType.QUERY,
        objective="check memory usage",
        confidence=0.9,
    )
    rep = await engine.build(intent, conversation="User asked about memory")
    assert isinstance(rep, ContextualRepresentation)
    assert rep.intent.intent_type == IntentType.QUERY
    assert len(rep.blocks) >= 1
    assert rep.total_chars > 0


@pytest.mark.asyncio
async def test_context_engine_with_memory(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()
    db.migrate()
    store = MemoryStore(db, vector_dir=str(tmp_path / "vectors"))
    store.create(kind="episodic", content="system uptime was high yesterday", title="uptime event")

    engine = ContextEngine(memory=store)
    intent = Intent(
        raw_prompt="uptime",
        intent_type=IntentType.QUERY,
        objective="uptime",
        confidence=0.9,
    )
    rep = await engine.build(intent)
    assert len(rep.blocks) >= 1
    db.close()


# --------------------------------------------------------------------------- #
# Reasoning Engine
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reasoning_engine_returns_structured_result():
    engine = ReasoningEngine()
    result = await engine.reason("optimize system performance", evidence=["cpu is high"])
    assert isinstance(result, ReasoningResult)
    assert result.conclusion
    assert len(result.steps) >= 6
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.asyncio
async def test_reasoning_detects_contradictions():
    engine = ReasoningEngine()
    result = await engine.reason(
        "decide",
        evidence=["server is up"],
        assumptions=["not server is up"],
    )
    assert len(result.contradictions) >= 1


# --------------------------------------------------------------------------- #
# Decision Engine
# --------------------------------------------------------------------------- #

def test_decision_engine_selects_best():
    engine = DecisionEngine()
    options = [
        DecisionOption(
            option_id="a",
            description="safe option",
            expected_outcome="ok",
            evidence_quality=0.9,
            operational_risk="low",
            resource_consumption="low",
            reversible=True,
            confidence=0.9,
        ),
        DecisionOption(
            option_id="b",
            description="risky option",
            expected_outcome="maybe",
            evidence_quality=0.4,
            operational_risk="critical",
            resource_consumption="high",
            reversible=False,
            confidence=0.3,
        ),
    ]
    result = engine.decide(options)
    assert result.recommended_option.option_id == "a"
    assert result.approval_requirement == ApprovalRequirement.NONE
    assert result.score > 0


def test_decision_engine_critical_denied():
    engine = DecisionEngine()
    options = [
        DecisionOption(
            option_id="c",
            description="critical op",
            expected_outcome="",
            evidence_quality=0.5,
            operational_risk="critical",
            resource_consumption="low",
            reversible=False,
            confidence=0.5,
        ),
    ]
    result = engine.decide(options)
    assert result.approval_requirement == ApprovalRequirement.DENIED


# --------------------------------------------------------------------------- #
# Planner Engine
# --------------------------------------------------------------------------- #

def test_planner_creates_and_starts_plan():
    engine = PlannerEngine()
    plan = engine.generate_plan("analyze system logs", intent_capabilities=("memory.retrieval", "automation"))
    assert plan.objective == "analyze system logs"
    assert len(plan.steps) >= 3
    assert plan.status == PlanStatus.DRAFT

    assert engine.start_plan(plan.plan_id) is True
    updated = engine.get_plan(plan.plan_id)
    assert updated.status == PlanStatus.ACTIVE


def test_planner_complete_and_cancel():
    engine = PlannerEngine()
    plan = engine.generate_plan("task", intent_capabilities=())
    engine.start_plan(plan.plan_id)
    first_step = plan.steps[0].step_id
    engine.complete_step(plan.plan_id, first_step, result="done")
    updated = engine.get_plan(plan.plan_id)
    step = updated.step_by_id(first_step)
    assert step.status == PlanStepStatus.COMPLETED
    assert step.result == "done"

    assert engine.cancel_plan(plan.plan_id) is True
    cancelled = engine.get_plan(plan.plan_id)
    assert cancelled.status == PlanStatus.CANCELLED


# --------------------------------------------------------------------------- #
# Knowledge Engine
# --------------------------------------------------------------------------- #

def test_knowledge_add_and_validate():
    engine = KnowledgeEngine()
    record = engine.add(
        kind=KnowledgeKind.ASSUMPTION,
        content="the server will handle 1000 req/s",
        source=KnowledgeSource.MODEL_INFERENCE,
        confidence=0.6,
    )
    assert record.kind == KnowledgeKind.ASSUMPTION

    # Promove a fato verificado.
    ok = engine.validate_fact(record.record_id, validated_by="operator", evidence=["benchmark"])
    assert ok is True
    updated = engine.get(record.record_id)
    assert updated.is_fact()
    assert updated.is_verified()


def test_knowledge_simulation_never_becomes_fact():
    engine = KnowledgeEngine()
    record = engine.add(
        kind=KnowledgeKind.SIMULATION,
        content="simulated outcome: load doubles",
        source=KnowledgeSource.SIMULATION_OUTPUT,
        confidence=0.9,
    )
    # Não deve ser promovido a fato.
    ok = engine.validate_fact(record.record_id, validated_by="operator")
    assert ok is False
    assert engine.get(record.record_id).kind == KnowledgeKind.SIMULATION


def test_knowledge_search():
    engine = KnowledgeEngine()
    engine.add(
        kind=KnowledgeKind.FACT,
        content="disk usage is at 85 percent",
        source=KnowledgeSource.OBSERVATION,
        confidence=0.9,
    )
    results = engine.search("disk usage")
    assert len(results) >= 1


def test_knowledge_stats():
    engine = KnowledgeEngine()
    engine.add(kind=KnowledgeKind.FACT, content="a", source=KnowledgeSource.USER, confidence=0.9)
    engine.add(kind=KnowledgeKind.HYPOTHESIS, content="b", source=KnowledgeSource.MODEL_INFERENCE, confidence=0.5)
    stats = engine.stats()
    assert stats["total"] == 2
    assert stats["FACT"] == 1
    assert stats["HYPOTHESIS"] == 1
