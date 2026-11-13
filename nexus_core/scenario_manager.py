"""Scenario Manager e Experiment Runner — gerenciamento de cenários e experimentos.

Responsabilidade:
* Scenario Manager: define cenários (baseline e alternativas) com variações de
  parâmetros para um mesmo mundo de simulação.
* Experiment Runner: executa múltiplas execuções de simulação (repetições),
  Monte Carlo (variação de random seed), análise de sensibilidade, e gera
  resultados reprodutíveis.

Princípios:
* Cada experimento é associado a um ou mais mundos.
* Resultados são explicitamente labelados como simulados/estimados.
* Reprodutibilidade: random seed e parâmetros são persistidos com cada execução.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .simulation_types import (
    SimulationParameters,
    SimulationParadigm,
    SimulationResult,
    WorldStatus,
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """Um cenário de simulação (baseline ou alternativa)."""

    scenario_id: str
    name: str
    world_id: str
    description: str
    parameters: SimulationParameters
    is_baseline: bool = False
    parameter_variations: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: time.monotonic())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "world_id": self.world_id,
            "description": self.description,
            "parameters": self.parameters.as_dict(),
            "is_baseline": self.is_baseline,
            "parameter_variations": dict(self.parameter_variations),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Experiment:
    """Um experimento: execução de um ou mais cenários com múltiplas repetições."""

    experiment_id: str
    name: str
    scenario_ids: tuple[str, ...]
    repetitions: int
    monte_carlo: bool = False  # variar random_seed a cada repetição
    sensitivity_analysis: bool = False
    created_at: float = field(default_factory=lambda: time.monotonic())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "scenario_ids": list(self.scenario_ids),
            "repetitions": self.repetitions,
            "monte_carlo": self.monte_carlo,
            "sensitivity_analysis": self.sensitivity_analysis,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class ScenarioManager:
    """Gerencia cenários de simulação (baseline e alternativas)."""

    def __init__(self) -> None:
        self._scenarios: dict[str, Scenario] = {}

    def create_scenario(
        self,
        *,
        name: str,
        world_id: str,
        description: str = "",
        parameters: SimulationParameters | None = None,
        is_baseline: bool = False,
        parameter_variations: Mapping[str, Any] | None = None,
        scenario_id: str | None = None,
    ) -> Scenario:
        sid = scenario_id or uuid.uuid4().hex
        scenario = Scenario(
            scenario_id=sid,
            name=name,
            world_id=world_id,
            description=description,
            parameters=parameters or SimulationParameters(
                paradigm=SimulationParadigm.AGENT_BASED
            ),
            is_baseline=is_baseline,
            parameter_variations=dict(parameter_variations or {}),
        )
        self._scenarios[sid] = scenario
        return scenario

    def get_scenario(self, scenario_id: str) -> Scenario | None:
        return self._scenarios.get(scenario_id)

    def list_scenarios(self, world_id: str | None = None) -> list[Scenario]:
        result = list(self._scenarios.values())
        if world_id is not None:
            result = [s for s in result if s.world_id == world_id]
        return result

    def list_baselines(self) -> list[Scenario]:
        return [s for s in self._scenarios.values() if s.is_baseline]

    def list_alternatives(self, baseline_id: str | None = None) -> list[Scenario]:
        result = [s for s in self._scenarios.values() if not s.is_baseline]
        if baseline_id is not None:
            baseline = self._scenarios.get(baseline_id)
            if baseline is not None:
                result = [s for s in result if s.world_id == baseline.world_id]
        return result

    def delete_scenario(self, scenario_id: str) -> bool:
        if scenario_id not in self._scenarios:
            return False
        del self._scenarios[scenario_id]
        return True


class ExperimentRunner:
    """Executa experimentos de simulação com múltiplas repetições."""

    def __init__(
        self,
        *,
        run_simulation: Callable[[SimulationParameters], SimulationResult] | None = None,
    ) -> None:
        self._run_simulation = run_simulation
        self._experiments: dict[str, Experiment] = {}
        self._results: dict[str, list[SimulationResult]] = {}  # experiment_id -> [results]

    def set_run_simulation(
        self, func: Callable[[SimulationParameters], SimulationResult]
    ) -> None:
        self._run_simulation = func

    def create_experiment(
        self,
        *,
        name: str,
        scenario_ids: tuple[str, ...],
        repetitions: int = 1,
        monte_carlo: bool = False,
        sensitivity_analysis: bool = False,
        experiment_id: str | None = None,
    ) -> Experiment:
        eid = experiment_id or uuid.uuid4().hex
        experiment = Experiment(
            experiment_id=eid,
            name=name,
            scenario_ids=scenario_ids,
            repetitions=repetitions,
            monte_carlo=monte_carlo,
            sensitivity_analysis=sensitivity_analysis,
        )
        self._experiments[eid] = experiment
        self._results[eid] = []
        return experiment

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        return self._experiments.get(experiment_id)

    def list_experiments(self) -> list[Experiment]:
        return list(self._experiments.values())

    def get_results(self, experiment_id: str) -> list[SimulationResult]:
        return list(self._results.get(experiment_id, []))

    async def run_experiment(
        self,
        experiment_id: str,
        *,
        scenario_params: Mapping[str, SimulationParameters] | None = None,
    ) -> list[SimulationResult]:
        """Executa o experimento completo e retorna todos os resultados."""
        experiment = self._experiments.get(experiment_id)
        if experiment is None:
            raise KeyError(f"experiment not found: {experiment_id}")
        if self._run_simulation is None:
            raise RuntimeError("no run_simulation function configured")

        results: list[SimulationResult] = []
        seed_offset = 0

        for scenario_id in experiment.scenario_ids:
            # Recupera parâmetros do cenário (simplificado: usa default ou variações).
            base_params = scenario_params.get(scenario_id) if scenario_params else None

            for rep in range(experiment.repetitions):
                params = base_params or SimulationParameters(
                    paradigm=SimulationParadigm.AGENT_BASED
                )
                if experiment.monte_carlo:
                    # Varia o random seed a cada repetição.
                    import copy as _copy

                    params = SimulationParameters(
                        paradigm=params.paradigm,
                        random_seed=(params.random_seed or 0) + seed_offset + rep,
                        max_steps=params.max_steps,
                        max_duration_s=params.max_duration_s,
                        step_interval_s=params.step_interval_s,
                        resource_limits=params.resource_limits,
                        enable_metrics=params.enable_metrics,
                        metadata=dict(params.metadata, repetition=rep),
                    )
                    seed_offset += 1

                started = time.time()
                try:
                    result = self._run_simulation(params)
                except Exception as exc:  # noqa: BLE001
                    result = SimulationResult(
                        world_id=scenario_id,
                        experiment_id=experiment_id,
                        status=WorldStatus.FAILED,
                        final_state={},
                        label="simulated",
                        metadata={"error": str(exc)[:512]},
                    )
                completed = time.time()

                result = SimulationResult(
                    world_id=result.world_id,
                    experiment_id=experiment_id,
                    status=result.status,
                    final_state=result.final_state,
                    events=result.events,
                    metrics=result.metrics,
                    started_at=started,
                    completed_at=completed,
                    duration_s=completed - started,
                    label=result.label,
                    metadata=dict(result.metadata, scenario_id=scenario_id, repetition=rep),
                )
                results.append(result)

        self._results[experiment_id].extend(results)
        return results

    def delete_experiment(self, experiment_id: str) -> bool:
        if experiment_id not in self._experiments:
            return False
        del self._experiments[experiment_id]
        self._results.pop(experiment_id, None)
        return True

    def compare_scenarios(self, experiment_id: str) -> dict[str, Any]:
        """Compara resultados entre cenários de um experimento."""
        results = self._results.get(experiment_id, [])
        by_scenario: dict[str, list[SimulationResult]] = {}
        for r in results:
            by_scenario.setdefault(r.metadata.get("scenario_id", "unknown"), []).append(r)
        comparison = {}
        for sid, rs in by_scenario.items():
            completed = [r for r in rs if r.status.name == "COMPLETED"]
            comparison[sid] = {
                "runs": len(rs),
                "completed": len(completed),
                "failed": len(rs) - len(completed),
                "avg_duration_s": (
                    sum(r.duration_s or 0 for r in completed) / len(completed)
                    if completed
                    else 0
                ),
            }
        return comparison


__all__ = [
    "ScenarioManager",
    "ExperimentRunner",
    "Scenario",
    "Experiment",
]
