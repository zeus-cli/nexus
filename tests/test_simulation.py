"""Testes dos componentes de Universal Simulation (Fase 5)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from nexus_core.simulation_types import (
    SimulationEntity,
    SimulationEvent,
    SimulationParameters,
    SimulationParadigm,
    SimulationResult,
    WorldDefinition,
    WorldStatus,
    EntityType,
    EventType,
)
from nexus_core.simulation_clock import SimulationClock
from nexus_core.state_manager import StateManager, WorldSnapshot
from nexus_core.rules_engine import RulesEngine, SimulationRule, RuleStatus
from nexus_core.event_scheduler import EventScheduler, ScheduledEvent
from nexus_core.world_builder import WorldBuilder, WorldRegistry
from nexus_core.scenario_manager import ScenarioManager, ExperimentRunner, Scenario, Experiment
from nexus_core.metrics_engine import MetricsEngine, MetricSample
from nexus_core.simulation_analyzer import SimulationAnalyzer, SimulationAnalysis, ResultsStore


# --------------------------------------------------------------------------- #
# Simulation Types
# --------------------------------------------------------------------------- #

def test_simulation_parameters():
    p = SimulationParameters(
        paradigm=SimulationParadigm.AGENT_BASED,
        random_seed=42,
        max_steps=100,
        step_interval_s=0.5,
    )
    assert p.paradigm == SimulationParadigm.AGENT_BASED
    assert p.random_seed == 42
    d = p.as_dict()
    assert d["paradigm"] == "AGENT_BASED"
    assert d["random_seed"] == 42


def test_world_definition():
    wd = WorldDefinition(
        world_id="w-1",
        name="Test World",
        paradigm=SimulationParadigm.ECONOMIC,
        parameters=SimulationParameters(paradigm=SimulationParadigm.ECONOMIC),
    )
    assert wd.world_id == "w-1"
    assert wd.paradigm == SimulationParadigm.ECONOMIC


def test_simulation_result_label():
    r = SimulationResult(
        world_id="w-1",
        experiment_id="exp-1",
        status=WorldStatus.COMPLETED,
        final_state={"x": 1},
        label="simulated",
    )
    assert r.is_simulation() is True
    assert r.as_dict()["label"] == "simulated"


# --------------------------------------------------------------------------- #
# Simulation Clock
# --------------------------------------------------------------------------- #

def test_simulation_clock_step():
    clock = SimulationClock(world_id="w-1", step_interval=1.0, max_steps=3)
    clock.start()
    assert clock.is_running
    clock.step()
    assert clock.current_time == 1.0
    assert clock.step_count == 1
    clock.step()
    assert clock.current_time == 2.0
    clock.step()
    assert clock.current_time == 3.0
    assert clock.should_stop() is True
    clock.stop()
    assert clock.is_running is False


def test_simulation_clock_pause_resume():
    clock = SimulationClock(world_id="w-1")
    clock.start()
    clock.pause()
    assert clock.is_paused
    clock.step()  # não deve avançar
    assert clock.current_time == 0.0
    clock.resume()
    assert clock.is_paused is False
    clock.step()
    assert clock.current_time == 1.0


def test_simulation_clock_delta():
    clock = SimulationClock(world_id="w-1")
    clock.start()
    clock.step(delta=5.0)
    assert clock.current_time == 5.0


# --------------------------------------------------------------------------- #
# State Manager
# --------------------------------------------------------------------------- #

def test_state_manager_initialize_and_get():
    sm = StateManager()
    sm.initialize_world(
        world_id="w-1",
        initial_state={"pop": 100},
        entities={"e1": {"type": "agent", "hp": 10}},
    )
    state = sm.get_state("w-1")
    assert state is not None
    assert state["state"]["pop"] == 100
    assert state["entities"]["e1"]["hp"] == 10


def test_state_manager_update_and_entity():
    sm = StateManager()
    sm.initialize_world(world_id="w-1", initial_state={"pop": 100})
    sm.update_state(world_id="w-1", state={"pop": 200}, step_count=5)
    sm.set_entity_state("w-1", "e1", {"hp": 5})
    state = sm.get_state("w-1")
    assert state["state"]["pop"] == 200
    assert state["step_count"] == 5
    assert sm.get_entity_state("w-1", "e1") == {"hp": 5}


def test_state_manager_snapshot_restore():
    sm = StateManager()
    sm.initialize_world(world_id="w-1", initial_state={"pop": 100})
    sid = sm.take_snapshot("w-1", clock_time=0.0, step_count=0, label="initial")
    sm.update_state(world_id="w-1", state={"pop": 999})
    assert sm.get_state("w-1")["state"]["pop"] == 999
    ok = sm.restore_snapshot("w-1", sid)
    assert ok is True
    assert sm.get_state("w-1")["state"]["pop"] == 100


def test_state_manager_snapshots_list():
    sm = StateManager()
    sm.initialize_world(world_id="w-1", initial_state={})
    sm.take_snapshot("w-1", clock_time=0.0, step_count=0)
    sm.take_snapshot("w-1", clock_time=1.0, step_count=1)
    snaps = sm.list_snapshots("w-1")
    assert len(snaps) == 3  # 1 inicial + 2 manuais


# --------------------------------------------------------------------------- #
# Rules Engine
# --------------------------------------------------------------------------- #

def test_rules_engine_register_and_evaluate():
    re_ = RulesEngine()
    rule = SimulationRule(
        rule_id="r1",
        name="pop threshold",
        paradigm="economic",
        condition="pop > 100",
        action="alert",
        priority=1,
    )
    re_.register_rule(rule)
    assert re_.evaluate("r1", {"pop": 150}) is True
    assert re_.evaluate("r1", {"pop": 50}) is False


def test_rules_engine_fire_matching():
    re_ = RulesEngine()
    fired = []
    rule = SimulationRule(
        rule_id="r2",
        name="high pop",
        paradigm="economic",
        condition="pop > 1000",
        action="notify",
    )
    re_.register_rule(rule)
    re_.register_action_handler("r2", lambda ctx: "notified")
    fired = re_.fire_matching({"pop": 2000}, paradigm="economic")
    assert len(fired) == 1
    assert fired[0][0] == "r2"
    assert fired[0][1] == "notified"


# --------------------------------------------------------------------------- #
# Event Scheduler
# --------------------------------------------------------------------------- #

def test_event_scheduler_schedule_and_pop():
    sched = EventScheduler()
    eid = sched.schedule(
        world_id="w-1",
        timestamp=10.0,
        event_type=EventType.AGENT_ACTION,
        payload={"action": "move"},
        entity_id="e1",
    )
    assert sched.count_pending() == 1
    due = sched.pop_due(current_time=5.0)
    assert len(due) == 0  # ainda não chegou
    due = sched.pop_due(current_time=10.0)
    assert len(due) == 1
    assert due[0].event.event_id == eid


def test_event_scheduler_cancel():
    sched = EventScheduler()
    eid = sched.schedule(
        world_id="w-1",
        timestamp=10.0,
        event_type=EventType.STATE_CHANGE,
    )
    assert sched.cancel(eid) is True
    assert sched.count_pending() == 0
    assert sched.cancel(eid) is False  # já cancelado


# --------------------------------------------------------------------------- #
# World Builder and World Registry
# --------------------------------------------------------------------------- #

def test_world_builder():
    wb = WorldBuilder()
    wd = (
        wb.name("Economy Sim")
        .paradigm(SimulationParadigm.ECONOMIC)
        .random_seed(42)
        .max_steps(50)
        .initial_state({"gdp": 1000})
        .add_entity("e1", "AGENT", "Alice", state={"wealth": 50})
        .add_rule("r1")
        .add_agent("agent-1")
        .metadata("region", "NA")
        .build()
    )
    assert wd.name == "Economy Sim"
    assert wd.paradigm == SimulationParadigm.ECONOMIC
    assert wd.parameters.random_seed == 42
    assert wd.parameters.max_steps == 50
    assert wd.initial_state["gdp"] == 1000
    assert len(wd.entities) == 1
    assert wd.entities[0].name == "Alice"
    assert wd.rules == ("r1",)
    assert wd.agents == ("agent-1",)


def test_world_registry():
    wr = WorldRegistry()
    wb = WorldBuilder(world_id="w-reg-1")
    wd = wb.name("W").paradigm(SimulationParadigm.SOCIAL).build()
    wr.register(wd)
    assert wr.count() == 1
    assert wr.get_status("w-reg-1") == WorldStatus.CREATED
    wr.set_status("w-reg-1", WorldStatus.RUNNING)
    assert wr.get_status("w-reg-1") == WorldStatus.RUNNING
    worlds = wr.list_worlds()
    assert len(worlds) == 1
    assert wr.unregister("w-reg-1") is True
    assert wr.count() == 0


# --------------------------------------------------------------------------- #
# Scenario Manager and Experiment Runner
# --------------------------------------------------------------------------- #

def test_scenario_manager():
    sm = ScenarioManager()
    s = sm.create_scenario(
        name="baseline",
        world_id="w-1",
        description="base scenario",
        is_baseline=True,
    )
    assert s.is_baseline is True
    assert sm.list_baselines() == [s]
    assert sm.list_alternatives() == []

    alt = sm.create_scenario(
        name="alt",
        world_id="w-1",
        is_baseline=False,
    )
    assert len(sm.list_alternatives("w-1")) == 1


def test_experiment_runner():
    runner = ExperimentRunner()

    def fake_run(params):
        return SimulationResult(
            world_id="w-1",
            experiment_id="exp-1",
            status=WorldStatus.COMPLETED,
            final_state={"result": "ok"},
            label="simulated",
        )

    runner.set_run_simulation(fake_run)
    exp = runner.create_experiment(
        name="test exp",
        scenario_ids=("s1",),
        repetitions=2,
    )
    assert exp.repetitions == 2
    results = asyncio.run(runner.run_experiment(exp.experiment_id))
    assert len(results) == 2
    assert all(r.status == WorldStatus.COMPLETED for r in results)
    comp = runner.compare_scenarios(exp.experiment_id)
    assert comp["s1"]["runs"] == 2
    assert comp["s1"]["completed"] == 2


# --------------------------------------------------------------------------- #
# Metrics Engine
# --------------------------------------------------------------------------- #

def test_metrics_engine_record_and_aggregate():
    me = MetricsEngine()
    me.record(world_id="w-1", name="cpu", value=50.0)
    me.record(world_id="w-1", name="cpu", value=70.0)
    me.record(world_id="w-1", name="cpu", value=90.0)
    agg = me.aggregate("w-1:cpu", operation="avg")
    assert agg is not None
    assert agg["value"] == 70.0
    assert agg["count"] == 3

    summary = me.world_summary("w-1")
    assert "cpu" in summary["metrics"]
    assert summary["metrics"]["cpu"]["avg"]["value"] == 70.0


# --------------------------------------------------------------------------- #
# Simulation Analyzer and Results Store
# --------------------------------------------------------------------------- #

def test_simulation_analyzer():
    analyzer = SimulationAnalyzer()
    results = [
        SimulationResult(
            world_id="w-1",
            experiment_id="exp-1",
            status=WorldStatus.COMPLETED,
            final_state={},
            metrics={"cpu": 50.0},
            label="simulated",
        ),
        SimulationResult(
            world_id="w-2",
            experiment_id="exp-1",
            status=WorldStatus.COMPLETED,
            final_state={},
            metrics={"cpu": 90.0},
            label="simulated",
        ),
    ]
    analysis = analyzer.analyze(experiment_id="exp-1", results=results)
    assert analysis.worlds_compared == ("w-1", "w-2")
    assert len(analysis.insights) >= 1


def test_results_store_persist_and_verify():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ResultsStore(Path(tmpdir) / "sim.db")
        r = SimulationResult(
            world_id="w-1",
            experiment_id="exp-1",
            status=WorldStatus.COMPLETED,
            final_state={"x": 1},
            label="simulated",
            metrics={"cpu": 50.0},
        )
        rid = store.store_result(r)
        assert rid is not None
        r2 = store.get_result(rid)
        assert r2 is not None
        assert r2.world_id == "w-1"
        assert r2.label == "simulated"
        report = store.verify_integrity(rid)
        assert report["ok"] is True

        results = store.list_results(world_id="w-1")
        assert len(results) == 1


__all__ = []
