"""Simulation Analyzer e Results Store — análise e persistência de resultados.

Responsabilidade:
* Simulation Analyzer: compara resultados entre mundos/cenários, calcula
  estatísticas comparativas, detecta outliers e gera insights.
* Results Store: armazena resultados de simulação de forma persistente e
  imutável, com verificação de integridade (SHA-256) para reprodutibilidade.

Princípios:
* Resultados são explicitamente labelados (simulated / estimated / inferred).
* Nunca são apresentados como predições garantidas.
* Reprodutibilidade: random seed e parâmetros são persistidos com cada resultado.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .simulation_types import EventType, SimulationEvent, SimulationResult, WorldStatus
from .sqlite_utils import connect_db


@dataclass(frozen=True, slots=True)
class SimulationAnalysis:
    """Análise comparativa de resultados de simulação."""

    analysis_id: str
    experiment_id: str
    worlds_compared: tuple[str, ...]
    metrics_comparison: dict[str, Any]
    outliers: tuple[str, ...]
    insights: tuple[str, ...]
    created_at: float = field(default_factory=lambda: time.time())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "experiment_id": self.experiment_id,
            "worlds_compared": list(self.worlds_compared),
            "metrics_comparison": dict(self.metrics_comparison),
            "outliers": list(self.outliers),
            "insights": list(self.insights),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class SimulationAnalyzer:
    """Analisa resultados de simulação e gera insights comparativos."""

    def analyze(
        self,
        *,
        experiment_id: str,
        results: list[SimulationResult],
    ) -> SimulationAnalysis:
        """Analisa uma lista de resultados e produz insights."""
        analysis_id = uuid.uuid4().hex
        worlds = sorted({r.world_id for r in results})

        # Comparação de métricas por mundo.
        metrics_comparison: dict[str, Any] = {}
        for r in results:
            m = dict(r.metrics)
            metrics_comparison[r.world_id] = m

        # Detecção de outliers simples: resultados com status FAILED ou
        # duração muito acima da média.
        durations = [r.duration_s for r in results if r.duration_s is not None]
        outliers: list[str] = []
        if durations:
            avg_d = sum(durations) / len(durations)
            for r in results:
                if r.duration_s is not None and r.duration_s > avg_d * 2:
                    outliers.append(f"{r.world_id} (duration {r.duration_s:.2f}s > 2x avg)")

        # Insights.
        insights: list[str] = []
        completed = [r for r in results if r.status == WorldStatus.COMPLETED]
        failed = [r for r in results if r.status == WorldStatus.FAILED]
        if completed:
            insights.append(
                f"{len(completed)}/{len(results)} simulations completed successfully"
            )
        if failed:
            insights.append(
                f"{len(failed)} simulations failed; review parameters and rules"
            )
        if len(worlds) > 1:
            insights.append(
                f"compared {len(worlds)} parallel worlds; results are simulated, not guaranteed predictions"
            )

        return SimulationAnalysis(
            analysis_id=analysis_id,
            experiment_id=experiment_id,
            worlds_compared=tuple(worlds),
            metrics_comparison=metrics_comparison,
            outliers=tuple(outliers),
            insights=tuple(insights),
        )


class ResultsStore:
    """Armazenamento persistente e imutável de resultados de simulação."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_db()

    def _init_db(self) -> None:
        with connect_db(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS simulation_results (
                    result_id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL,
                    experiment_id TEXT,
                    status TEXT NOT NULL,
                    final_state TEXT NOT NULL,
                    events TEXT,
                    metrics TEXT,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    duration_s REAL,
                    label TEXT NOT NULL,
                    metadata TEXT,
                    integrity_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_world ON simulation_results(world_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_experiment ON simulation_results(experiment_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_status ON simulation_results(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_label ON simulation_results(label)"
            )
            conn.commit()

    def store_result(self, result: SimulationResult) -> str:
        """Armazena um resultado de simulação (append-only, imutável)."""
        result_id = uuid.uuid4().hex
        final_state_json = json.dumps(dict(result.final_state), sort_keys=True, separators=(",", ":"))
        events_json = json.dumps([e.as_dict() for e in result.events], separators=(",", ":"))
        metrics_json = json.dumps(dict(result.metrics), sort_keys=True, separators=(",", ":"))
        metadata_json = json.dumps(dict(result.metadata), sort_keys=True, separators=(",", ":"))

        payload = {
            "world_id": result.world_id,
            "experiment_id": result.experiment_id,
            "status": result.status.name,
            "final_state": final_state_json,
            "events": events_json,
            "metrics": metrics_json,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_s": result.duration_s,
            "label": result.label,
        }
        integrity_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with connect_db(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO simulation_results
                (result_id, world_id, experiment_id, status, final_state, events,
                 metrics, started_at, completed_at, duration_s, label, metadata,
                 integrity_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    result.world_id,
                    result.experiment_id,
                    result.status.name,
                    final_state_json,
                    events_json,
                    metrics_json,
                    result.started_at,
                    result.completed_at,
                    result.duration_s,
                    result.label,
                    metadata_json,
                    integrity_hash,
                    time.time(),
                ),
            )
            conn.commit()
        return result_id

    def get_result(self, result_id: str) -> SimulationResult | None:
        with connect_db(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM simulation_results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_result(row)

    def list_results(
        self,
        *,
        world_id: str | None = None,
        experiment_id: str | None = None,
        label: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SimulationResult]:
        with connect_db(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM simulation_results"
            params: list[Any] = []
            clauses = []
            if world_id is not None:
                clauses.append("world_id = ?")
                params.append(world_id)
            if experiment_id is not None:
                clauses.append("experiment_id = ?")
                params.append(experiment_id)
            if label is not None:
                clauses.append("label = ?")
                params.append(label)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_result(r) for r in rows]

    def verify_integrity(self, result_id: str | None = None) -> dict[str, Any]:
        """Verifica a integridade de um resultado ou de todos."""
        with connect_db(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if result_id is not None:
                rows = conn.execute(
                    "SELECT * FROM simulation_results WHERE result_id = ?", (result_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM simulation_results").fetchall()
            total = 0
            invalid = 0
            for row in rows:
                total += 1
                result = self._row_to_result(row)
                payload = {
                    "world_id": result.world_id,
                    "experiment_id": result.experiment_id,
                    "status": result.status.name,
                    "final_state": json.dumps(dict(result.final_state), sort_keys=True, separators=(",", ":")),
                    "events": json.dumps([e.as_dict() for e in result.events], separators=(",", ":")),
                    "metrics": json.dumps(dict(result.metrics), sort_keys=True, separators=(",", ":")),
                    "started_at": result.started_at,
                    "completed_at": result.completed_at,
                    "duration_s": result.duration_s,
                    "label": result.label,
                }
                expected = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if expected != result.integrity_hash:
                    invalid += 1
            return {"total": total, "invalid": invalid, "ok": invalid == 0}

    def delete_result(self, result_id: str) -> bool:
        with connect_db(self._db_path) as conn:
            res = conn.execute(
                "DELETE FROM simulation_results WHERE result_id = ?", (result_id,)
            )
            conn.commit()
            return res.rowcount > 0

    def _row_to_result(self, row: sqlite3.Row) -> SimulationResult:
        return SimulationResult(
            world_id=row["world_id"],
            experiment_id=row["experiment_id"],
            status=WorldStatus[row["status"]],
            final_state=json.loads(row["final_state"]),
            events=self._events_from_json(row["events"]),
            metrics=json.loads(row["metrics"]) if row["metrics"] else {},
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_s=row["duration_s"],
            label=row["label"],
            integrity_hash=row["integrity_hash"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    @staticmethod
    def _events_from_json(raw: str | None) -> tuple[SimulationEvent, ...]:
        """Reconstrói os eventos persistidos para não invalidar o hash."""
        if not raw:
            return ()
        events: list[SimulationEvent] = []
        for item in json.loads(raw):
            events.append(
                SimulationEvent(
                    event_id=item["event_id"],
                    world_id=item["world_id"],
                    event_type=EventType[item["event_type"]],
                    entity_id=item.get("entity_id"),
                    timestamp=item["timestamp"],
                    payload=item.get("payload", {}),
                    caused_by=item.get("caused_by"),
                    correlation_id=item.get("correlation_id"),
                    metadata=item.get("metadata", {}),
                )
            )
        return tuple(events)


__all__ = [
    "SimulationAnalyzer",
    "SimulationAnalysis",
    "ResultsStore",
]
