"""Simulation Clock — gerenciamento do tempo de simulação.

O Simulation Clock desacopla o tempo de simulação do tempo real do sistema,
permitindo execução rápida (step-based) ou em tempo real (contínuo). Cada
mundo de simulação possui seu próprio clock, garantindo isolamento entre
mundos paralelos.

Princípios:
* Determinismo: dado o mesmo random seed e os mesmos eventos, o clock avança
  de forma previsível.
* Isolamento: cada mundo tem seu clock independente.
* Pausa/retomada: o clock pode ser pausado e retomado sem perder o estado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SimulationClock:
    """Relógio de simulação para um mundo isolado."""

    world_id: str
    current_time: float = 0.0  # tempo de simulação (unidades abstratas)
    step_count: int = 0
    step_interval: float = 1.0  # duração de cada passo (unidades de simulação)
    real_time_factor: float = 1.0  # 1.0 = tempo real; >1 = acelerado; 0 = passo manual
    is_running: bool = False
    is_paused: bool = False
    max_steps: int | None = None
    max_duration: float | None = None
    started_at_real: float | None = None
    paused_at_real: float | None = None
    total_paused_real: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    # Callbacks opcionais (disparados a cada passo)
    on_step: Callable[["SimulationClock"], None] | None = None

    def start(self) -> None:
        """Inicia o clock."""
        import time

        self.is_running = True
        self.is_paused = False
        self.started_at_real = time.monotonic()
        self.paused_at_real = None

    def pause(self) -> None:
        """Pausa o clock."""
        import time

        if self.is_running and not self.is_paused:
            self.is_paused = True
            self.paused_at_real = time.monotonic()

    def resume(self) -> None:
        """Retoma o clock."""
        import time

        if self.is_paused:
            self.is_paused = False
            if self.paused_at_real is not None:
                self.total_paused_real += time.monotonic() - self.paused_at_real
            self.paused_at_real = None

    def stop(self) -> None:
        """Para o clock."""
        self.is_running = False
        self.is_paused = False

    def step(self, delta: float | None = None) -> float:
        """Avança o clock em um passo.

        Retorna o novo tempo de simulação.
        """
        if not self.is_running or self.is_paused:
            return self.current_time

        advance = delta if delta is not None else self.step_interval
        self.current_time += advance
        self.step_count += 1

        if self.on_step is not None:
            self.on_step(self)

        return self.current_time

    def should_stop(self) -> bool:
        """Verifica se o clock deve parar com base nos limites."""
        if self.max_steps is not None and self.step_count >= self.max_steps:
            return True
        if self.max_duration is not None and self.current_time >= self.max_duration:
            return True
        return False

    def elapsed_real(self) -> float:
        """Retorna o tempo real decorrido desde o início (excluindo pausas)."""
        import time

        if self.started_at_real is None:
            return 0.0
        now = time.monotonic()
        if self.is_paused and self.paused_at_real is not None:
            return self.paused_at_real - self.started_at_real - self.total_paused_real
        return now - self.started_at_real - self.total_paused_real

    def reset(self) -> None:
        """Reseta o clock para o estado inicial."""
        self.current_time = 0.0
        self.step_count = 0
        self.is_running = False
        self.is_paused = False
        self.started_at_real = None
        self.paused_at_real = None
        self.total_paused_real = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "current_time": self.current_time,
            "step_count": self.step_count,
            "step_interval": self.step_interval,
            "real_time_factor": self.real_time_factor,
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "max_steps": self.max_steps,
            "max_duration": self.max_duration,
            "elapsed_real": self.elapsed_real(),
            "metadata": dict(self.metadata),
        }


__all__ = ["SimulationClock"]
