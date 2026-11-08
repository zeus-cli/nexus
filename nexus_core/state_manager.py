"""State Manager — gerenciamento de estado de mundos de simulação.

Responsabilidade: manter o estado atual de cada mundo, tirar snapshots,
restaurar estados anteriores e permitir reprodutibilidade de execuções.

Princípios:
* Cada mundo tem seu estado isolado.
* Snapshots permitem voltar a estados anteriores (reprodutibilidade).
* Estados são serializáveis para persistência e comparação entre mundos paralelos.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class WorldSnapshot:
    """Snapshot do estado de um mundo em um ponto do tempo."""

    snapshot_id: str
    world_id: str
    clock_time: float
    step_count: int
    state: Mapping[str, Any]
    entities: dict[str, dict[str, Any]]
    created_at: float = field(default_factory=lambda: time.monotonic())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "world_id": self.world_id,
            "clock_time": self.clock_time,
            "step_count": self.step_count,
            "state": dict(self.state),
            "entities": dict(self.entities),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class StateManager:
    """Gerencia estados e snapshots de mundos de simulação."""

    def __init__(self, max_snapshots_per_world: int = 100) -> None:
        self._world_states: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, list[WorldSnapshot]] = {}  # world_id -> [snapshots]
        self._max_snapshots = max_snapshots_per_world

    def initialize_world(
        self,
        world_id: str,
        initial_state: Mapping[str, Any],
        entities: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Inicializa o estado de um mundo."""
        self._world_states[world_id] = {
            "state": dict(initial_state),
            "entities": dict(entities or {}),
            "clock_time": 0.0,
            "step_count": 0,
            "created_at": time.monotonic(),
        }
        self._snapshots[world_id] = []
        self.take_snapshot(world_id, clock_time=0.0, step_count=0, label="initial")

    def get_state(self, world_id: str) -> dict[str, Any] | None:
        """Retorna uma cópia profunda do estado atual do mundo."""
        ws = self._world_states.get(world_id)
        if ws is None:
            return None
        return {
            "state": copy.deepcopy(ws["state"]),
            "entities": copy.deepcopy(ws["entities"]),
            "clock_time": ws["clock_time"],
            "step_count": ws["step_count"],
            "created_at": ws["created_at"],
        }

    def update_state(
        self,
        world_id: str,
        *,
        state: Mapping[str, Any] | None = None,
        entities: dict[str, dict[str, Any]] | None = None,
        clock_time: float | None = None,
        step_count: int | None = None,
    ) -> None:
        """Atualiza o estado do mundo (merge parcial)."""
        ws = self._world_states.get(world_id)
        if ws is None:
            raise KeyError(f"world not found: {world_id}")
        if state is not None:
            ws["state"].update(dict(state))
        if entities is not None:
            ws["entities"].update(entities)
        if clock_time is not None:
            ws["clock_time"] = clock_time
        if step_count is not None:
            ws["step_count"] = step_count

    def set_entity_state(
        self,
        world_id: str,
        entity_id: str,
        state: Mapping[str, Any],
    ) -> None:
        """Atualiza o estado de uma entidade específica no mundo."""
        ws = self._world_states.get(world_id)
        if ws is None:
            raise KeyError(f"world not found: {world_id}")
        ws["entities"][entity_id] = dict(state)

    def get_entity_state(
        self,
        world_id: str,
        entity_id: str,
    ) -> dict[str, Any] | None:
        ws = self._world_states.get(world_id)
        if ws is None:
            return None
        return dict(ws["entities"].get(entity_id, {}))

    def take_snapshot(
        self,
        world_id: str,
        *,
        clock_time: float,
        step_count: int,
        label: str | None = None,
    ) -> str:
        """Tira um snapshot do estado atual do mundo."""
        ws = self._world_states.get(world_id)
        if ws is None:
            raise KeyError(f"world not found: {world_id}")
        snapshot_id = uuid.uuid4().hex
        snapshot = WorldSnapshot(
            snapshot_id=snapshot_id,
            world_id=world_id,
            clock_time=clock_time,
            step_count=step_count,
            state=copy.deepcopy(ws["state"]),
            entities=copy.deepcopy(ws["entities"]),
            metadata={"label": label} if label else {},
        )
        snaps = self._snapshots[world_id]
        snaps.append(snapshot)
        if len(snaps) > self._max_snapshots:
            snaps.pop(0)
        return snapshot_id

    def restore_snapshot(
        self,
        world_id: str,
        snapshot_id: str,
    ) -> bool:
        """Restaura o mundo para o estado de um snapshot anterior."""
        snaps = self._snapshots.get(world_id, [])
        snapshot = next((s for s in snaps if s.snapshot_id == snapshot_id), None)
        if snapshot is None:
            return False
        ws = self._world_states.get(world_id)
        if ws is None:
            return False
        ws["state"] = copy.deepcopy(snapshot.state)
        ws["entities"] = copy.deepcopy(snapshot.entities)
        ws["clock_time"] = snapshot.clock_time
        ws["step_count"] = snapshot.step_count
        return True

    def list_snapshots(self, world_id: str) -> list[WorldSnapshot]:
        return list(self._snapshots.get(world_id, []))

    def delete_world(self, world_id: str) -> bool:
        """Remove um mundo e seus snapshots."""
        if world_id not in self._world_states:
            return False
        del self._world_states[world_id]
        self._snapshots.pop(world_id, None)
        return True

    def list_worlds(self) -> list[str]:
        return list(self._world_states.keys())

    def world_exists(self, world_id: str) -> bool:
        return world_id in self._world_states

    def as_dict(self, world_id: str) -> dict[str, Any] | None:
        """Retorna uma representação serializável do mundo."""
        ws = self._world_states.get(world_id)
        if ws is None:
            return None
        return {
            "world_id": world_id,
            "state": copy.deepcopy(ws["state"]),
            "entities": copy.deepcopy(ws["entities"]),
            "clock_time": ws["clock_time"],
            "step_count": ws["step_count"],
            "created_at": ws["created_at"],
            "snapshots": [s.as_dict() for s in self._snapshots.get(world_id, [])],
        }


__all__ = [
    "StateManager",
    "WorldSnapshot",
]
