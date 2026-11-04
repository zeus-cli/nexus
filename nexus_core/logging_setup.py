"""Logging estruturado do NEXUS.

Um único ponto de configuração: arquivo rotativo (JSON por padrão) + console.
Uso: ``configure_logging(config.logging, config.paths.log_file)`` e depois
``get_logger("memory")`` para obter ``zeus.memory``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import LoggingConfig
from .privatefs import ensure_private_file

LOGGER_ROOT = "zeus"

_RESERVED_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Serializa cada registro como uma linha JSON (formato de ingestão)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Formato legível para operação em terminal."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that creates every log segment with mode 0600."""

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= os.O_APPEND if "a" in self.mode else os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(
            descriptor,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
        )


def configure_logging(cfg: LoggingConfig, log_file: Path | None, *,
                      console: bool = True) -> logging.Logger:
    """Configura o logger raiz ``zeus`` de forma idempotente."""
    root = logging.getLogger(LOGGER_ROOT)
    root.setLevel(cfg.level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_formatter: logging.Formatter = JsonFormatter() if cfg.json_logs else TextFormatter()
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_private_file(log_file)
        file_handler = PrivateRotatingFileHandler(
            log_file,
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(TextFormatter())
        root.addHandler(console_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """Retorna ``zeus.<name>`` (nunca o logger raiz da aplicação)."""
    return logging.getLogger(f"{LOGGER_ROOT}.{name}")
