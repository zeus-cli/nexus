"""CLI operacional do NEXUS (diagnóstico e administração local).

Não executa operações privilegiadas nem concede autonomia: apenas lê estado,
consulta/registra eventos e mantém o banco.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .app import NexusRuntime, bootstrap, health, serve
from .config import AUTONOMY_LEVELS, ConfigError, load_config
from .events import Event, Severity, parse_timestamp
from .privatefs import ensure_private_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="NEXUS — camada cognitiva local-first para Linux.",
    )
    parser.add_argument("--config", help="caminho do nexus.toml (padrão: config/nexus.toml)")
    parser.add_argument("--version", action="version", version=f"NEXUS {__version__}")
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="estado operacional e health check")
    subparsers.add_parser("config", help="configuração efetiva")

    desktop = subparsers.add_parser("desktop", help="abre o painel NEXUS em uma janela do navegador")
    desktop.add_argument("--no-start", action="store_true", help="não iniciar a API se estiver parada")

    logs = subparsers.add_parser("logs", help="mostra o final do log local")
    logs.add_argument("--lines", type=int, default=100)

    subparsers.add_parser("tui", help="abre o painel de estado no terminal")

    run = subparsers.add_parser("run", help="executa o serviço residente (heartbeat + retenção)")
    run.add_argument("--interval", type=float, default=60.0, help="segundos entre ciclos")
    run.add_argument("--iterations", type=int, default=None, help="número de ciclos (padrão: infinito)")

    serve_cmd = subparsers.add_parser("serve", help="executa a API local (HTTP + WebSocket)")
    serve_cmd.add_argument("--host", help="sobrescreve api.host")
    serve_cmd.add_argument("--port", type=int, help="sobrescreve api.port")

    llm = subparsers.add_parser("llm", help="modelos locais e completions")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_sub.add_parser("models", help="verifica provedores e modelos configurados")
    completion = llm_sub.add_parser("complete", help="solicita uma completion sem executar a resposta")
    completion.add_argument("--prompt", help="texto; omita para ler da entrada padrão")
    completion.add_argument("--system")
    completion.add_argument("--task", choices=("reasoning", "planning", "summary"),
                            default="reasoning")
    completion.add_argument("--temperature", type=float, default=0.2)
    completion.add_argument("--max-tokens", type=int, default=1024)
    completion.add_argument("--json-mode", action="store_true")
    completion.add_argument("--provider", help="provedor configurado; omita para roteamento automático")
    completion.add_argument("--model", help="modelo permitido na cadeia configurada")

    task = subparsers.add_parser("task", help="executa tarefas cognitivas sem ações no sistema")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_run = task_sub.add_parser("run", help="interpreta prompt, contexto de memória e LLM")
    task_run.add_argument("--prompt", help="texto; omita para ler da entrada padrão")
    task_run.add_argument("--system")
    task_run.add_argument("--task", choices=("reasoning", "planning", "summary"),
                          default="reasoning")
    task_run.add_argument("--temperature", type=float, default=0.2)
    task_run.add_argument("--max-tokens", type=int, default=1024)
    task_run.add_argument("--json-mode", action="store_true")
    task_run.add_argument("--provider")
    task_run.add_argument("--model")

    memory = subparsers.add_parser("memory", help="gerencia registros de memória locais")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_list = memory_sub.add_parser("list", help="lista memórias persistidas")
    memory_list.add_argument("--kind", choices=("short_term", "long_term", "episodic", "semantic"))
    memory_list.add_argument("--limit", type=int, default=50)
    memory_list.add_argument("--offset", type=int, default=0)
    memory_list.add_argument("--include-expired", action="store_true")
    memory_search = memory_sub.add_parser("search", help="busca textual na memória")
    memory_search.add_argument("query")
    memory_search.add_argument("--kind", choices=("short_term", "long_term", "episodic", "semantic"))
    memory_search.add_argument("--limit", type=int, default=10)
    memory_add = memory_sub.add_parser("add", help="adiciona uma memória lendo conteúdo da entrada padrão")
    memory_add.add_argument("--kind", required=True,
                            choices=("short_term", "long_term", "episodic", "semantic"))
    memory_add.add_argument("--title", default="")
    memory_add.add_argument("--metadata", default="{}", help="objeto JSON sem conteúdo pessoal")
    memory_add.add_argument("--expires-at", help="timestamp ISO-8601 opcional")
    memory_delete = memory_sub.add_parser("delete", help="apaga explicitamente uma memória")
    memory_delete.add_argument("memory_id")
    memory_purge = memory_sub.add_parser("purge-expired", help="remove memórias com expiração vencida")
    memory_export = memory_sub.add_parser("export", help="exporta registros para JSON privado")
    memory_export.add_argument("--to", required=True, help="arquivo de destino (modo 0600; diretório já deve existir)")
    memory_export.add_argument("--include-expired", action="store_true")

    diagnostics = subparsers.add_parser("diagnostics", help="diagnósticos locais de leitura")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    diagnostics_sub.add_parser("list", help="lista operações registradas e seus requisitos")
    diagnostic_run = diagnostics_sub.add_parser("run", help="executa um diagnóstico registrado")
    diagnostic_run.add_argument("action_id")

    token = subparsers.add_parser("token", help="gerencia o token local da API")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("show", help="mostra o token efetivo")
    token_sub.add_parser("rotate", help="gera um novo token local")
    token_sub.add_parser("path", help="mostra o caminho do arquivo de token")

    events = subparsers.add_parser("events", help="consulta e registro de eventos")
    event_sub = events.add_subparsers(dest="events_command", required=True)

    listing = event_sub.add_parser("list", help="lista eventos persistidos")
    listing.add_argument("--kind", help="filtra por kind exato")
    listing.add_argument("--source")
    listing.add_argument("--severity", choices=[item.value for item in Severity])
    listing.add_argument("--correlation-id")
    listing.add_argument("--since", help="ISO-8601 (ex.: 2026-11-01T00:00:00Z)")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--oldest-first", action="store_true")

    emit = event_sub.add_parser("emit", help="registra um evento manualmente")
    emit.add_argument("--kind", required=True)
    emit.add_argument("--source", default="cli")
    emit.add_argument("--payload", default="{}", help="JSON objeto")
    emit.add_argument("--severity", choices=[item.value for item in Severity], default="info")
    emit.add_argument("--correlation-id")

    event_sub.add_parser("verify", help="verifica integridade do event store")
    event_sub.add_parser("kinds", help="agrega eventos por kind")
    purge = event_sub.add_parser("purge", help="remove eventos além da retenção")
    purge.add_argument("--days", type=int, default=None, help="sobrescreve events.retention_days")

    database = subparsers.add_parser("db", help="manutenção do banco")
    db_sub = database.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("migrate", help="aplica migrações pendentes")
    backup = db_sub.add_parser("backup", help="copia consistente do banco")
    backup.add_argument("--to", help="destino (padrão: data/backups/zeus_events-<ts>.db)")
    return parser


# --------------------------------------------------------------------------- #
# comandos
# --------------------------------------------------------------------------- #
def _cmd_status(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    data = health(runtime)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        autonomy = data["autonomy"]
        print(f"NEXUS {data['version']} — estado: {data['status']}")
        _row("ambiente", data["environment"])
        _row("autonomia", f"nível {autonomy['level']} ({autonomy['name']})")
        _row("banco", data["database"]["path"])
        _row("schema", f"v{data['database']['schema_version']}")
        _row("integridade", data["database"]["integrity"])
        _row("eventos", f"{data['events']['total']} ({_human_bytes(data['events']['size_bytes'])})")
        retention = data["retention"]
        _row("retenção", f"{retention['days']} dias" if retention["enabled"] else "desativada")
        _row("provedores LLM", str(data["llm"]["enabled_providers"]))
        _row("bus", ", ".join(f"{k}={v}" for k, v in data["bus"].items()) or "-")
        _row("log", data["log_file"])
        for check in data["checks"]:
            if not check["ok"]:
                _row("falha", f"{check['name']}: {check.get('detail', 'não conforme')}")
    return 0 if data["status"] == "ok" else 1


def _cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data = config.as_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    _row("arquivo", data["source"] or "(defaults)")
    _row("ambiente", data["environment"])
    _row("autonomia", f"nível {data['autonomy']['level']} ({data['autonomy']['name']})")
    _row("data_dir", data["paths"]["data_dir"])
    _row("banco", data["paths"]["database"])
    _row("log", data["paths"]["log_file"])
    events = data["events"]
    _row("retenção", f"{events['retention_days']} dias")
    _row("dedup", f"{events['dedup_window_hours']} h")
    _row("payload máx.", f"{events['max_payload_bytes']} bytes")
    _row("api", f"{data['api']['host']}:{data['api']['port']}")
    return 0


def _api_is_nexus(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            report = json.loads(response.read(16_384))
        return report.get("name") == "NEXUS"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return False


def _cmd_desktop(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    host = f"[{config.api.host}]" if ":" in config.api.host else config.api.host
    base_url = f"http://{host}:{config.api.port}"
    health_url = f"{base_url}/health"
    if not _api_is_nexus(health_url):
        if args.no_start:
            raise RuntimeError("API NEXUS indisponível; inicie `nexus serve` ou remova --no-start")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(config.paths.project_root)
        environment["PYTHONUNBUFFERED"] = "1"
        command = [
            sys.executable, "-m", "nexus_core", "serve", "--config", str(config.source)
        ]
        child = subprocess.Popen(
            command,
            cwd=config.paths.project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.poll() is None:
            if _api_is_nexus(health_url):
                break
            time.sleep(0.2)
        if not _api_is_nexus(health_url):
            raise RuntimeError(
                f"API não iniciou (processo {child.pid}); consulte {config.paths.log_file}"
            )

    url = base_url + "/"
    browser = next(
        (shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser") if shutil.which(name)),
        None,
    )
    if browser:
        subprocess.Popen(
            [browser, f"--app={url}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    else:
        webbrowser.open_new(url)
    print(f"Painel aberto em {url}")
    print("Cole o token local na tela; obtenha-o com `nexus token show`.")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.lines < 1 or args.lines > 10_000:
        raise ValueError("--lines deve ficar entre 1 e 10000")
    path = config.paths.log_file
    if not path.is_file():
        print(f"log ainda não existe: {path}")
        return 0
    # Bound the read so a large active log cannot consume unbounded memory.
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 2_000_000))
        content = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in content[-args.lines :]:
        print(line)
    return 0


def _cmd_tui(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("nexus tui exige um terminal interativo")
    from .tui import run_tui

    run_tui(runtime)
    return 0


def _cmd_run(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(serve(runtime, interval=args.interval, iterations=args.iterations))

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, task.cancel)
        except (NotImplementedError, RuntimeError):  # plataformas sem suporte
            pass

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    return 0


def _cmd_serve(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    try:
        from .api.server import run_server
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError(
            "dependências da API ausentes (fastapi/uvicorn/websockets). "
            "Instale com '.venv/bin/pip install -e .' ou use 'nexus run' sem API."
        ) from exc
    run_server(runtime, host=args.host, port=args.port)
    return 0


def _cmd_llm_models(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    async def query() -> dict[str, Any]:
        try:
            return await runtime.llm_gateway.models()
        finally:
            await runtime.llm_gateway.aclose()

    data = asyncio.run(query())
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for item in data["providers"]:
        health = item["health"]
        state = "online" if health.get("ok") else str(health.get("state") or "unavailable")
        models = ", ".join(item["discovered_models"] or item["configured_models"]) or "-"
        _row(item["name"], f"{state}; modelos: {models}")
    return 0


def _cmd_llm_complete(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    from .llm_router.gateway import GatewayError

    if args.prompt is None and sys.stdin.isatty():
        raise ValueError("forneça --prompt ou envie o texto pela entrada padrão")
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        raise ValueError("forneça --prompt ou envie o texto pela entrada padrão")

    async def complete() -> dict[str, Any]:
        try:
            result = await runtime.llm_gateway.complete(
                prompt,
                task=args.task,
                system=args.system,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                json_mode=args.json_mode,
                provider_name=args.provider,
                model_name=args.model,
            )
            metadata = result.as_dict()
            await runtime.recorder.record(
                Event(
                    kind="llm.completion.completed",
                    source="llm_gateway.cli",
                    payload={key: value for key, value in metadata.items() if key != "text"},
                    dedup=False,
                )
            )
            return metadata
        except GatewayError as exc:
            await runtime.recorder.record(
                Event(
                    kind="llm.completion.failed",
                    source="llm_gateway.cli",
                    severity=Severity.WARNING,
                    payload={"task": args.task, "error": str(exc)[:512]},
                    dedup=False,
                )
            )
            raise
        finally:
            await runtime.llm_gateway.aclose()

    result = asyncio.run(complete())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["text"])
        print(
            f"\n[{result['provider']}/{result['model']} · {result['latency_ms']} ms · "
            f"tokens {result['prompt_tokens']}/{result['completion_tokens']} · "
            f"custo estimado: {result['estimated_cost_usd']}]",
            file=sys.stderr,
        )
    return 0


def _cmd_task_run(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    if args.prompt is None and sys.stdin.isatty():
        raise ValueError("forneça --prompt ou envie o texto pela entrada padrão")
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        raise ValueError("prompt vazio")

    async def run() -> dict[str, Any]:
        try:
            result = await runtime.cognitive.run(
                prompt,
                task=args.task,
                system=args.system,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                json_mode=args.json_mode,
                provider_name=args.provider,
                model_name=args.model,
            )
            return result.as_dict()
        finally:
            await runtime.llm_gateway.aclose()

    result = asyncio.run(run())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["completion"]["text"])
        print(f"\ntask_id: {result['task_id']}", file=sys.stderr)
    return 0


def _cmd_memory_list(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    records = runtime.memory.list(
        kind=args.kind,
        limit=args.limit,
        offset=args.offset,
        include_expired=args.include_expired,
    )
    if args.json:
        print(json.dumps([record.as_dict() for record in records], ensure_ascii=False, indent=2))
    else:
        for record in records:
            preview = record.content.replace("\n", " ")
            if len(preview) > 80:
                preview = preview[:79] + "…"
            print(f"{record.memory_id}  {record.kind:<10} {record.title or '-'}  {preview}")
        print(f"{len(records)} memória(s)")
    return 0


def _cmd_memory_search(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    records = runtime.memory.search(args.query, kind=args.kind, limit=args.limit)
    if args.json:
        print(json.dumps([record.as_dict() for record in records], ensure_ascii=False, indent=2))
    else:
        for record in records:
            print(f"{record.memory_id}  {record.kind:<10} {record.title or '-'}\n{record.content}\n")
    return 0


def _cmd_memory_add(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    if sys.stdin.isatty():
        raise ValueError("envie o conteúdo da memória pela entrada padrão")
    content = sys.stdin.read()
    metadata = json.loads(args.metadata)
    if not isinstance(metadata, dict):
        raise ValueError("--metadata deve ser um objeto JSON")
    record = runtime.memory.create(
        kind=args.kind,
        title=args.title,
        content=content,
        metadata=metadata,
        expires_at=parse_timestamp(args.expires_at, "--expires-at") if args.expires_at else None,
    )
    asyncio.run(
        runtime.recorder.record(
            Event(
                kind="memory.created",
                source="memory.cli",
                payload={"memory_id": record.memory_id, "kind": record.kind},
                dedup=False,
            )
        )
    )
    print(record.memory_id)
    return 0


def _cmd_memory_delete(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    record = runtime.memory.get(args.memory_id)
    if record is None:
        raise ValueError(f"memória não encontrada: {args.memory_id}")
    runtime.memory.delete(args.memory_id)
    asyncio.run(
        runtime.recorder.record(
            Event(
                kind="memory.deleted",
                source="memory.cli",
                payload={"memory_id": record.memory_id, "kind": record.kind},
                dedup=False,
            )
        )
    )
    print(f"memória apagada: {args.memory_id}")
    return 0


def _cmd_memory_purge_expired(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    removed = runtime.memory.purge_expired()
    asyncio.run(
        runtime.recorder.record(
            Event(
                kind="memory.expired.purged",
                source="memory.cli",
                payload={"count": removed},
                dedup=False,
            )
        )
    )
    print(f"{removed} memória(s) expirada(s) removida(s)")
    return 0


def _cmd_memory_export(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    destination = Path(args.to).expanduser()
    if not destination.parent.is_dir():
        raise ValueError("o diretório de destino precisa existir")
    ensure_private_file(destination)
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags)
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("[\n")
            offset = 0
            while True:
                records = runtime.memory.list(
                    limit=200,
                    offset=offset,
                    include_expired=args.include_expired,
                )
                if not records:
                    break
                for record in records:
                    if count:
                        handle.write(",\n")
                    json.dump(record.as_dict(), handle, ensure_ascii=False, indent=2)
                    count += 1
                offset += len(records)
            handle.write("\n]\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    os.chmod(destination, 0o600, follow_symlinks=False)
    asyncio.run(
        runtime.recorder.record(
            Event(
                kind="memory.exported",
                source="memory.cli",
                payload={"count": count},
                dedup=False,
            )
        )
    )
    print(f"{count} memória(s) exportada(s) para {destination} (0600)")
    return 0


def _cmd_diagnostics_list(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    items = runtime.automation.actions()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for item in items:
            state = "disponível" if item["allowed"] else "exige autonomia nível 2"
            _row(item["action_id"], f"{item['description']} ({state})")
    return 0


def _cmd_diagnostics_run(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    result = asyncio.run(runtime.automation.run(args.action_id))
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        _row("código de saída", str(result.exit_code))
        _row("duração", f"{result.duration_ms} ms")
    return 0 if result.exit_code == 0 else 1


def _cmd_token(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    from .api.auth import ENV_API_TOKEN, resolve_token, rotate_token_file

    path = runtime.config.api.token_file
    if args.token_command == "path":
        print(path)
        return 0

    if args.token_command == "rotate":
        if os.environ.get(ENV_API_TOKEN):
            print(
                f"atenção: {ENV_API_TOKEN} está definido e tem precedência sobre {path}",
                file=sys.stderr,
            )
        token = rotate_token_file(path)
        print(f"novo token: {token}")
        print(f"arquivo: {path} (permissão 0600)")
        return 0

    info = resolve_token(runtime.config)
    if args.json:
        print(
            json.dumps(
                {
                    "token": info.token,
                    "source": info.source,
                    "path": str(info.path) if info.path else None,
                    "created": info.created,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(info.token)
    print(
        f"origem: {info.source}" + (f" ({info.path})" if info.path else ""),
        file=sys.stderr,
    )
    return 0


def _cmd_events_list(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    events = runtime.store.query(
        kind=args.kind,
        source=args.source,
        severity=args.severity,
        since=parse_timestamp(args.since, "--since") if args.since else None,
        correlation_id=args.correlation_id,
        limit=args.limit,
        offset=args.offset,
        oldest_first=args.oldest_first,
    )
    if args.json:
        print(json.dumps([event.to_dict() for event in events], ensure_ascii=False, indent=2))
        return 0
    if not events:
        print("nenhum evento encontrado")
        return 0
    for event in events:
        print(
            f"{event.created_at.isoformat(timespec='seconds')} "
            f"{event.severity.value:<8} {event.kind:<28} {event.source:<16} "
            f"{event.event_id[:8]}  {_payload_preview(event)}"
        )
    print(f"\n{len(events)} evento(s) exibido(s)")
    return 0


def _cmd_events_emit(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--payload não é JSON válido: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--payload deve ser um objeto JSON")
    event = Event(
        kind=args.kind,
        source=args.source,
        payload=payload,
        severity=Severity(args.severity),
        correlation_id=args.correlation_id,
    )
    stored = asyncio.run(runtime.recorder.record(event))
    if args.json:
        print(json.dumps(stored.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"evento registrado: {stored.kind} ({stored.event_id})")
    return 0


def _cmd_events_verify(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    report = runtime.store.verify()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _row("integridade", report["integrity"])
        _row("schema", f"v{report['schema_version']}")
        _row("eventos", str(report["events"]))
        _row("payloads inválidos", str(report["invalid_payloads"]))
        _row("tamanho", _human_bytes(report["size_bytes"]))
        print(f"resultado: {'ok' if report['ok'] else 'falha'}")
    return 0 if report["ok"] else 1


def _cmd_events_kinds(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    kinds = runtime.store.list_kinds()
    if args.json:
        print(json.dumps(kinds, ensure_ascii=False, indent=2))
        return 0
    if not kinds:
        print("nenhum evento registrado")
        return 0
    width = max(len(item["kind"]) for item in kinds)
    for item in kinds:
        print(f"{item['kind']:<{width}}  {item['count']}")
    return 0


def _cmd_events_purge(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    if args.days is None:
        removed = runtime.store.apply_retention()
        scope = f"retenção configurada ({runtime.config.events.retention_days} dias)"
    else:
        if args.days < 0:
            raise ValueError("--days não pode ser negativo")
        cutoff = datetime.now(timezone.utc).replace(microsecond=0)
        if args.days == 0:
            removed = 0
        else:
            from datetime import timedelta

            removed = runtime.store.purge(older_than=cutoff - timedelta(days=args.days))
        scope = f"mais de {args.days} dias"
    print(f"{removed} evento(s) removido(s) — {scope}")
    return 0


def _cmd_db_migrate(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    pending = runtime.database.pending_migrations()
    applied = runtime.database.migrate()
    if args.json:
        print(json.dumps({"pending": pending, "applied": applied,
                          "schema_version": runtime.database.schema_version()}, indent=2))
    else:
        _row("pendentes", ", ".join(map(str, pending)) or "nenhuma")
        _row("aplicadas", ", ".join(map(str, applied)) or "nenhuma")
        _row("schema", f"v{runtime.database.schema_version()}")
    return 0


def _cmd_db_backup(runtime: NexusRuntime, args: argparse.Namespace) -> int:
    if args.to:
        destination = Path(args.to).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = runtime.config.paths.data_dir / "backups" / f"zeus_events-{stamp}.db"
    target = runtime.database.backup_to(destination)
    if args.json:
        print(json.dumps({"backup": str(target), "size_bytes": target.stat().st_size}, indent=2))
    else:
        print(f"backup criado: {target} ({_human_bytes(target.stat().st_size)})")
    return 0


# --------------------------------------------------------------------------- #
# utilitários de saída
# --------------------------------------------------------------------------- #
def _row(label: str, value: Any, width: int = 20) -> None:
    print(f"  {label + ' ':-<{width}} {value}")


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _payload_preview(event: Event, limit: int = 60) -> str:
    text = json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _dispatch(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command

    if command == "config":
        return _cmd_config(args)
    if command == "desktop":
        return _cmd_desktop(args)
    if command == "logs":
        return _cmd_logs(args)

    runtime = bootstrap(args.config)
    try:
        if command == "status":
            return _cmd_status(runtime, args)
        if command == "tui":
            return _cmd_tui(runtime, args)
        if command == "run":
            return _cmd_run(runtime, args)
        if command == "serve":
            return _cmd_serve(runtime, args)
        if command == "llm":
            if args.llm_command == "models":
                return _cmd_llm_models(runtime, args)
            if args.llm_command == "complete":
                return _cmd_llm_complete(runtime, args)
        if command == "task" and args.task_command == "run":
            return _cmd_task_run(runtime, args)
        if command == "memory":
            handlers = {
                "list": _cmd_memory_list,
                "search": _cmd_memory_search,
                "add": _cmd_memory_add,
                "delete": _cmd_memory_delete,
                "purge-expired": _cmd_memory_purge_expired,
                "export": _cmd_memory_export,
            }
            return handlers[args.memory_command](runtime, args)
        if command == "diagnostics":
            if args.diagnostics_command == "list":
                return _cmd_diagnostics_list(runtime, args)
            if args.diagnostics_command == "run":
                return _cmd_diagnostics_run(runtime, args)
        if command == "token":
            return _cmd_token(runtime, args)
        if command == "events":
            handlers = {
                "list": _cmd_events_list,
                "emit": _cmd_events_emit,
                "verify": _cmd_events_verify,
                "kinds": _cmd_events_kinds,
                "purge": _cmd_events_purge,
            }
            return handlers[args.events_command](runtime, args)
        if command == "db":
            handlers = {"migrate": _cmd_db_migrate, "backup": _cmd_db_backup}
            return handlers[args.db_command](runtime, args)
        raise ValueError(f"comando desconhecido: {command}")
    finally:
        runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except ConfigError as exc:
        print(f"erro de configuração: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrompido", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - fronteira de processo: reporta e sai
        print(f"erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
