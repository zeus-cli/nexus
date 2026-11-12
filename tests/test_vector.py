"""Testes do VectorStore: compatibilidade de API e escape de filtros."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_core.memory.vector import VectorStore


def test_table_names_supports_new_and_old_lancedb_apis() -> None:
    class Listing:
        tables = ["memories"]

    class NewDatabase:
        def list_tables(self):
            return Listing()

        def table_names(self):  # pragma: no cover - não deve ser chamado
            raise AssertionError("API antiga não deveria ser usada quando há list_tables")

    class OldDatabase:
        def table_names(self):
            return ["memories"]

    assert VectorStore._table_names(NewDatabase()) == {"memories"}
    assert VectorStore._table_names(OldDatabase()) == {"memories"}


def test_literal_escapes_single_quotes() -> None:
    assert VectorStore._literal("abc") == "'abc'"
    assert VectorStore._literal("a'b") == "'a''b'"
    assert VectorStore._literal("a\\b") == "'a\\b'"


def test_upsert_replaces_existing_and_delete_escapes_id(tmp_path) -> None:
    pytest.importorskip("lancedb")

    store = VectorStore(str(tmp_path / "vectors"))
    store.embed = lambda text: np.array([1.0, 0.0], dtype=np.float32)  # type: ignore[method-assign]

    # ID com aspas e backslash exercita o escape do filtro SQL.
    memory_id = "mem-1'o\\b"
    store.upsert(memory_id, "primeira versão")
    store.upsert(memory_id, "segunda versão")

    assert store._db is not None
    table = store._db.open_table("nexus_memories")
    assert table.count_rows() == 1
    assert table.to_pandas().iloc[0]["memory_id"] == memory_id

    store.delete(memory_id)
    # O handle aberto antes do delete continua vendo a versão antiga da tabela.
    table = store._db.open_table("nexus_memories")
    assert table.count_rows() == 0
