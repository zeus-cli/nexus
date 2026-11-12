"""Abertura de conexões SQLite com fechamento garantido.

O gerenciador de contexto nativo de ``sqlite3.Connection`` faz commit/rollback,
mas não fecha a conexão. Este helper mantém os dois comportamentos e ainda
configura ``busy_timeout`` para reduzir falhas transitórias de lock.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect_db(path: str | Path, *, timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    """Abre uma conexão, confirma no sucesso, desfaz no erro e sempre fecha."""
    connection = sqlite3.connect(str(path), timeout=timeout)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = ["connect_db"]
