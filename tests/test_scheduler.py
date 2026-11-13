"""Testes do agendador de tarefas em background."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nexus_core.scheduler import Scheduler


class _Automation:
    async def run(self, target: str) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, duration_ms=1, stdout="", stderr="")


class _Cognitive:
    async def run(self, target: str) -> SimpleNamespace:
        now = datetime.now(timezone.utc)
        return SimpleNamespace(task_id="task", started_at=now, completed_at=now)


def _scheduler() -> Scheduler:
    return Scheduler(_Automation(), _Cognitive(), tick_s=0.01)


async def _run_for(scheduler: Scheduler, seconds: float = 0.05) -> None:
    await scheduler.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scheduler.stop()


def test_scheduler_next_run_uses_wall_clock():
    scheduler = _scheduler()
    before = time.time()
    task_id = scheduler.schedule(name="tick", kind="automation", target="noop")
    task = scheduler.get_task(task_id)

    assert task is not None
    assert before <= task["next_run"] <= time.time()
    assert abs(task["next_run"] - time.time()) < 5


@pytest.mark.asyncio
async def test_scheduler_start_at_wall_clock_runs_due_task():
    scheduler = _scheduler()
    scheduler.schedule(
        name="now", kind="automation", target="noop", start_at=time.time()
    )

    await _run_for(scheduler)

    assert len(scheduler.get_history()) == 1


@pytest.mark.asyncio
async def test_scheduler_cancel_prevents_execution_and_frees_task_id():
    scheduler = _scheduler()
    task_id = scheduler.schedule(name="cancel", kind="automation", target="noop")

    scheduler.cancel(task_id)
    assert scheduler.get_task(task_id) is None

    await _run_for(scheduler)
    assert scheduler.get_history() == []

    # O ID pode ser reutilizado depois do cancelamento.
    assert (
        scheduler.schedule(
            name="again", kind="automation", target="noop", task_id=task_id
        )
        == task_id
    )


@pytest.mark.asyncio
async def test_scheduler_disabled_task_does_not_run():
    scheduler = _scheduler()
    task_id = scheduler.schedule(name="off", kind="automation", target="noop")
    scheduler.disable(task_id)

    await _run_for(scheduler)

    assert scheduler.get_history() == []
    task = scheduler.get_task(task_id)
    assert task is not None and task["enabled"] is False


@pytest.mark.asyncio
async def test_scheduler_enable_reactivates_scheduled_task():
    scheduler = _scheduler()
    task_id = scheduler.schedule(
        name="later", kind="automation", target="noop", enabled=False
    )
    scheduler.enable(task_id)

    await _run_for(scheduler)

    assert len(scheduler.get_history()) == 1
    assert scheduler.get_task(task_id) is None  # one-shot executada


__all__ = []
