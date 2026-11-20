//! Persistência durável de aprovações e execuções (T6).
//!
//! O schema é compatível com o SQLite do runtime Python
//! (`data/databases/nexus_approvals.db`): a tabela `approvals` usa `id` como
//! chave e a tabela `executions` usa `approval_id`. O daemon Rust passa a ser
//! a fonte de verdade quando o Control Plane está autoritativo; o Python não
//! deve abrir o mesmo banco em modo de escrita nessa configuração.
//!
//! A conexão é única por processo e serializada por `Mutex`, como no
//! `EventStore`. As transições que afetam aprovação e execução juntas
//! (`claim`, `start` e `complete`) usam transações SQLite para não deixar
//! estados parciais após uma falha.

use crate::contracts::ApprovalRecord;
use crate::control_schema::{migrate, restrict_permissions, unix_now, SchemaError};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::Mutex;

/// Registro do ciclo de execução vinculado a uma aprovação.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionRecord {
    pub approval_id: String,
    pub status: String,
    pub started_at: Option<f64>,
    pub completed_at: Option<f64>,
    pub result_summary: Option<String>,
}

/// Loja persistente de aprovações e execuções em SQLite (WAL).
pub struct ApprovalStore {
    conn: Mutex<Connection>,
}

impl ApprovalStore {
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, SchemaError> {
        let path = path.as_ref();
        let mut conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA busy_timeout = 5000;
             PRAGMA foreign_keys = ON;",
        )?;
        Self::initialize(&mut conn)?;
        restrict_permissions(path);
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    pub fn open_in_memory() -> Result<Self, SchemaError> {
        let mut conn = Connection::open_in_memory()?;
        conn.execute_batch("PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;")?;
        Self::initialize(&mut conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    fn initialize(conn: &mut Connection) -> Result<(), SchemaError> {
        migrate(conn, unix_now())?;
        Ok(())
    }

    pub fn insert_approval(&self, record: &ApprovalRecord) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        conn.execute(
            "INSERT INTO approvals (
                id, task_id, plan_id, tool_id, params_hash, risk_level,
                access_level, execution_mode, fs_scope, net_scope, resource_limits,
                policy_version, justification, status, created_at, expires_at,
                decided_at, decided_by, consumed_at
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)",
            params![
                record.approval_id,
                record.task_id,
                record.plan_id,
                record.tool_id,
                record.params_hash,
                record.risk_level,
                record.access_level,
                record.execution_mode,
                record.fs_scope,
                record.net_scope,
                record.resource_limits,
                record.policy_version,
                record.justification,
                record.status,
                record.created_at,
                record.expires_at,
                record.decided_at,
                record.decided_by,
                record.consumed_at,
            ],
        )?;
        Ok(())
    }

    pub fn get_approval(
        &self,
        approval_id: &str,
    ) -> Result<Option<ApprovalRecord>, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        conn.query_row(
            "SELECT id, tool_id, params_hash, task_id, plan_id, execution_mode,
                    access_level, fs_scope, net_scope, resource_limits, policy_version,
                    justification, status, created_at, expires_at, decided_at,
                    decided_by, consumed_at, risk_level
             FROM approvals WHERE id = ?1",
            params![approval_id],
            row_to_approval,
        )
        .optional()
    }

    pub fn set_decision(
        &self,
        approval_id: &str,
        status: &str,
        decided_at: f64,
        decided_by: &str,
    ) -> Result<bool, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let updated = conn.execute(
            "UPDATE approvals SET status = ?2, decided_at = ?3, decided_by = ?4
             WHERE id = ?1 AND status = 'PENDING'",
            params![approval_id, status, decided_at, decided_by],
        )?;
        Ok(updated > 0)
    }

    pub fn revoke(&self, approval_id: &str) -> Result<bool, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let updated = conn.execute(
            "UPDATE approvals SET status = 'REVOKED'
             WHERE id = ?1 AND status IN ('PENDING', 'APPROVED')",
            params![approval_id],
        )?;
        Ok(updated > 0)
    }

    pub fn refresh_expiry(&self, approval_id: &str, now: f64) -> Result<bool, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let updated = conn.execute(
            "UPDATE approvals SET status = 'EXPIRED'
             WHERE id = ?1 AND status IN ('PENDING', 'APPROVED') AND expires_at <= ?2",
            params![approval_id, now],
        )?;
        Ok(updated > 0)
    }

    pub fn expire_stale(&self, now: f64) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let updated = conn.execute(
            "UPDATE approvals SET status = 'EXPIRED'
             WHERE expires_at <= ?1 AND status IN ('PENDING', 'APPROVED')",
            params![now],
        )?;
        Ok(updated)
    }

    /// Claim atômico: `APPROVED → CLAIMED` + execução `PREPARED`.
    ///
    /// A transação impede o caso de uma aprovação reivindicada sem registro de
    /// execução, que quebraria o ciclo `STARTING → COMPLETED`.
    pub fn claim(&self, approval_id: &str, now: f64) -> Result<bool, rusqlite::Error> {
        let mut conn = self.conn.lock().expect("lock de approvals store");
        let tx = conn.transaction()?;
        let updated = tx.execute(
            "UPDATE approvals SET status = 'CLAIMED'
             WHERE id = ?1 AND status = 'APPROVED'",
            params![approval_id],
        )?;
        if updated == 0 {
            tx.commit()?;
            return Ok(false);
        }
        tx.execute(
            "INSERT INTO executions (approval_id, status, started_at, completed_at, result_summary)
             VALUES (?1, 'PREPARED', ?2, NULL, NULL)
             ON CONFLICT(approval_id) DO UPDATE SET
                status = 'PREPARED',
                started_at = excluded.started_at,
                completed_at = NULL,
                result_summary = NULL",
            params![approval_id, now],
        )?;
        tx.commit()?;
        Ok(true)
    }

    /// Consome a aprovação: `CLAIMED → CONSUMED` + execução `STARTING`.
    pub fn start_execution(&self, approval_id: &str, now: f64) -> Result<bool, rusqlite::Error> {
        let mut conn = self.conn.lock().expect("lock de approvals store");
        let tx = conn.transaction()?;
        let updated = tx.execute(
            "UPDATE approvals SET status = 'CONSUMED', consumed_at = ?2
             WHERE id = ?1 AND status = 'CLAIMED'",
            params![approval_id, now],
        )?;
        if updated == 0 {
            tx.commit()?;
            return Ok(false);
        }
        let execution = tx.execute(
            "UPDATE executions SET status = 'STARTING', started_at = ?2,
                    completed_at = NULL, result_summary = NULL
             WHERE approval_id = ?1",
            params![approval_id, now],
        )?;
        if execution == 0 {
            tx.execute(
                "INSERT INTO executions (approval_id, status, started_at)
                 VALUES (?1, 'STARTING', ?2)",
                params![approval_id, now],
            )?;
        }
        tx.commit()?;
        Ok(true)
    }

    /// Fecha a execução e consome uma aprovação ainda `CLAIMED`, caso o
    /// chamador tenha pulado o `start`. A operação é idempotente.
    pub fn complete_execution(
        &self,
        approval_id: &str,
        status: &str,
        summary: Option<&str>,
        now: f64,
    ) -> Result<bool, rusqlite::Error> {
        let mut conn = self.conn.lock().expect("lock de approvals store");
        let tx = conn.transaction()?;
        tx.execute(
            "UPDATE approvals SET status = 'CONSUMED', consumed_at = ?2
             WHERE id = ?1 AND status = 'CLAIMED'",
            params![approval_id, now],
        )?;
        let updated = tx.execute(
            "UPDATE executions SET status = ?2, completed_at = ?3, result_summary = ?4
             WHERE approval_id = ?1",
            params![approval_id, status, now, summary],
        )?;
        if updated == 0 {
            tx.execute(
                "INSERT INTO executions (approval_id, status, started_at, completed_at, result_summary)
                 VALUES (?1, ?2, ?3, ?3, ?4)",
                params![approval_id, status, now, summary],
            )?;
        }
        tx.commit()?;
        Ok(true)
    }

    pub fn get_execution(
        &self,
        approval_id: &str,
    ) -> Result<Option<ExecutionRecord>, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        conn.query_row(
            "SELECT approval_id, status, started_at, completed_at, result_summary
             FROM executions WHERE approval_id = ?1",
            params![approval_id],
            |row| {
                Ok(ExecutionRecord {
                    approval_id: row.get(0)?,
                    status: row.get(1)?,
                    started_at: row.get(2)?,
                    completed_at: row.get(3)?,
                    result_summary: row.get(4)?,
                })
            },
        )
        .optional()
    }

    pub fn list_approvals(
        &self,
        status: Option<&str>,
        limit: u32,
        offset: u32,
    ) -> Result<Vec<ApprovalRecord>, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let limit = i64::from(limit.max(1).min(1000));
        let offset = i64::from(offset);
        let sql = match status {
            Some(_) => {
                "SELECT id, tool_id, params_hash, task_id, plan_id, execution_mode,
                        access_level, fs_scope, net_scope, resource_limits, policy_version,
                        justification, status, created_at, expires_at, decided_at,
                        decided_by, consumed_at, risk_level
                 FROM approvals WHERE status = ?1
                 ORDER BY created_at DESC LIMIT ?2 OFFSET ?3"
            }
            None => {
                "SELECT id, tool_id, params_hash, task_id, plan_id, execution_mode,
                        access_level, fs_scope, net_scope, resource_limits, policy_version,
                        justification, status, created_at, expires_at, decided_at,
                        decided_by, consumed_at, risk_level
                 FROM approvals
                 ORDER BY created_at DESC LIMIT ?1 OFFSET ?2"
            }
        };
        let mut statement = conn.prepare(sql)?;
        let rows = match status {
            Some(status) => statement.query_map(params![status, limit, offset], row_to_approval)?,
            None => statement.query_map(params![limit, offset], row_to_approval)?,
        };
        rows.collect()
    }

    pub fn recover_orphans(&self, now: f64, timeout_s: f64) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let cutoff = now - timeout_s.max(0.0);
        let updated = conn.execute(
            "UPDATE executions SET status = 'UNKNOWN'
             WHERE status IN ('PREPARED', 'STARTING', 'RUNNING') AND started_at < ?1",
            params![cutoff],
        )?;
        Ok(updated)
    }

    pub fn count(&self) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM approvals", [], |row| row.get(0))?;
        Ok(count.max(0) as usize)
    }

    #[cfg(test)]
    pub fn set_expires_at(
        &self,
        approval_id: &str,
        expires_at: f64,
    ) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().expect("lock de approvals store");
        conn.execute(
            "UPDATE approvals SET expires_at = ?2 WHERE id = ?1",
            params![approval_id, expires_at],
        )?;
        Ok(())
    }
}

fn row_to_approval(row: &Row<'_>) -> rusqlite::Result<ApprovalRecord> {
    Ok(ApprovalRecord {
        approval_id: row.get(0)?,
        tool_id: row.get(1)?,
        params_hash: row.get(2)?,
        task_id: row.get(3)?,
        plan_id: row.get(4)?,
        execution_mode: row.get(5)?,
        access_level: row.get(6)?,
        fs_scope: row.get(7)?,
        net_scope: row.get(8)?,
        resource_limits: row.get(9)?,
        policy_version: row.get(10)?,
        justification: row.get(11)?,
        status: row.get(12)?,
        created_at: row.get(13)?,
        expires_at: row.get(14)?,
        decided_at: row.get(15)?,
        decided_by: row.get(16)?,
        consumed_at: row.get(17)?,
        risk_level: row.get(18)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_memory_store_roundtrips_approval_and_execution() {
        let store = ApprovalStore::open_in_memory().expect("store");
        let record = ApprovalRecord {
            approval_id: "approval-1".to_string(),
            tool_id: "sys.dangerous".to_string(),
            params_hash: "a".repeat(64),
            task_id: "task-1".to_string(),
            plan_id: "plan-1".to_string(),
            execution_mode: "HOST".to_string(),
            access_level: "WRITE".to_string(),
            fs_scope: Some("DEFAULT".to_string()),
            net_scope: Some("NONE".to_string()),
            resource_limits: Some("default".to_string()),
            policy_version: "1.0".to_string(),
            justification: Some("test".to_string()),
            status: "APPROVED".to_string(),
            created_at: 100.0,
            expires_at: 200.0,
            decided_at: Some(105.0),
            decided_by: Some("operator".to_string()),
            consumed_at: None,
            risk_level: Some("HIGH".to_string()),
        };
        store.insert_approval(&record).expect("insert");
        let stored = store
            .get_approval("approval-1")
            .expect("get")
            .expect("record");
        assert_eq!(stored.status, "APPROVED");
        assert_eq!(stored.risk_level.as_deref(), Some("HIGH"));
        assert!(store.claim("approval-1", 110.0).expect("claim"));
        let execution = store
            .get_execution("approval-1")
            .expect("execution")
            .expect("row");
        assert_eq!(execution.status, "PREPARED");
        assert!(store.start_execution("approval-1", 120.0).expect("start"));
        let stored = store
            .get_approval("approval-1")
            .expect("get")
            .expect("record");
        assert_eq!(stored.status, "CONSUMED");
        assert_eq!(stored.consumed_at, Some(120.0));
        assert!(store
            .complete_execution("approval-1", "COMPLETED", Some("ok"), 130.0)
            .expect("complete"));
        let execution = store
            .get_execution("approval-1")
            .expect("execution")
            .expect("row");
        assert_eq!(execution.status, "COMPLETED");
        assert_eq!(execution.result_summary.as_deref(), Some("ok"));
    }

    #[test]
    fn file_store_survives_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("approvals.db");
        let record = ApprovalRecord {
            approval_id: "approval-2".to_string(),
            tool_id: "sys.dangerous".to_string(),
            params_hash: "b".repeat(64),
            task_id: "task-2".to_string(),
            plan_id: "plan-2".to_string(),
            execution_mode: "HOST".to_string(),
            access_level: "READ".to_string(),
            fs_scope: Some("DEFAULT".to_string()),
            net_scope: Some("NONE".to_string()),
            resource_limits: Some("default".to_string()),
            policy_version: "1.0".to_string(),
            justification: None,
            status: "PENDING".to_string(),
            created_at: 100.0,
            expires_at: 200.0,
            decided_at: None,
            decided_by: None,
            consumed_at: None,
            risk_level: None,
        };
        {
            let store = ApprovalStore::open(&path).expect("open");
            store.insert_approval(&record).expect("insert");
        }
        let store = ApprovalStore::open(&path).expect("reopen");
        let stored = store
            .get_approval("approval-2")
            .expect("get")
            .expect("record");
        assert_eq!(stored.tool_id, "sys.dangerous");
        assert_eq!(store.count().expect("count"), 1);
    }

    #[test]
    fn list_filters_and_paginates() {
        let store = ApprovalStore::open_in_memory().expect("store");
        for index in 0..3 {
            let record = ApprovalRecord {
                approval_id: format!("approval-{index}"),
                tool_id: "sys.dangerous".to_string(),
                params_hash: "c".repeat(64),
                task_id: "task".to_string(),
                plan_id: "plan".to_string(),
                execution_mode: "HOST".to_string(),
                access_level: "READ".to_string(),
                fs_scope: None,
                net_scope: None,
                resource_limits: None,
                policy_version: "1.0".to_string(),
                justification: None,
                status: if index == 0 { "APPROVED" } else { "PENDING" }.to_string(),
                created_at: 100.0 + f64::from(index),
                expires_at: 200.0,
                decided_at: None,
                decided_by: None,
                consumed_at: None,
                risk_level: None,
            };
            store.insert_approval(&record).expect("insert");
        }
        let pending = store.list_approvals(Some("PENDING"), 10, 0).expect("list");
        assert_eq!(pending.len(), 2);
        let page = store.list_approvals(None, 1, 1).expect("page");
        assert_eq!(page.len(), 1);
    }

    #[test]
    fn expire_and_recover_are_bounded() {
        let store = ApprovalStore::open_in_memory().expect("store");
        let record = ApprovalRecord {
            approval_id: "approval-exp".to_string(),
            tool_id: "sys.dangerous".to_string(),
            params_hash: "d".repeat(64),
            task_id: "task".to_string(),
            plan_id: "plan".to_string(),
            execution_mode: "HOST".to_string(),
            access_level: "READ".to_string(),
            fs_scope: None,
            net_scope: None,
            resource_limits: None,
            policy_version: "1.0".to_string(),
            justification: None,
            status: "PENDING".to_string(),
            created_at: 100.0,
            expires_at: 110.0,
            decided_at: None,
            decided_by: None,
            consumed_at: None,
            risk_level: None,
        };
        store.insert_approval(&record).expect("insert");
        assert_eq!(store.expire_stale(120.0).expect("expire"), 1);
        let stored = store
            .get_approval("approval-exp")
            .expect("get")
            .expect("record");
        assert_eq!(stored.status, "EXPIRED");
    }
}
