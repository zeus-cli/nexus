use super::types::Event;
use rusqlite::{Connection, OptionalExtension};
use std::path::Path;
use std::sync::Mutex;

/// Loja persistente de eventos em SQLite (WAL).
///
/// Espelho mínimo e compatível do schema Python (`events`):
/// `event_id, kind, source, severity, payload, dedup_key, correlation_id, created_at, recorded_at`.
///
/// Todas as operações são serializadas por um `Mutex` (uma única conexão por
/// processo), eliminando `database is locked` sem a necessidade de pool.
pub struct EventStore {
    conn: Mutex<Connection>,
}

impl EventStore {
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 5000;")?;
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload TEXT NOT NULL,
                dedup_key TEXT,
                correlation_id TEXT,
                created_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_kind_created ON events(kind, created_at DESC)",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_dedup ON events(dedup_key, created_at DESC)",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_correlation ON events(correlation_id)",
            [],
        )?;
        Ok(Self { conn: Mutex::new(conn) })
    }

    /// Insere um evento, respeitando dedup dentro da janela configurada.
    /// Retorna `(evento_armazenado, inserido)`.
    pub fn append(&self, event: &Event, dedup_window_seconds: i64) -> Result<(Event, bool), rusqlite::Error> {
        let payload_json = serde_json::to_string(&event.payload).unwrap_or_default();
        let dedup_key = event.dedup_key();

        let conn = self.conn.lock().unwrap();

        // Dedup: evento equivalente dentro da janela já existe?
        if let Some(key) = &dedup_key {
            let cutoff = chrono_utc_minus_seconds(dedup_window_seconds);
            let row: Option<String> = conn
                .prepare("SELECT event_id FROM events WHERE dedup_key = ?1 AND created_at >= ?2 ORDER BY id DESC LIMIT 1")?
                .query_row([key, &cutoff], |r| r.get(0))
                .optional()?;
            if row.is_some() {
                return Ok((event.clone(), false));
            }
        }

        let recorded_at = now_iso();
        conn.execute(
            "INSERT INTO events (event_id, kind, source, severity, payload, dedup_key, correlation_id, created_at, recorded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                event.event_id,
                event.kind,
                event.source,
                event.severity,
                payload_json,
                dedup_key,
                event.correlation_id,
                event.created_at,
                recorded_at,
            ],
        )?;
        Ok((event.clone(), true))
    }

    pub fn count(&self) -> Result<i64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
    }

    pub fn query_by_kind(&self, kind: &str, limit: i64) -> Result<Vec<Event>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT event_id, kind, source, severity, payload, dedup_key, correlation_id, created_at FROM events
             WHERE kind = ?1 ORDER BY created_at DESC, id DESC LIMIT ?2",
        )?;
        let rows = stmt.query_map(rusqlite::params![kind, limit], |r| {
            Ok(Event {
                event_id: r.get(0)?,
                kind: r.get(1)?,
                source: r.get(2)?,
                payload: serde_json::from_str(&r.get::<_, String>(3)?).unwrap_or(serde_json::Value::Null),
                severity: r.get(4)?,
                dedup: r.get::<_, Option<String>>(5)?.is_some(),
                correlation_id: r.get(6)?,
                created_at: r.get(7)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    pub fn integrity_check(&self) -> Result<String, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.query_row("PRAGMA quick_check", [], |r| r.get(0))
    }
}

fn now_iso() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("relógio");
    crate::events::types::chrono_naive_utc(now.as_secs(), now.subsec_nanos())
}

fn chrono_utc_minus_seconds(seconds: i64) -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("relógio");
    let secs = now.as_secs() as i64 - seconds;
    let nanos = now.subsec_nanos();
    crate::events::types::chrono_naive_utc(secs as u64, nanos)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn append_and_count() {
        let store = EventStore::open(":memory:").unwrap();
        let ev = Event::new("system.started", "test", serde_json::json!({"pid": 1}));
        let (_, inserted) = store.append(&ev, 86400).unwrap();
        assert!(inserted);
        assert_eq!(store.count().unwrap(), 1);

        // Segundo evento idêntico dentro da janela não é inserido (dedup).
        let ev2 = Event::new("system.started", "test", serde_json::json!({"pid": 1}));
        let (_, inserted2) = store.append(&ev2, 86400).unwrap();
        assert!(!inserted2);
        assert_eq!(store.count().unwrap(), 1);
    }

    #[test]
    fn query_by_kind() {
        let store = EventStore::open(":memory:").unwrap();
        store.append(&Event::new("system.started", "a", serde_json::json!({})), 86400).unwrap();
        store.append(&Event::new("agent.task.started", "b", serde_json::json!({})), 86400).unwrap();
        let rows = store.query_by_kind("system.started", 10).unwrap();
        assert_eq!(rows.len(), 1);
    }
}
