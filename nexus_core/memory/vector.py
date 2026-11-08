"""Vector-based semantic memory store using LanceDB and Sentence-Transformers.

O store vetorial é opcional: se o modelo de embeddings ou o LanceDB não
estiverem disponíveis, as operações degradam graciosamente para não travar
o bootstrap nem a criação/leitura de memórias.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np


@dataclass(frozen=True, slots=True)
class VectorResult:
    memory_id: str
    score: float
    vector: np.ndarray | None = None


class VectorStore:
    """Local-first vector store for semantic retrieval.

    Todas as operações de carregamento de modelo/banco usam um timeout para
    evitar travamentos quando o ambiente não tem o modelo em cache nem acesso
    à internet para baixá-lo.
    """

    EMBEDDING_LOAD_TIMEOUT_S = 5.0

    def __init__(
        self,
        data_dir: str,
        model_name: str = "all-MiniLM-L6-v2",
        index_name: str = "nexus_memories",
    ) -> None:
        self._data_dir = data_dir
        self._model_name = model_name
        self._table_name = index_name
        self._db: Any | None = None
        self._model: Any | None = None
        self._available = True
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Carregamento com timeout
    # ------------------------------------------------------------------ #

    def _connect(self):
        if self._db is not None:
            return self._db
        if not self._available:
            return None
        try:
            import lancedb

            self._db = lancedb.connect(self._data_dir)
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False
            self._db = None
        return self._db

    def _embedder(self):
        if self._model is not None:
            return self._model
        if not self._available:
            return None

        with self._load_lock:
            if self._model is not None:
                return self._model
            if not self._available:
                return None

            result_holder: list[Any] = []
            error_holder: list[BaseException] = []

            def _load() -> None:
                try:
                    from sentence_transformers import SentenceTransformer

                    result_holder.append(SentenceTransformer(self._model_name))
                except BaseException as exc:  # noqa: BLE001
                    error_holder.append(exc)

            thread = threading.Thread(target=_load, daemon=True)
            thread.start()
            thread.join(timeout=self.EMBEDDING_LOAD_TIMEOUT_S)

            if thread.is_alive():
                # Modelo não carregou no tempo limite: desabilita o store vetorial.
                self._available = False
                self._model = None
                return None

            if error_holder:
                self._available = False
                self._model = None
                return None

            self._model = result_holder[0]
            return self._model

    def embed(self, text: str) -> np.ndarray:
        """Generate a vector embedding for the given text.

        Raises ``RuntimeError`` se o store vetorial estiver indisponível.
        """
        model = self._embedder()
        if model is None:
            raise RuntimeError("vector store indisponível (modelo de embeddings não carregado)")
        return model.encode(text)

    # ------------------------------------------------------------------ #
    # Operações CRUD
    # ------------------------------------------------------------------ #

    def upsert(self, memory_id: str, text: str) -> None:
        """Add or update a vector for a specific memory ID.

        Silenciosamente ignorado se o store vetorial estiver indisponível.
        """
        if not self._available:
            return
        try:
            vector = self.embed(text)
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False
            return
        database = self._connect()
        if database is None:
            return
        try:
            if self._table_name not in database.table_names():
                table = database.create_table(
                    self._table_name, [{"memory_id": memory_id, "vector": vector}]
                )
            else:
                table = database.open_table(self._table_name)
                table.add([{"memory_id": memory_id, "vector": vector}])
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False

    def search(self, query_text: str, limit: int = 10) -> list[VectorResult]:
        """Semantic search returning the most relevant memory IDs.

        Retorna lista vazia se o store vetorial estiver indisponível.
        """
        if not self._available:
            return []
        database = self._connect()
        if database is None:
            return []
        try:
            if self._table_name not in database.table_names():
                return []
            query_vec = self.embed(query_text)
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False
            return []
        try:
            table = database.open_table(self._table_name)
            results = table.search(query_vec).limit(limit).to_pandas()
            return [
                VectorResult(memory_id=row["memory_id"], score=float(row["_distance"]))
                for _, row in results.iterrows()
            ]
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False
            return []

    def delete(self, memory_id: str) -> None:
        """Remove a vector from the store.

        Silenciosamente ignorado se o store vetorial estiver indisponível.
        """
        if not self._available:
            return
        database = self._connect()
        if database is None:
            return
        try:
            if self._table_name in database.table_names():
                table = database.open_table(self._table_name)
                table.delete(f"memory_id = '{memory_id}'")
        except Exception:  # noqa: BLE001 - vetor é opcional
            self._available = False

    def aclose(self) -> None:
        pass
