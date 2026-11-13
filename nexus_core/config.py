"""Configuração central do NEXUS.

Precedência (menor para maior): defaults embutidos -> arquivo TOML -> variáveis
de ambiente. Chaves desconhecidas são erro, não aviso, para que erros de
digitação não passem silenciosamente.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_CONFIG_NAME = "nexus.toml"
DEFAULT_CONFIG_RELPATH = Path("config") / DEFAULT_CONFIG_NAME

ENV_CONFIG_FILE = "NEXUS_CONFIG"
ENV_DATA_DIR = "NEXUS_DATA_DIR"
ENV_LOG_LEVEL = "NEXUS_LOG_LEVEL"
ENV_AUTONOMY_LEVEL = "NEXUS_AUTONOMY_LEVEL"

AUTONOMY_LEVELS: dict[int, str] = {
    0: "observation",
    1: "assistance",
    2: "controlled_execution",
    3: "privileged_execution",
    4: "limited_autonomy",
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

LLM_KINDS = ("ollama", "openai_compatible")
LLM_TASKS = ("reasoning", "planning", "summary", "synthesis")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

_TOP_LEVEL_KEYS = ("nexus", "paths", "logging", "events", "api", "safety", "llm")


class ConfigError(Exception):
    """Configuração inválida, ilegível ou incompleta."""


@dataclass(frozen=True, slots=True)
class PathsConfig:
    project_root: Path
    data_dir: Path
    database: Path
    log_file: Path

    @property
    def approvals_db(self) -> Path:
        return self.data_dir / "databases" / "nexus_approvals.db"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    json_logs: bool = True
    max_bytes: int = 5_242_880
    backup_count: int = 5


@dataclass(frozen=True, slots=True)
class EventsConfig:
    retention_days: int = 0
    dedup_window_hours: int = 24
    max_payload_bytes: int = 65_536
    query_limit: int = 500


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token_file: Path = Path("data/secrets/api_token")


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    autonomy_level: int = 0


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provedor de LLM declarado explicitamente na configuração."""

    name: str
    kind: str
    base_url: str = ""
    models: tuple[str, ...] = ()
    enabled: bool = False
    authorized: bool = False
    api_key_env: str | None = None
    timeout_s: float = 60.0
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


@dataclass(frozen=True, slots=True)
class ChainLink:
    """Entrada da cadeia de roteamento (com ``task`` opcional para especializar)."""

    provider: str
    model: str
    task: str | None = None


@dataclass(frozen=True, slots=True)
class LlmConfig:
    providers: tuple[ProviderConfig, ...] = ()
    chain: tuple[ChainLink, ...] = ()
    max_context_tokens: int = 8192
    failure_threshold: int = 3
    cooldown_s: float = 60.0
    max_retries: int = 1

    def provider(self, name: str) -> ProviderConfig | None:
        for item in self.providers:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True, slots=True)
class Config:
    paths: PathsConfig
    logging: LoggingConfig
    events: EventsConfig
    api: ApiConfig
    safety: SafetyConfig
    llm: LlmConfig = field(default_factory=LlmConfig)
    environment: str = "production"
    source: Path | None = None

    @property
    def autonomy_name(self) -> str:
        return AUTONOMY_LEVELS[self.safety.autonomy_level]

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "source": str(self.source) if self.source else None,
            "autonomy": {"level": self.safety.autonomy_level, "name": self.autonomy_name},
            "paths": {
                "project_root": str(self.paths.project_root),
                "data_dir": str(self.paths.data_dir),
                "database": str(self.paths.database),
                "log_file": str(self.paths.log_file),
            },
            "logging": {
                "level": self.logging.level,
                "json_logs": self.logging.json_logs,
                "max_bytes": self.logging.max_bytes,
                "backup_count": self.logging.backup_count,
            },
            "events": {
                "retention_days": self.events.retention_days,
                "dedup_window_hours": self.events.dedup_window_hours,
                "max_payload_bytes": self.events.max_payload_bytes,
                "query_limit": self.events.query_limit,
            },
            "api": {
                "host": self.api.host,
                "port": self.api.port,
                "token_file": str(self.api.token_file),
            },
            "llm": {
                "max_context_tokens": self.llm.max_context_tokens,
                "failure_threshold": self.llm.failure_threshold,
                "cooldown_s": self.llm.cooldown_s,
                "max_retries": self.llm.max_retries,
                "providers": [
                    {
                        "name": item.name,
                        "kind": item.kind,
                        "base_url": item.base_url,
                        "models": list(item.models),
                        "enabled": item.enabled,
                        "authorized": item.authorized,
                        "api_key_env": item.api_key_env,
                        "timeout_s": item.timeout_s,
                        "input_cost_per_million": item.input_cost_per_million,
                        "output_cost_per_million": item.output_cost_per_million,
                    }
                    for item in self.llm.providers
                ],
                "chain": [
                    {"provider": link.provider, "model": link.model, "task": link.task}
                    for link in self.llm.chain
                ],
            },
        }


def find_project_root(start: Path | None = None) -> Path:
    """Sobe a partir do pacote (ou de ``start``) até achar ``config/nexus.toml``."""
    origin = Path(start) if start is not None else Path(__file__).resolve()
    origin = origin.resolve()
    candidates = [origin, *origin.parents] if origin.is_dir() else [origin.parent, *origin.parents]
    for candidate in candidates:
        if (candidate / DEFAULT_CONFIG_RELPATH).is_file():
            return candidate
    raise ConfigError(
        f"não foi possível localizar {DEFAULT_CONFIG_RELPATH} a partir de {origin}; "
        f"defina {ENV_CONFIG_FILE} apontando para o arquivo de configuração"
    )


def find_config_file(explicit: str | Path | None = None, start: Path | None = None) -> Path:
    raw = explicit or os.environ.get(ENV_CONFIG_FILE)
    if raw:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"arquivo de configuração não encontrado: {path}")
        return path
    return (find_project_root(start) / DEFAULT_CONFIG_RELPATH)


def load_config(explicit_path: str | Path | None = None, *, start: Path | None = None) -> Config:
    path = find_config_file(explicit_path, start)
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML inválido em {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"falha ao ler {path}: {exc}") from exc
    return config_from_mapping(raw, source=path)


def config_from_mapping(raw: Mapping[str, Any], *, source: Path | None = None,
                        env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    unknown_top = set(raw) - set(_TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigError(f"chaves de topo desconhecidas: {', '.join(sorted(unknown_top))}")

    nexus_section = _section(raw, "nexus", ("name", "environment"))
    environment = _as_str(nexus_section.get("environment", "production"), "nexus.environment")

    if source is not None:
        # Um arquivo explícito fora de ``config/`` deve resolver caminhos
        # relativos ao seu próprio diretório; exigir a descoberta do projeto
        # impediria configurações como ``NEXUS_CONFIG=/etc/nexus.toml``.
        project_root = (
            source.parent.parent if source.parent.name == "config" else source.parent
        )
    else:
        project_root = find_project_root()
    project_root = project_root.resolve()

    paths_section = _section(raw, "paths", ("data_dir", "database", "log_file"))
    raw_data_dir = env.get(ENV_DATA_DIR) or paths_section.get("data_dir", "data")
    data_dir = _resolve(project_root, raw_data_dir)
    database = (_resolve(project_root, paths_section["database"])
                if "database" in paths_section
                else data_dir / "databases" / "nexus_events.db")
    log_file = (_resolve(project_root, paths_section["log_file"])
                if "log_file" in paths_section
                else data_dir / "logs" / "nexus.log")

    logging_section = _section(raw, "logging", ("level", "json", "max_bytes", "backup_count"))
    level = _as_str(env.get(ENV_LOG_LEVEL) or logging_section.get("level", "INFO"),
                    "logging.level").upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"logging.level inválido: {level!r} (esperado: {', '.join(LOG_LEVELS)})")

    events_section = _section(
        raw, "events", ("retention_days", "dedup_window_hours", "max_payload_bytes", "query_limit")
    )
    api_section = _section(raw, "api", ("host", "port", "token_file"))
    api_host = _as_str(api_section.get("host", "127.0.0.1"), "api.host")
    if not is_loopback_host(api_host):
        raise ConfigError(
            "api.host deve ser um endereço de loopback (127.0.0.1, ::1 ou localhost); "
            "a API não oferece TLS"
        )
    safety_section = _section(raw, "safety", ("autonomy_level",))

    raw_level = env.get(ENV_AUTONOMY_LEVEL, safety_section.get("autonomy_level", 0))
    try:
        autonomy_level = int(raw_level)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"safety.autonomy_level inválido: {raw_level!r}") from exc
    if autonomy_level not in AUTONOMY_LEVELS:
        raise ConfigError(
            f"safety.autonomy_level deve estar entre 0 e 4 (recebido: {autonomy_level})"
        )

    return Config(
        environment=environment,
        source=source,
        paths=PathsConfig(
            project_root=project_root,
            data_dir=data_dir,
            database=database,
            log_file=log_file,
        ),
        logging=LoggingConfig(
            level=level,
            json_logs=_as_bool(logging_section.get("json", True), "logging.json"),
            max_bytes=_as_int(logging_section.get("max_bytes", 5_242_880), "logging.max_bytes",
                              minimum=0),
            backup_count=_as_int(logging_section.get("backup_count", 5), "logging.backup_count",
                                 minimum=0, maximum=100),
        ),
        events=EventsConfig(
            retention_days=_as_int(events_section.get("retention_days", 0), "events.retention_days",
                                   minimum=0),
            dedup_window_hours=_as_int(events_section.get("dedup_window_hours", 24),
                                       "events.dedup_window_hours", minimum=0, maximum=8760),
            max_payload_bytes=_as_int(events_section.get("max_payload_bytes", 65_536),
                                      "events.max_payload_bytes", minimum=1024),
            query_limit=_as_int(events_section.get("query_limit", 500), "events.query_limit",
                                minimum=1, maximum=100_000),
        ),
        api=ApiConfig(
            host=api_host,
            port=_as_int(api_section.get("port", 8765), "api.port", minimum=1, maximum=65_535),
            token_file=(_resolve(project_root, api_section["token_file"])
                        if "token_file" in api_section
                        else data_dir / "secrets" / "api_token"),
        ),
        safety=SafetyConfig(autonomy_level=autonomy_level),
        llm=_parse_llm(raw),
    )


def is_loopback_host(host: str) -> bool:
    """Retorna verdadeiro para IP de loopback ou nomes ``localhost``.

    O host pode vir de ``api.host`` ou do hostname de uma URL. Aceitar
    ``localhost`` evita que a configuração padrão de ferramentas locais seja
    tratada como acesso remoto (o que exigiria HTTPS e autorização explícita).
    """
    if not isinstance(host, str):
        return False
    normalized = host.strip().lower().rstrip(".")
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _section(raw: Mapping[str, Any], name: str, allowed: tuple[str, ...]) -> Mapping[str, Any]:
    data = raw.get(name, {})
    if not isinstance(data, Mapping):
        raise ConfigError(f"seção [{name}] inválida: esperado uma tabela TOML")
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f"seção [{name}] possui chaves desconhecidas: {', '.join(sorted(unknown))}")
    return data


def _resolve(root: Path, value: Any) -> Path:
    path = Path(_as_str(value, "caminho")).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _as_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} deve ser uma string não vazia (recebido: {value!r})")
    return value.strip()


def _as_int(value: Any, label: str, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} deve ser inteiro (recebido: {value!r})")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{label} deve ser >= {minimum} (recebido: {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{label} deve ser <= {maximum} (recebido: {value})")
    return value


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} deve ser booleano (recebido: {value!r})")
    return value


def _as_float(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} deve ser numérico (recebido: {value!r})")
    if not math.isfinite(float(value)):
        raise ConfigError(f"{label} deve ser finito (recebido: {value!r})")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{label} deve ser >= {minimum} (recebido: {value})")
    return float(value)


def _parse_llm(raw: Mapping[str, Any]) -> LlmConfig:
    """Valida provedores e cadeia de roteamento de LLM.

    Regra de segurança: provedor ``openai_compatible`` só pode ser habilitado
    com ``authorized = true`` explícito (seção 7: nada sai da máquina sem
    autorização).
    """
    section = _section(
        raw,
        "llm",
        ("providers", "chain", "max_context_tokens", "failure_threshold", "cooldown_s", "max_retries"),
    )
    provider_keys = (
        "name", "kind", "base_url", "models", "enabled", "authorized",
        "api_key_env", "timeout_s", "input_cost_per_million", "output_cost_per_million",
    )
    providers_raw = section.get("providers", [])
    if not isinstance(providers_raw, list):
        raise ConfigError("llm.providers deve ser uma lista de tabelas ([[llm.providers]])")

    providers: list[ProviderConfig] = []
    for index, item in enumerate(providers_raw):
        label = f"llm.providers[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{label} inválido: esperado tabela TOML")
        unknown = set(item) - set(provider_keys)
        if unknown:
            raise ConfigError(f"{label} possui chaves desconhecidas: {', '.join(sorted(unknown))}")

        name = _as_str(item.get("name"), f"{label}.name")
        if not _PROVIDER_NAME.match(name):
            raise ConfigError(f"{label}.name inválido: {name!r} (use minúsculas, dígitos, '_' e '-')")
        if any(existing.name == name for existing in providers):
            raise ConfigError(f"provedor duplicado em llm.providers: {name}")

        kind = _as_str(item.get("kind"), f"{label}.kind").lower()
        if kind not in LLM_KINDS:
            raise ConfigError(f"{label}.kind deve ser um de: {', '.join(LLM_KINDS)}")

        enabled = _as_bool(item.get("enabled", False), f"{label}.enabled")
        authorized = _as_bool(item.get("authorized", False), f"{label}.authorized")
        raw_base_url = item.get("base_url", "")
        base_url = _as_str(raw_base_url, f"{label}.base_url") if raw_base_url else ""

        raw_models = item.get("models", [])
        if not isinstance(raw_models, list):
            raise ConfigError(f"{label}.models deve ser uma lista de strings")
        models = tuple(_as_str(model, f"{label}.models[]") for model in raw_models)

        if enabled:
            parsed_url = urlsplit(base_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ConfigError(f"{label}.base_url deve ser uma URL http(s) válida")
            try:
                parsed_url.port
            except ValueError as exc:
                raise ConfigError(f"{label}.base_url possui porta inválida") from exc
            if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
                raise ConfigError(f"{label}.base_url não pode conter credenciais, query ou fragmento")
            if not models:
                raise ConfigError(f"{label}.models não pode ser vazio para provedor habilitado")
            if kind == "openai_compatible" and not authorized:
                raise ConfigError(
                    f"{label}: provedor externo exige authorized = true (autorização explícita)"
                )
            is_local = parsed_url.hostname.lower() == "localhost" or is_loopback_host(
                parsed_url.hostname
            )
            if not is_local and not authorized:
                raise ConfigError(f"{label}: provedor remoto exige authorized = true")
            if not is_local and parsed_url.scheme != "https":
                raise ConfigError(f"{label}: provedor remoto exige HTTPS")

        api_key_env = item.get("api_key_env")
        providers.append(
            ProviderConfig(
                name=name,
                kind=kind,
                base_url=base_url,
                models=models,
                enabled=enabled,
                authorized=authorized,
                api_key_env=_as_str(api_key_env, f"{label}.api_key_env") if api_key_env else None,
                timeout_s=_as_float(item.get("timeout_s", 60.0), f"{label}.timeout_s", minimum=1.0),
                input_cost_per_million=(
                    _as_float(item["input_cost_per_million"], f"{label}.input_cost_per_million",
                              minimum=0.0)
                    if item.get("input_cost_per_million") is not None
                    else None
                ),
                output_cost_per_million=(
                    _as_float(item["output_cost_per_million"], f"{label}.output_cost_per_million",
                              minimum=0.0)
                    if item.get("output_cost_per_million") is not None
                    else None
                ),
            )
        )

    chain_raw = section.get("chain", [])
    if not isinstance(chain_raw, list):
        raise ConfigError("llm.chain deve ser uma lista de tabelas ([[llm.chain]])")
    chain: list[ChainLink] = []
    for index, item in enumerate(chain_raw):
        label = f"llm.chain[{index}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{label} inválido: esperado tabela TOML")
        unknown = set(item) - {"provider", "model", "task"}
        if unknown:
            raise ConfigError(f"{label} possui chaves desconhecidas: {', '.join(sorted(unknown))}")
        provider_name = _as_str(item.get("provider"), f"{label}.provider")
        target = next((entry for entry in providers if entry.name == provider_name), None)
        if target is None:
            raise ConfigError(f"{label}.provider não declarado em llm.providers: {provider_name}")
        model = _as_str(item.get("model"), f"{label}.model")
        if target.models and model not in target.models:
            raise ConfigError(
                f"{label}.model {model!r} não está em llm.providers.{provider_name}.models"
            )
        task = item.get("task")
        if task is not None:
            task = _as_str(task, f"{label}.task").lower()
            if task not in LLM_TASKS:
                raise ConfigError(f"{label}.task deve ser um de: {', '.join(LLM_TASKS)}")
        chain.append(ChainLink(provider=provider_name, model=model, task=task))

    return LlmConfig(
        providers=tuple(providers),
        chain=tuple(chain),
        max_context_tokens=_as_int(
            section.get("max_context_tokens", 8192), "llm.max_context_tokens", minimum=256
        ),
        failure_threshold=_as_int(
            section.get("failure_threshold", 3), "llm.failure_threshold", minimum=1, maximum=10
        ),
        cooldown_s=_as_float(section.get("cooldown_s", 60.0), "llm.cooldown_s", minimum=0.0),
        max_retries=_as_int(section.get("max_retries", 1), "llm.max_retries", minimum=0, maximum=3),
    )
