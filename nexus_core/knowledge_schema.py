"""Schema SQL do Knowledge Graph persistente.

Nós e arestas vivem no mesmo SQLite governado do EventStore, com proveniência,
confiança, validade temporal e evidência de validação. As instruções são
idempotentes para que o `KnowledgeGraph` também possa operar em bancos
independentes de teste.
"""

from __future__ import annotations

KNOWLEDGE_GRAPH_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_nodes (
        node_id TEXT PRIMARY KEY,
        node_type TEXT NOT NULL,
        label TEXT NOT NULL,
        properties TEXT NOT NULL DEFAULT '{}',
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        source_ref TEXT,
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        validated_by TEXT,
        validation_evidence TEXT NOT NULL DEFAULT '[]',
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_type_updated "
        "ON knowledge_nodes(node_type, updated_at DESC)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_kind ON knowledge_nodes(kind)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_nodes_expires ON knowledge_nodes(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_edges (
        edge_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id) ON DELETE CASCADE,
        target_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id) ON DELETE CASCADE,
        relation TEXT NOT NULL,
        properties TEXT NOT NULL DEFAULT '{}',
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        source_ref TEXT,
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        validated_by TEXT,
        validation_evidence TEXT NOT NULL DEFAULT '[]',
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_source_relation "
        "ON knowledge_edges(source_id, relation)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_target_relation "
        "ON knowledge_edges(target_id, relation)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_relation ON knowledge_edges(relation)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_edges_expires ON knowledge_edges(expires_at)",
)


__all__ = ["KNOWLEDGE_GRAPH_STATEMENTS"]
