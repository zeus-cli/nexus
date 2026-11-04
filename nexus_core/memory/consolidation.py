
\"\"\"Consolidation pipeline: transform episodic patterns into semantic knowledge.\"\"\"

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence
from dataclasses import dataclass

from .records import MemoryStore, MemoryRecord
from .store import Database
from ..events import Event, Severity
from ..recorder import EventRecorder

@dataclass(frozen=True, slots=True)
class ConsolidationProposal:
    proposal_id: str
    source_ids: tuple[str, ...]
    suggested_title: str
    suggested_content: str
    confidence: float

class ConsolidationEngine:
    \"\"\"Analyzes episodic memories to propose semantic facts.\"\"\"

    def __init__(
        self,
        memory: MemoryStore,
        recorder: EventRecorder,
        *,
        min_recurrence: int = 3,
        confidence_threshold: float = 0.7,
    ) -> None:
        self._memory = memory
        self._recorder = recorder
        self._min_recurrence = min_recurrence
        self._confidence_threshold = confidence_threshold

    async def consolidate_episodic(self, kind: str = \"episodic\") -> int:
        \"\"\"Scan episodic memories for patterns and propose semantic facts.\"\"\"
        # 1. Retrieve recent episodic memories
        records = self._memory.list(kind=kind, limit=200)
        if not records:
            return 0

        # 2. Clustering Logic (Simplified for initial phase)
        # In a full version, we'd use the VectorStore to find semantic clusters.
        # For now, we'll use a basic frequency analysis of metadata/titles.
        clusters: dict[str, list[MemoryRecord]] = {}
        for rec in records:
            # We use a simple key based on title or a fragment of content
            key = rec.title if rec.title else rec.content[:50]
            clusters.setdefault(key, []).append(rec)

        proposals_created = 0
        for key, cluster in clusters.items():
            if len(cluster) >= self._min_recurrence:
                # 3. Propose a semantic fact
                proposal_id = uuid.uuid4().hex
                source_ids = tuple(r.memory_id for r in cluster)
                
                # We record the proposal as an event so the user can confirm it
                await self._recorder.record(
                    Event(
                        kind=\"memory.consolidation.proposed\",
                        source=\"consolidation_engine\",
                        payload={
                            \"proposal_id\": proposal_id,
                            \"suggested_title\": key,
                            \"suggested_content\": f\"Pattern identified from {len(cluster)} episodes.\",
                            \"source_ids\": list(source_ids),
                            \"confidence\": 0.8,
                        },
                        dedup=False,
                    )
                )
                proposals_created += 1

        return proposals_created

    async def confirm_proposal(self, proposal_id: str, title: str, content: str, source_ids: Sequence[str]) -> str:
        \"\"\"Convert a proposal into a confirmed semantic memory.\"\"\"
        # Create the final semantic record
        record = self._memory.create(
            kind=\"semantic\",
            title=title,
            content=content,
            metadata={\"consolidated_from\": list(source_ids)},
            # Semantic memories are usually long-term
        )
        return record.memory_id
