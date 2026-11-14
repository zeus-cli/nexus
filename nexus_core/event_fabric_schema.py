"""Schema SQL do Event Fabric, compartilhado por migração e outbox.

O outbox, a dead-letter queue e a tabela de consumidores idempotentes vivem no
mesmo banco SQLite do EventStore. Isso permite que ``append`` e ``enqueue``
participem da mesma transação, fechando o risco conhecido do ADR 0012 (dois
bancos sem atomicidade).

As instruções são idempotentes (``IF NOT EXISTS``) para que o
``TransactionalOutbox`` também possa abrir bancos independentes em testes e
ferramentas de inspeção.
"""

from __future__ import annotations

EVENT_FABRIC_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS outbox (
        entry_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        severity TEXT NOT NULL,
        payload TEXT NOT NULL,
        dedup_key TEXT,
        correlation_id TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        next_retry_at REAL,
        last_error TEXT,
        recorded_at REAL NOT NULL,
        delivered_at REAL,
        metadata TEXT,
        dedup INTEGER NOT NULL DEFAULT 1
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS ix_outbox_status_next_retry "
        "ON outbox(status, next_retry_at)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_outbox_event_id ON outbox(event_id)",
    """
    CREATE TABLE IF NOT EXISTS dead_letter (
        entry_id TEXT PRIMARY KEY,
        original_entry_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        payload TEXT NOT NULL,
        reason TEXT NOT NULL,
        failed_attempts INTEGER NOT NULL,
        created_at REAL NOT NULL,
        dedup_key TEXT,
        correlation_id TEXT,
        event_created_at TEXT,
        metadata TEXT,
        dedup INTEGER NOT NULL DEFAULT 1
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS ix_dead_letter_created "
        "ON dead_letter(created_at)"
    ),
    """
    CREATE TABLE IF NOT EXISTS consumer_processed (
        consumer_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        dedup_key TEXT,
        processed_at REAL NOT NULL,
        PRIMARY KEY (consumer_id, event_id)
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS ix_consumer_processed_dedup "
        "ON consumer_processed(consumer_id, dedup_key, processed_at)"
    ),
)


__all__ = ["EVENT_FABRIC_STATEMENTS"]
