"""Vector-based semantic memory store using LanceDB and Sentence-Transformers."""

from __future__ import annotations

import lancedb
import numpy as np
from sentence_transformers import SentenceTransformer
from typing import Any, Sequence, Optional
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class VectorResult:
    memory_id: str
    score: float
    vector: np.ndarray | None = None

class VectorStore:
    """Local-first vector store for semantic retrieval."""

    def __init__(
        self, 
        data_dir: str, 
        model_name: str = "all-MiniLM-L6-v2",
        index_name: str = "nexus_memories"
    ) -> None:
        self._db = lancedb.connect(data_dir)
        self._model = SentenceTransformer(model_name)
        self._table_name = index_name
        
    def embed(self, text: str) -> np.ndarray:
        """Generate a vector embedding for the given text."""
        return self._model.encode(text)

    def upsert(self, memory_id: str, text: str) -> None:
        """Add or update a vector for a specific memory ID."""
        vector = self.embed(text)
        
        if self._table_name not in self._db.table_names():
            # Create table with the first record
            table = self._db.create_table(self._table_name, [{"memory_id": memory_id, "vector": vector}])
        else:
            table = self._db.open_table(self._table_name)
            # For a simple implementation, we append. 
            # LanceDB search will return the most recent/closest.
            table.add([{"memory_id": memory_id, "vector": vector}])

    def search(self, query_text: str, limit: int = 10) -> list[VectorResult]:
        """Semantic search returning the most relevant memory IDs."""
        if self._table_name not in self._db.table_names():
            return []
            
        query_vec = self.embed(query_text)
        table = self._db.open_table(self._table_name)
        
        results = table.search(query_vec).limit(limit).to_pandas()
        
        return [
            VectorResult(
                memory_id=row["memory_id"],
                score=float(row["_distance"])
            )
            for _, row in results.iterrows()
        ]

    def delete(self, memory_id: str) -> None:
        """Remove a vector from the store."""
        if self._table_name in self._db.table_names():
            table = self._db.open_table(self._table_name)
            table.delete("memory_id = '" + memory_id + "'")

    def aclose(self) -> None:
        pass
