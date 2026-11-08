"""Metrics Engine — coleta e agregação de métricas de simulação.

Responsabilidade: coletar métricas durante a execução de simulações,
agregá-las por mundo/cenário e expor resultados para análise.

Princípios:
* Métricas são coletadas de forma isolada por mundo.
* Métricas suportam agregação temporal (média, min, max, contagem).
* Resultado é explicitamente labelado como simulado/estimado.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class MetricSample:
    """Uma amostra de métrica em um ponto do tempo."""

    metric_id: str
    world_id: str
    name: str
    value: float
    timestamp: float
    tags: Mapping[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class MetricsEngine:
    """Coleta e agrega métricas de simulação."""

    def __init__(self) -> None:
        self._samples: dict[str, list[MetricSample]] = {}  # metric_id -> [samples]
        self._world_metrics: dict[str, set[str]] = {}  # world_id -> {metric_ids}

    def record(
        self,
        *,
        world_id: str,
        name: str,
        value: float,
        timestamp: float | None = None,
        tags: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Registra uma amostra de métrica."""
        metric_id = f"{world_id}:{name}"
        sample = MetricSample(
            metric_id=metric_id,
            world_id=world_id,
            name=name,
            value=float(value),
            timestamp=timestamp if timestamp is not None else time.monotonic(),
            tags=dict(tags or {}),
            metadata=dict(metadata or {}),
        )
        self._samples.setdefault(metric_id, []).append(sample)
        self._world_metrics.setdefault(world_id, set()).add(metric_id)
        return metric_id

    def get_samples(self, metric_id: str) -> list[MetricSample]:
        return list(self._samples.get(metric_id, []))

    def get_world_metrics(self, world_id: str) -> list[MetricSample]:
        result: list[MetricSample] = []
        for metric_id in self._world_metrics.get(world_id, set()):
            result.extend(self._samples.get(metric_id, []))
        result.sort(key=lambda s: s.timestamp)
        return result

    def aggregate(
        self,
        metric_id: str,
        *,
        operation: str = "avg",
    ) -> dict[str, Any] | None:
        """Agrega amostras de uma métrica (avg, min, max, count, sum, latest)."""
        samples = self._samples.get(metric_id)
        if not samples:
            return None
        values = [s.value for s in samples]
        if operation == "avg":
            result = sum(values) / len(values)
        elif operation == "min":
            result = min(values)
        elif operation == "max":
            result = max(values)
        elif operation == "sum":
            result = sum(values)
        elif operation == "count":
            result = float(len(values))
        elif operation == "latest":
            result = samples[-1].value
        else:
            result = sum(values) / len(values)
        return {
            "metric_id": metric_id,
            "operation": operation,
            "value": result,
            "count": len(values),
            "first_timestamp": samples[0].timestamp,
            "last_timestamp": samples[-1].timestamp,
        }

    def world_summary(self, world_id: str) -> dict[str, Any]:
        """Resumo de todas as métricas de um mundo."""
        summary: dict[str, Any] = {"world_id": world_id, "metrics": {}}
        for metric_id in self._world_metrics.get(world_id, set()):
            name = metric_id.split(":", 1)[1]
            summary["metrics"][name] = {
                "avg": self.aggregate(metric_id, operation="avg"),
                "min": self.aggregate(metric_id, operation="min"),
                "max": self.aggregate(metric_id, operation="max"),
                "count": self.aggregate(metric_id, operation="count"),
                "latest": self.aggregate(metric_id, operation="latest"),
            }
        return summary

    def clear_world(self, world_id: str) -> int:
        """Remove todas as amostras de um mundo."""
        metric_ids = self._world_metrics.pop(world_id, set())
        count = 0
        for mid in metric_ids:
            if mid in self._samples:
                count += len(self._samples.pop(mid))
        return count


__all__ = [
    "MetricsEngine",
    "MetricSample",
]
