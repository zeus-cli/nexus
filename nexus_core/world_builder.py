"""World Builder e World Registry — construção e registro de mundos de simulação.

Responsabilidade:
* World Builder: constrói WorldDefinition a partir de parâmetros, estado inicial,
  entidades e regras.
* World Registry: gerencia múltiplos mundos isolados (parallel worlds), cada um
  com seu próprio estado, clock, regras e eventos.

Princípios:
* Mundos são isolados entre si (simulações paralelas independentes).
* Cada mundo tem seu random seed, estado inicial e parâmetros próprios.
* Não há comunicação automática entre mundos paralelos.
"""

from __future__ import annotations

from typing import Any, Mapping

from .simulation_types import (
    SimulationEntity,
    SimulationEvent,
    SimulationParameters,
    SimulationParadigm,
    SimulationResult,
    WorldDefinition,
    WorldStatus,
)


class WorldBuilder:
    """Constrói WorldDefinition de forma fluente."""

    def __init__(self, world_id: str | None = None) -> None:
        import uuid

        self._world_id = world_id or uuid.uuid4().hex
        self._name = "Untitled World"
        self._paradigm = SimulationParadigm.AGENT_BASED
        self._parameters = SimulationParameters(paradigm=SimulationParadigm.AGENT_BASED)
        self._initial_state: dict[str, Any] = {}
        self._entities: list[SimulationEntity] = []
        self._rules: list[str] = []
        self._agents: list[str] = []
        self._metadata: dict[str, Any] = {}

    def name(self, value: str) -> "WorldBuilder":
        self._name = value
        return self

    def paradigm(self, value: SimulationParadigm) -> "WorldBuilder":
        self._paradigm = value
        self._parameters = SimulationParameters(paradigm=value)
        return self

    def parameters(self, value: SimulationParameters) -> "WorldBuilder":
        self._parameters = value
        return self

    def random_seed(self, value: int | None) -> "WorldBuilder":
        self._parameters = SimulationParameters(
            paradigm=self._parameters.paradigm,
            random_seed=value,
            max_steps=self._parameters.max_steps,
            max_duration_s=self._parameters.max_duration_s,
            step_interval_s=self._parameters.step_interval_s,
            resource_limits=self._parameters.resource_limits,
            enable_metrics=self._parameters.enable_metrics,
            metadata=self._parameters.metadata,
        )
        return self

    def max_steps(self, value: int) -> "WorldBuilder":
        self._parameters = SimulationParameters(
            paradigm=self._parameters.paradigm,
            random_seed=self._parameters.random_seed,
            max_steps=value,
            max_duration_s=self._parameters.max_duration_s,
            step_interval_s=self._parameters.step_interval_s,
            resource_limits=self._parameters.resource_limits,
            enable_metrics=self._parameters.enable_metrics,
            metadata=self._parameters.metadata,
        )
        return self

    def initial_state(self, state: Mapping[str, Any]) -> "WorldBuilder":
        self._initial_state = dict(state)
        return self

    def add_entity(
        self,
        entity_id: str,
        entity_type,
        name: str,
        *,
        state: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> "WorldBuilder":
        from .simulation_types import EntityType

        et = entity_type if isinstance(entity_type, EntityType) else EntityType[entity_type]
        self._entities.append(
            SimulationEntity(
                entity_id=entity_id,
                entity_type=et,
                name=name,
                state=dict(state or {}),
                properties=dict(properties or {}),
                parent_id=parent_id,
            )
        )
        return self

    def add_rule(self, rule_id: str) -> "WorldBuilder":
        self._rules.append(rule_id)
        return self

    def add_agent(self, agent_id: str) -> "WorldBuilder":
        self._agents.append(agent_id)
        return self

    def metadata(self, key: str, value: Any) -> "WorldBuilder":
        self._metadata[key] = value
        return self

    def build(self) -> WorldDefinition:
        return WorldDefinition(
            world_id=self._world_id,
            name=self._name,
            paradigm=self._paradigm,
            parameters=self._parameters,
            initial_state=self._initial_state,
            entities=tuple(self._entities),
            rules=tuple(self._rules),
            agents=tuple(self._agents),
            metadata=self._metadata,
        )


class WorldRegistry:
    """Registro de mundos de simulação isolados (parallel worlds)."""

    def __init__(self) -> None:
        self._worlds: dict[str, WorldDefinition] = {}
        self._statuses: dict[str, WorldStatus] = {}

    def register(self, definition: WorldDefinition) -> None:
        if definition.world_id in self._worlds:
            raise ValueError(f"world already registered: {definition.world_id}")
        self._worlds[definition.world_id] = definition
        self._statuses[definition.world_id] = WorldStatus.CREATED

    def unregister(self, world_id: str) -> bool:
        if world_id not in self._worlds:
            return False
        del self._worlds[world_id]
        del self._statuses[world_id]
        return True

    def get(self, world_id: str) -> WorldDefinition | None:
        return self._worlds.get(world_id)

    def get_status(self, world_id: str) -> WorldStatus | None:
        return self._statuses.get(world_id)

    def set_status(self, world_id: str, status: WorldStatus) -> bool:
        if world_id not in self._worlds:
            return False
        self._statuses[world_id] = status
        return True

    def list_worlds(self) -> list[tuple[WorldDefinition, WorldStatus]]:
        return [
            (self._worlds[wid], self._statuses[wid]) for wid in self._worlds
        ]

    def list_by_paradigm(self, paradigm: SimulationParadigm) -> list[WorldDefinition]:
        return [w for w, _ in self.list_worlds() if w.paradigm == paradigm]

    def count(self) -> int:
        return len(self._worlds)

    def list_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "world_id": w.world_id,
                "name": w.name,
                "paradigm": w.paradigm.name,
                "status": self._statuses[w.world_id].name,
                "parameters": w.parameters.as_dict(),
            }
            for w, _ in self.list_worlds()
        ]


__all__ = [
    "WorldBuilder",
    "WorldRegistry",
]
