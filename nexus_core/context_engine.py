"""Context Engine — construção de representações contextuais.

Responsabilidade: montar um contexto enxuto e relevante para cada requisição
do modelo, a partir de:

* conversa atual
* memória persistente (episódica, semântica, etc.)
* tarefas ativas
* estado do sistema
* eventos históricos
* relações de conhecimento
* documentos relevantes

Regra de projeto: nunca carrega toda a memória em cada requisição do modelo.
Usa recuperação contextual e ranqueamento de relevância.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .memory.records import MemoryRecord, MemoryStore
from .memory.eventstore import EventStore
from .events import Event
from .intent_engine import Intent, IntentType


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """Um bloco de contexto com metadados de relevância."""

    title: str
    content: str
    source: str  # 'memory', 'event', 'task', 'system', 'knowledge', 'document'
    relevance: float  # 0.0 a 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "relevance": self.relevance,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextualRepresentation:
    """Representação contextual pronta para ser injetada no modelo."""

    intent: Intent
    blocks: tuple[ContextBlock, ...]
    total_chars: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.as_dict(),
            "blocks": [b.as_dict() for b in self.blocks],
            "total_chars": self.total_chars,
            "truncated": self.truncated,
        }

    def to_text(self) -> str:
        """Serializa o contexto como texto para inclusão no system prompt."""
        if not self.blocks:
            return ""
        lines = ["Relevant context:"]
        for block in self.blocks:
            lines.append(f"- [{block.source}] {block.title}: {block.content}")
        return "\n".join(lines)


class ContextEngine:
    """Constrói representações contextuais enxutas e relevantes."""

    # Limites de tamanho para evitar encher a janela de contexto.
    MAX_BLOCKS = 8
    MAX_CHARS_PER_BLOCK = 600
    MAX_TOTAL_CHARS = 2400

    def __init__(
        self,
        memory: MemoryStore | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self._memory = memory
        self._event_store = event_store

    async def build(
        self,
        intent: Intent,
        *,
        conversation: str | None = None,
        active_tasks: Mapping[str, Any] | None = None,
        system_state: Mapping[str, Any] | None = None,
        recent_events: tuple[Event, ...] | None = None,
        max_total_chars: int | None = None,
    ) -> ContextualRepresentation:
        """Constrói a representação contextual para a intent dada.

        A relevância de cada bloco é calculada com base na correspondência de
        palavras-chave entre o prompt da intent e o conteúdo do bloco.
        """
        max_total = max_total_chars or self.MAX_TOTAL_CHARS
        blocks: list[ContextBlock] = []

        # 1. Conversa atual (se fornecida).
        if conversation:
            blocks.append(
                ContextBlock(
                    title="current conversation",
                    content=self._truncate(conversation, self.MAX_CHARS_PER_BLOCK),
                    source="conversation",
                    relevance=1.0,
                )
            )

        # 2. Memória persistente (busca semântica/lexical limitada).
        if self._memory is not None:
            query = intent.objective or intent.raw_prompt
            try:
                memories = self._memory.search(query, limit=self.MAX_BLOCKS)
            except Exception:
                memories = []
            for mem in memories[: self.MAX_BLOCKS]:
                blocks.append(
                    ContextBlock(
                        title=mem.title or mem.memory_id[:8],
                        content=self._truncate(mem.content, self.MAX_CHARS_PER_BLOCK),
                        source="memory",
                        relevance=self._relevance(query, mem.content),
                        metadata={"kind": mem.kind, "memory_id": mem.memory_id},
                    )
                )

        # 3. Eventos recentes (se fornecidos).
        if recent_events:
            for event in recent_events[: self.MAX_BLOCKS]:
                payload_preview = self._truncate(
                    str(event.payload), self.MAX_CHARS_PER_BLOCK
                )
                blocks.append(
                    ContextBlock(
                        title=event.kind,
                        content=payload_preview,
                        source="event",
                        relevance=0.6,
                        metadata={
                            "event_id": event.event_id,
                            "created_at": event.created_at.isoformat(),
                        },
                    )
                )

        # 4. Estado do sistema (se fornecido).
        if system_state:
            state_preview = self._truncate(str(system_state), self.MAX_CHARS_PER_BLOCK)
            blocks.append(
                ContextBlock(
                    title="system state",
                    content=state_preview,
                    source="system",
                    relevance=0.5,
                )
            )

        # 5. Tarefas ativas (se fornecidas).
        if active_tasks:
            tasks_preview = self._truncate(str(active_tasks), self.MAX_CHARS_PER_BLOCK)
            blocks.append(
                ContextBlock(
                    title="active tasks",
                    content=tasks_preview,
                    source="task",
                    relevance=0.7,
                )
            )

        # 6. Ranqueamento por relevância e truncamento.
        blocks.sort(key=lambda b: b.relevance, reverse=True)
        selected = blocks[: self.MAX_BLOCKS]

        total_chars = 0
        final_blocks: list[ContextBlock] = []
        truncated = False
        for block in selected:
            block_chars = len(block.title) + len(block.content) + len(block.source)
            if total_chars + block_chars > max_total:
                truncated = True
                break
            final_blocks.append(block)
            total_chars += block_chars

        return ContextualRepresentation(
            intent=intent,
            blocks=tuple(final_blocks),
            total_chars=total_chars,
            truncated=truncated,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _relevance(query: str, content: str) -> float:
        """Ranqueamento simples baseado em sobreposição de palavras-chave."""
        if not query or not content:
            return 0.0
        query_terms = set(
            t for t in re.findall(r"[\w-]+", query.lower()) if len(t) > 2
        )
        content_terms = set(
            t for t in re.findall(r"[\w-]+", content.lower()) if len(t) > 2
        )
        if not query_terms:
            return 0.0
        overlap = len(query_terms & content_terms)
        return min(1.0, overlap / len(query_terms))


__all__ = ["ContextEngine", "ContextualRepresentation", "ContextBlock"]
