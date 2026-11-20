//! Schema versionado do Control Plane DB (T6 + T8).
//!
//! O mesmo arquivo SQLite (`data/databases/nexus_approvals.db`) guarda as
//! aprovações/execuções do T6 e as tabelas de identidade do T8. A versão 1
//! representa o schema do T6; a versão 2 adiciona identidade e as colunas de
//! vínculo em `approvals`. Não confundir com a migração 5 do event store
//! Python: são bancos e históricos diferentes.
//!
//! A migração é idempotente e transacional. Um banco legado, criado antes de
//! `schema_migrations`, é adotado como versão 1 e recebe a versão 2.

use crate::identity::catalog::Catalog;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use std::path::Path;

/// Versão máxima que este binário sabe abrir.
pub const SUPPORTED_SCHEMA_VERSION: i64 = 2;

/// DDL do T6 (aprovações e execuções), compartilhado com `ApprovalStore`.
pub const APPROVALS_DDL: &str = "
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        tool_id TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        risk_level TEXT,
        access_level TEXT NOT NULL,
        execution_mode TEXT NOT NULL,
        fs_scope TEXT,
        net_scope TEXT,
        resource_limits TEXT,
        policy_version TEXT NOT NULL,
        justification TEXT,
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        decided_at REAL,
        decided_by TEXT,
        consumed_at REAL
    );
    CREATE TABLE IF NOT EXISTS executions (
        approval_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        started_at REAL,
        completed_at REAL,
        result_summary TEXT,
        FOREIGN KEY(approval_id) REFERENCES approvals(id)
    );
    CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
    CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
    CREATE INDEX IF NOT EXISTS idx_approvals_created ON approvals(created_at);
";

/// DDL do T8. `devices.id` é o identificador público; `principal_id` liga o
/// hardware ao principal do usuário.
pub const IDENTITY_DDL: &str = "
    CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        builtin INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS role_scopes (
        role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        scope TEXT NOT NULL,
        PRIMARY KEY (role_id, scope)
    );
    CREATE TABLE IF NOT EXISTS principals (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('user','device','agent','service')),
        display_name TEXT NOT NULL,
        role_id TEXT NOT NULL REFERENCES roles(id),
        parent_id TEXT REFERENCES principals(id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK (status IN ('active','suspended','revoked')),
        max_autonomy INTEGER NOT NULL DEFAULT 0 CHECK (max_autonomy BETWEEN 0 AND 4),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        revoked_at REAL,
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS principal_scopes (
        principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
        scope TEXT NOT NULL,
        granted_by TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
        granted_at REAL NOT NULL,
        expires_at REAL,
        PRIMARY KEY (principal_id, scope)
    );
    CREATE TABLE IF NOT EXISTS credentials (
        id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('local_owner','device_key','access','refresh','agent')),
        secret_hash TEXT,
        public_key TEXT,
        algorithm TEXT CHECK (algorithm IN ('p256','ed25519')),
        family_id TEXT,
        rotated_from TEXT,
        status TEXT NOT NULL CHECK (status IN ('active','rotated','revoked','expired')),
        created_at REAL NOT NULL,
        last_used_at REAL,
        expires_at REAL,
        revoked_at REAL,
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        model TEXT,
        app_version TEXT,
        public_key_fingerprint TEXT NOT NULL,
        constraints TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
        paired_at REAL NOT NULL,
        last_seen_at REAL,
        revoked_at REAL
    );
    CREATE TABLE IF NOT EXISTS pairing_sessions (
        id TEXT PRIMARY KEY,
        secret_hash TEXT NOT NULL,
        code_hash TEXT,
        role_id TEXT NOT NULL REFERENCES roles(id),
        created_by TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
        constraints TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK (status IN ('pending','consumed','expired','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        consumed_at REAL
    );
    CREATE TABLE IF NOT EXISTS identity_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_principals_kind_status ON principals(kind, status);
    CREATE INDEX IF NOT EXISTS idx_principals_parent ON principals(parent_id);
    CREATE INDEX IF NOT EXISTS idx_principal_scopes_principal ON principal_scopes(principal_id);
    CREATE INDEX IF NOT EXISTS idx_credentials_principal_status ON credentials(principal_id, status);
    CREATE INDEX IF NOT EXISTS idx_credentials_family ON credentials(family_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_secret_hash
        ON credentials(secret_hash) WHERE secret_hash IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_devices_principal ON devices(principal_id);
    CREATE INDEX IF NOT EXISTS idx_pairing_status_expires ON pairing_sessions(status, expires_at);
";

#[derive(Debug, thiserror::Error)]
pub enum SchemaError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("schema do control plane {found} é mais novo que o suportado {supported}")]
    UnsupportedVersion { found: i64, supported: i64 },
}

/// Aplica as migrações pendentes e devolve a versão final.
pub fn migrate(conn: &mut Connection, now: f64) -> Result<i64, SchemaError> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        );",
    )?;
    let current: i64 = conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        [],
        |row| row.get(0),
    )?;
    if current > SUPPORTED_SCHEMA_VERSION {
        return Err(SchemaError::UnsupportedVersion {
            found: current,
            supported: SUPPORTED_SCHEMA_VERSION,
        });
    }

    if current < 1 {
        let tx = conn.transaction()?;
        tx.execute_batch(APPROVALS_DDL)?;
        record_migration(&tx, 1, "t6_approvals", now)?;
        tx.commit()?;
    }
    if current < 2 {
        let tx = conn.transaction()?;
        tx.execute_batch(IDENTITY_DDL)?;
        add_binding_columns(&tx)?;
        record_migration(&tx, 2, "t8_identity", now)?;
        tx.commit()?;
    }
    Ok(SUPPORTED_SCHEMA_VERSION)
}

fn record_migration(
    tx: &Transaction<'_>,
    version: i64,
    name: &str,
    now: f64,
) -> Result<(), rusqlite::Error> {
    tx.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
         VALUES (?1, ?2, ?3)",
        params![version, name, now],
    )?;
    Ok(())
}

fn add_binding_columns(conn: &Connection) -> Result<(), rusqlite::Error> {
    for (column, definition) in [
        ("principal_id", "TEXT"),
        ("device_id", "TEXT"),
        ("required_scope", "TEXT"),
        ("decided_by_principal_id", "TEXT"),
    ] {
        if !column_exists(conn, "approvals", column)? {
            conn.execute(
                &format!("ALTER TABLE approvals ADD COLUMN {column} {definition}"),
                [],
            )?;
        }
    }
    Ok(())
}

fn column_exists(conn: &Connection, table: &str, column: &str) -> Result<bool, rusqlite::Error> {
    let mut statement = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        if name == column {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Semeia os papéis built-in do catálogo no banco de controle.
///
/// O catálogo é a fonte da verdade: papéis built-in são atualizados e seus
/// escopos substituídos na mesma transação. Papéis customizados não são
/// tocados.
pub fn seed_builtin_roles(
    conn: &mut Connection,
    catalog: &Catalog,
    now: f64,
) -> Result<(), rusqlite::Error> {
    let tx = conn.transaction()?;
    for role in catalog.roles.values() {
        tx.execute(
            "INSERT INTO roles (id, name, description, builtin, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                builtin = excluded.builtin",
            params![
                role.id,
                role.name,
                role.description,
                i64::from(role.builtin),
                now
            ],
        )?;
        tx.execute(
            "DELETE FROM role_scopes WHERE role_id = ?1",
            params![role.id],
        )?;
        for scope in &role.scopes {
            tx.execute(
                "INSERT INTO role_scopes (role_id, scope) VALUES (?1, ?2)",
                params![role.id, scope],
            )?;
        }
    }
    tx.execute(
        "INSERT INTO identity_meta (key, value) VALUES ('catalog_version', ?1)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![catalog.catalog_version],
    )?;
    tx.execute(
        "INSERT INTO identity_meta (key, value) VALUES ('contract_version', ?1)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![catalog.contract_version],
    )?;
    tx.commit()?;
    Ok(())
}

pub fn schema_version(conn: &Connection) -> Result<i64, rusqlite::Error> {
    let exists: Option<String> = conn
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_none() {
        return Ok(0);
    }
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        [],
        |row| row.get(0),
    )
}

pub fn unix_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(unix)]
pub fn restrict_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    if path.as_os_str().is_empty() {
        return;
    }
    if let Ok(metadata) = std::fs::metadata(path) {
        let mut permissions = metadata.permissions();
        permissions.set_mode(0o600);
        let _ = std::fs::set_permissions(path, permissions);
    }
}

#[cfg(not(unix))]
pub fn restrict_permissions(_path: &Path) {}
