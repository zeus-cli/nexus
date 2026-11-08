"""Tipos base e contratos do NEXUS Simulation (Fase 5).

Responsabilidade: definir os contratos compartilhados por todos os componentes
do subsistema de simulação universal (World Builder, Entity Manager, Rules
Engine, Simulation Clock, Event Scheduler, Scenario Manager, Experiment
Runner, Metrics Engine, Simulation Analyzer, Results Store).

Princípios:
* Simulação isolada do ambiente operacional real.
* Agentes simulados não recebem automaticamente privilégios de execução real.
* Resultados são explicitamente labelados como simulados/estimados/inferidos.
* Mundos paralelos independentes com random seed e estado próprio.
* Reprodutibilidade: estados, parâmetros e sementes são persistíveis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Mapping


class SimulationParadigm(Enum):
    """Paradigmas de simulação suportados."""

    AGENT_BASED = auto()           # Modelagem baseada em agentes
    DISCRETE_EVENT = auto()        # Simulação de eventos discretos
    RULE_BASED = auto()            # Simulação baseada em regras
    MATHEMATICAL = auto()          # Simulação matemática
    PROBABILISTIC = auto()         # Simulação probabilística
    SOCIAL = auto()                # Simulação social
    ECONOMIC = auto()              # Simulação econômica
    NETWORK = auto()               # Simulação de rede
    TECHNICAL = auto()             # Simulação técnica
    DIGITAL_TWIN = auto()          # Modelo de gêmeo digital


class WorldStatus(Enum):
    CREATED = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    PAUSED = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


class EntityType(Enum):
    AGENT = auto()
    RESOURCE = auto()
    LOCATION = auto()
    EVENT = auto()
    CUSTOM = auto()


class EventType(Enum):
    STATE_CHANGE = auto()
    AGENT_ACTION = auto()
    RULE_TRIGGERED = auto()
    MESSAGE = auto()
    CUSTOM = auto()


@dataclass(frozen=True, slots=True)
class SimulationParameters:
    """Parâmetros de configuração de um mundo de simulação."""

    paradigm: SimulationParadigm
    random_seed: int | None = None
    max_steps: int | None = None
    max_duration_s: float | None = None
    step_interval_s: float = 1.0
    resource_limits: Mapping[str, Any] = field(default_factory=dict)
    enable_metrics: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paradigm": self.paradigm.name,
            "random_seed": self.random_seed,
            "max_steps": self.max_steps,
            "max_duration_s": self.max_duration_s,
            "step_interval_s": self.step_interval_s,
            "resource_limits": dict(self.resource_limits),
            "enable_metrics": self.enable_metrics,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SimulationEntity:
    """Entidade dentro de um mundo de simulação."""

    entity_id: str
    entity_type: EntityType
    name: str
    state: Mapping[str, Any] = field(default_factory=dict)
    properties: Mapping[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type.name,
            "name": self.name,
            "state": dict(self.state),
            "properties": dict(self.properties),
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SimulationEvent:
    """Evento ocorrido dentro de um mundo de simulação."""

    event_id: str
    world_id: str
    event_type: EventType
    entity_id: str | None
    timestamp: float
    payload: Mapping[str, Any] = field(default_factory=dict)
    caused_by: str | None = None  # event_id do evento causador
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "world_id": self.world_id,
            "event_type": self.event_type.name,
            "entity_id": self.entity_id,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
            "caused_by": self.caused_by,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorldDefinition:
    """Definição completa de um mundo de simulação."""

    world_id: str
    name: str
    paradigm: SimulationParadigm
    parameters: SimulationParameters
    initial_state: Mapping[str, Any] = field(default_factory=dict)
    entities: tuple[SimulationEntity, ...] = ()
    rules: tuple[str, ...] = ()  # IDs de regras aplicáveis
    agents: tuple[str, ...] = ()  # IDs de agentes participantes
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "name": self.name,
            "paradigm": self.paradigm.name,
            "parameters": self.parameters.as_dict(),
            "initial_state": dict(self.initial_state),
            "entities": [e.as_dict() for e in self.entities],
            "rules": list(self.rules),
            "agents": list(self.agents),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Resultado de uma execução de simulação."""

    world_id: str
    experiment_id: str | None = None
    status: WorldStatus = WorldStatus.CREATED
    final_state: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[SimulationEvent, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    completed_at: float | None = None
    duration_s: float | None = None
    label: str = "simulated"  # 'simulated' | 'estimated' | 'inferred'
    integrity_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "experiment_id": self.experiment_id,
            "status": self.status.name,
            "final_state": dict(self.final_state),
            "events": [e.as_dict() for e in self.events],
            "metrics": dict(self.metrics),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_s": self.duration_s,
            "label": self.label,
            "integrity_hash": self.integrity_hash,
            "metadata": dict(self.metadata),
        }

    def is_simulation(self) -> bool:
        return self.label in ("simulated", "estimated", "inferred")


class SimulationError(Exception):
    """Erro base do subsistema de simulação."""


class WorldNotFound(SimulationError):
    """Mundo não encontrado."""


class InvalidParadigm(SimulationError):
    """Paradigma de simulação inválido."""


__all__ = [
    "SimulationParadigm",
    "WorldStatus",
    "EntityType",
    "EventType",
    "SimulationParameters",
    "SimulationEntity",
    "SimulationEvent",
    "WorldDefinition",
    "SimulationResult",
    "SimulationError",
    "WorldNotFound",
    "InvalidParadigm",
]
