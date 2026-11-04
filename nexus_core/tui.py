"""Small read-only terminal dashboard for runtime state."""

from __future__ import annotations

import curses
from datetime import datetime, timezone
from typing import Any

from .app import NexusRuntime, health

PANELS = {ord("1"): "status", ord("2"): "events", ord("3"): "memory", ord("4"): "tasks"}


def _truncate(value: object, width: int) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _task_events(runtime: NexusRuntime) -> list[Any]:
    events = runtime.store.query(limit=200)
    return [event for event in events if event.kind.startswith("agent.task.")][:40]


def _draw(screen: Any, runtime: NexusRuntime, panel: str, offset: int) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    screen.addnstr(0, 0, "NEXUS · painel local", max(1, width - 1), curses.A_BOLD)
    screen.addnstr(
        1, 0,
        " 1 estado   2 eventos   3 memória   4 tarefas   ↑/↓ navegar   r atualizar   q sair",
        max(1, width - 1),
    )
    screen.addnstr(2, 0, "─" * max(0, width - 1), max(0, width - 1), curses.A_DIM)
    rows: list[str] = []
    now = datetime.now(timezone.utc)

    if panel == "status":
        report = health(runtime)
        rows = [
            f"Estado: {report['status']} · versão {report['version']} · ambiente {report['environment']}",
            f"Autonomia: {report['autonomy']['level']}/4 ({report['autonomy']['name']})",
            f"Banco: schema v{report['database']['schema_version']} · integridade {report['database']['integrity']}",
            f"Eventos: {report['events']['total']} · {report['database']['size_bytes']} bytes",
            f"Memórias: {report['memory'].get('total', 0)} · provedores LLM ativos: {report['llm']['enabled_providers']}",
            f"Retenção: {report['retention']['days']} dias" if report["retention"]["enabled"] else "Retenção: desativada",
            f"Runtime desde: {report['uptime_s']:.0f} s · atualizado {now.astimezone().strftime('%H:%M:%S')}",
            "",
            "Este painel é somente leitura. Use `nexus task run` para solicitar uma tarefa.",
        ]
    elif panel == "events":
        rows = [
            f"{event.created_at.astimezone().strftime('%m-%d %H:%M:%S')}  {event.severity.value:<8} {event.kind:<32} {event.source}"
            for event in runtime.store.query(limit=200)
        ] or ["Nenhum evento registrado."]
    elif panel == "memory":
        rows = [
            f"{record.created_at.astimezone().strftime('%m-%d %H:%M')}  {record.kind:<11} {record.title or '—'} · {record.content}"
            for record in runtime.memory.list(limit=200)
        ] or ["Nenhuma memória registrada."]
    else:
        rows = [
            f"{event.created_at.astimezone().strftime('%m-%d %H:%M:%S')}  {event.kind}  {event.payload}"
            for event in _task_events(runtime)
        ] or ["Nenhuma tarefa registrada."]

    content_height = max(0, height - 4)
    max_offset = max(0, len(rows) - content_height)
    offset = min(max(offset, 0), max_offset)
    for index, row in enumerate(rows[offset : offset + content_height], start=3):
        try:
            screen.addnstr(index, 0, _truncate(row, width - 1), max(1, width - 1))
        except curses.error:
            pass
    screen.refresh()


def run_tui(runtime: NexusRuntime) -> None:
    """Run a terminal dashboard; requires an interactive terminal."""
    def draw_loop(screen: Any) -> None:
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(1000)
        panel, offset = "status", 0
        while True:
            _draw(screen, runtime, panel, offset)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in PANELS:
                panel, offset = PANELS[key], 0
            elif key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)

    curses.wrapper(draw_loop)
