//! Persistência de identidade no Control Plane DB (T8.2).
//!
//! O store compartilha o arquivo do T6 (`nexus_approvals.db`), mas é aberto
//! pelo Rust com o schema versionado de `control_schema`. Toda operação é
//! serializada por um único `Mutex<Connection>`, o que mantém as invariantes
//! de pairing e revogação sem depender de locks externos.
//!
//! O IPC/CLI passam a consumir estes tipos no T8.3; até lá algumas APIs são
//! exercitadas apenas pelos testes.

#![allow(dead_code)]

use super::catalog::{builtin_catalog, Catalog, CatalogError};
use crate::control_schema::{
    migrate, restrict_permissions, schema_version, seed_builtin_roles, unix_now, SchemaError,
};
use rusqlite::{params, Connection, ErrorCode, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Mutex;
use uuid::Uuid;

pub const MAX_DISPLAY_NAME: usize = 128;
pub const MAX_METADATA_BYTES: usize = 4096;
pub const MAX_PAIRING_TTL_S: f64 = 300.0;
pub const DEFAULT_PAIRING_ATTEMPTS: i64 = 5;

const PRINCIPAL_COLUMNS: &str = "id, kind, display_name, role_id, parent_id, status, \
                                 max_autonomy, created_at, updated_at, revoked_at, metadata";
const CREDENTIAL_COLUMNS: &str = "id, principal_id, kind, secret_hash, public_key, \
                                  algorithm, family_id, rotated_from, status, created_at, \
                                  last_used_at, expires_at, revoked_at, metadata";
const DEVICE_COLUMNS: &str = "id, principal_id, platform, model, app_version, \
                              public_key_fingerprint, constraints, status, paired_at, \
                              last_seen_at, revoked_at";
const PAIRING_COLUMNS: &str = "id, secret_hash, code_hash, role_id, created_by, \
                               constraints, status, attempts, max_attempts, created_at, \
                               expires_at, consumed_at";

#[derive(Debug, thiserror::Error)]
pub enum IdentityError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("schema: {0}")]
    Schema(#[from] SchemaError),
    #[error("catálogo: {0}")]
    Catalog(#[from] CatalogError),
    #[error("IO: {0}")]
    Io(#[from] std::io::Error),
    #[error("dados inválidos: {0}")]
    Invalid(String),
    #[error("conflito: {0}")]
    Conflict(String),
    #[error("principal não encontrado: {0}")]
    PrincipalNotFound(String),
    #[error("principal não está ativo: {0}")]
    PrincipalNotActive(String),
    #[error("device não encontrado: {0}")]
    DeviceNotFound(String),
    #[error("credencial não encontrada: {0}")]
    CredentialNotFound(String),
    #[error("sessão de pairing não encontrada: {0}")]
    PairingNotFound(String),
    #[error("sessão de pairing expirada")]
    PairingExpired,
    #[error("sessão de pairing já consumida")]
    PairingConsumed,
    #[error("sessão de pairing não está pendente: {0}")]
    PairingUnavailable(String),
    #[error("prova de pairing inválida")]
    PairingInvalidProof,
    #[error("principal {principal_id} não tem o escopo obrigatório {scope}")]
    MissingScope { principal_id: String, scope: String },
    #[error("escalada de privilégio: {0}")]
    PrivilegeEscalation(String),
    #[error("reuse de refresh detectado; família {family_id} revogada")]
    RefreshReuseDetected {
        family_id: String,
        principal_id: String,
    },
}

#[derive(Debug, Clone, Serialize)]
pub struct Principal {
    pub id: String,
    pub kind: String,
    pub display_name: String,
    pub role_id: String,
    pub parent_id: Option<String>,
    pub status: String,
    pub max_autonomy: u8,
    pub created_at: f64,
    pub updated_at: f64,
    pub revoked_at: Option<f64>,
    pub metadata: String,
}

#[derive(Debug, Clone)]
pub struct NewPrincipal {
    pub kind: String,
    pub display_name: String,
    pub role_id: String,
    pub parent_id: Option<String>,
    pub max_autonomy: u8,
    pub metadata: String,
}

impl NewPrincipal {
    pub fn new(
        kind: impl Into<String>,
        display_name: impl Into<String>,
        role_id: impl Into<String>,
        max_autonomy: u8,
    ) -> Self {
        Self {
            kind: kind.into(),
            display_name: display_name.into(),
            role_id: role_id.into(),
            parent_id: None,
            max_autonomy,
            metadata: "{}".to_string(),
        }
    }

    pub fn with_parent(mut self, parent_id: impl Into<String>) -> Self {
        self.parent_id = Some(parent_id.into());
        self
    }

    pub fn with_metadata(mut self, metadata: impl Into<String>) -> Self {
        self.metadata = metadata.into();
        self
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Credential {
    pub id: String,
    pub principal_id: String,
    pub kind: String,
    #[serde(skip_serializing)]
    pub secret_hash: Option<String>,
    pub public_key: Option<String>,
    pub algorithm: Option<String>,
    pub family_id: Option<String>,
    pub rotated_from: Option<String>,
    pub status: String,
    pub created_at: f64,
    pub last_used_at: Option<f64>,
    pub expires_at: Option<f64>,
    pub revoked_at: Option<f64>,
    pub metadata: String,
}

#[derive(Debug, Clone)]
pub struct NewCredential {
    pub principal_id: String,
    pub kind: String,
    pub secret_hash: Option<String>,
    pub public_key: Option<String>,
    pub algorithm: Option<String>,
    pub family_id: Option<String>,
    pub rotated_from: Option<String>,
    pub expires_at: Option<f64>,
    pub metadata: String,
}

impl NewCredential {
    pub fn new(principal_id: impl Into<String>, kind: impl Into<String>) -> Self {
        Self {
            principal_id: principal_id.into(),
            kind: kind.into(),
            secret_hash: None,
            public_key: None,
            algorithm: None,
            family_id: None,
            rotated_from: None,
            expires_at: None,
            metadata: "{}".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Device {
    pub id: String,
    pub principal_id: String,
    pub platform: String,
    pub model: Option<String>,
    pub app_version: Option<String>,
    pub public_key_fingerprint: String,
    pub constraints: String,
    pub status: String,
    pub paired_at: f64,
    pub last_seen_at: Option<f64>,
    pub revoked_at: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PairingSession {
    pub id: String,
    #[serde(skip_serializing)]
    pub secret_hash: String,
    #[serde(skip_serializing)]
    pub code_hash: Option<String>,
    pub role_id: String,
    pub created_by: String,
    pub constraints: String,
    pub status: String,
    pub attempts: i64,
    pub max_attempts: i64,
    pub created_at: f64,
    pub expires_at: f64,
    pub consumed_at: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct NewPairingSession {
    pub role_id: String,
    pub created_by: String,
    pub secret_hash: String,
    pub code_hash: Option<String>,
    pub constraints: String,
    pub ttl_s: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RevocationSummary {
    pub principal_ids: Vec<String>,
    pub principals: usize,
    pub credentials: usize,
    pub devices: usize,
    pub approvals: usize,
    pub pairing_sessions: usize,
}

/// Resultado do bootstrap do owner local (T8.4).
///
/// `created_principal`/`created_credential` permitem ao chamador emitir
/// auditoria apenas quando houve mudança real; uma segunda execução
/// idempotente retorna `false` em ambos.
#[derive(Debug, Clone, Serialize)]
pub struct OwnerBootstrap {
    pub principal: Principal,
    pub credential_id: String,
    pub created_principal: bool,
    pub created_credential: bool,
}

/// Owner local resolvido pelo hash do token, sem criar nada (T8.4).
#[derive(Debug, Clone, Serialize)]
pub struct LocalOwner {
    pub principal: Principal,
    pub credential_id: String,
}

#[derive(Debug, Deserialize, Default)]
struct Constraints {
    #[serde(default)]
    scopes: Option<Vec<String>>,
    #[serde(default)]
    max_autonomy: Option<u8>,
}

/// Store de identidade com conexão serializada.
pub struct IdentityStore {
    conn: Mutex<Connection>,
    catalog: &'static Catalog,
}

impl IdentityStore {
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, IdentityError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let mut conn = Connection::open(path)?;
        configure(&conn)?;
        Self::initialize(&mut conn)?;
        restrict_permissions(path);
        Ok(Self {
            conn: Mutex::new(conn),
            catalog: builtin_catalog(),
        })
    }

    pub fn open_in_memory() -> Result<Self, IdentityError> {
        let mut conn = Connection::open_in_memory()?;
        configure(&conn)?;
        Self::initialize(&mut conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
            catalog: builtin_catalog(),
        })
    }

    fn initialize(conn: &mut Connection) -> Result<(), IdentityError> {
        migrate(conn, unix_now())?;
        seed_builtin_roles(conn, builtin_catalog(), unix_now())?;
        Ok(())
    }

    pub fn catalog(&self) -> &'static Catalog {
        self.catalog
    }

    pub fn schema_version(&self) -> Result<i64, IdentityError> {
        let conn = self.lock();
        Ok(schema_version(&conn)?)
    }

    pub fn catalog_version(&self) -> Result<String, IdentityError> {
        let conn = self.lock();
        conn.query_row(
            "SELECT value FROM identity_meta WHERE key = 'catalog_version'",
            [],
            |row| row.get(0),
        )
        .optional()?
        .ok_or_else(|| IdentityError::Invalid("identity_meta sem catalog_version".to_string()))
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().expect("lock do identity store")
    }

    // -- principals --------------------------------------------------------

    pub fn create_principal(
        &self,
        new: &NewPrincipal,
        now: f64,
    ) -> Result<Principal, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        self.create_principal_conn(&conn, new, now)
    }

    fn create_principal_conn(
        &self,
        conn: &Connection,
        new: &NewPrincipal,
        now: f64,
    ) -> Result<Principal, IdentityError> {
        let role = self.catalog.role(&new.role_id)?;
        validate_kind(&new.kind)?;
        validate_display_name(&new.display_name)?;
        validate_autonomy(new.max_autonomy)?;
        let metadata = normalize_metadata(&new.metadata)?;

        match new.kind.as_str() {
            "user" | "service" => {
                if new.parent_id.is_some() {
                    return Err(IdentityError::Invalid(format!(
                        "principal {} não pode ter parent_id",
                        new.kind
                    )));
                }
            }
            "device" => {
                let parent_id = new.parent_id.as_deref().ok_or_else(|| {
                    IdentityError::Invalid("device exige parent_id de um user".to_string())
                })?;
                let parent = load_principal_conn(conn, parent_id)?;
                if parent.kind != "user" {
                    return Err(IdentityError::Invalid(
                        "parent de device deve ser um user".to_string(),
                    ));
                }
                self.ensure_parent_scope(conn, parent_id, &role.scopes, now)?;
            }
            "agent" => {
                let forbidden: Vec<&str> = role
                    .scopes
                    .iter()
                    .filter(|scope| agent_creation_forbidden(scope))
                    .map(String::as_str)
                    .collect();
                if !forbidden.is_empty() {
                    return Err(IdentityError::PrivilegeEscalation(format!(
                        "agente não pode receber escopos de gestão de identidade: {forbidden:?}"
                    )));
                }
                let parent_id = new.parent_id.as_deref().ok_or_else(|| {
                    IdentityError::Invalid("agent exige parent_id de user ou agent".to_string())
                })?;
                let parent = load_principal_conn(conn, parent_id)?;
                if !matches!(parent.kind.as_str(), "user" | "agent") {
                    return Err(IdentityError::Invalid(
                        "parent de agent deve ser user ou agent".to_string(),
                    ));
                }
                self.ensure_parent_scope(conn, parent_id, &role.scopes, now)?;
            }
            other => unreachable!("kind validado: {other}"),
        }

        if let Some(parent_id) = &new.parent_id {
            let parent = load_principal_conn(conn, parent_id)?;
            if parent.status != "active" {
                return Err(IdentityError::PrincipalNotActive(parent_id.clone()));
            }
        }

        let id = format!("prn_{}", Uuid::new_v4().simple());
        conn.execute(
            "INSERT INTO principals (
                id, kind, display_name, role_id, parent_id, status, max_autonomy,
                created_at, updated_at, revoked_at, metadata
             ) VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6, ?7, ?7, NULL, ?8)",
            params![
                id,
                new.kind,
                new.display_name.trim(),
                new.role_id,
                new.parent_id,
                new.max_autonomy,
                now,
                metadata,
            ],
        )?;
        load_principal_conn(conn, &id)
    }

    fn ensure_parent_scope(
        &self,
        conn: &Connection,
        parent_id: &str,
        child_scopes: &BTreeSet<String>,
        now: f64,
    ) -> Result<(), IdentityError> {
        let parent = load_principal_conn(conn, parent_id)?;
        if parent.status != "active" {
            return Err(IdentityError::PrincipalNotActive(parent_id.to_string()));
        }
        let parent_scopes =
            self.effective_scopes_conn(conn, parent_id, now, &mut BTreeSet::new())?;
        let missing: Vec<&str> = child_scopes
            .iter()
            .filter(|scope| !parent_scopes.contains(*scope))
            .map(String::as_str)
            .collect();
        if !missing.is_empty() {
            return Err(IdentityError::PrivilegeEscalation(format!(
                "filho de {parent_id} excede o pai; escopos ausentes: {missing:?}"
            )));
        }
        Ok(())
    }

    pub fn get_principal(&self, principal_id: &str) -> Result<Principal, IdentityError> {
        let conn = self.lock();
        load_principal_conn(&conn, principal_id)
    }

    pub fn list_principals(
        &self,
        kind: Option<&str>,
        status: Option<&str>,
    ) -> Result<Vec<Principal>, IdentityError> {
        if let Some(kind) = kind {
            validate_kind(kind)?;
        }
        if let Some(status) = status {
            validate_status(status)?;
        }
        let conn = self.lock();
        let mut statement = conn.prepare(&format!(
            "SELECT {PRINCIPAL_COLUMNS} FROM principals
             WHERE (?1 IS NULL OR kind = ?1) AND (?2 IS NULL OR status = ?2)
             ORDER BY created_at ASC, id ASC"
        ))?;
        let rows = statement.query_map(params![kind, status], principal_from_row)?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    pub fn count_principals(&self) -> Result<usize, IdentityError> {
        let conn = self.lock();
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM principals", [], |row| row.get(0))?;
        Ok(count.max(0) as usize)
    }

    /// Cria (ou reutiliza) o owner local e vincula o hash do token local.
    ///
    /// O bootstrap é idempotente: existe no máximo um principal `owner` do
    /// tipo `user`. Se o token local mudou, a credencial `local_owner` antiga
    /// é revogada e uma nova é emitida na mesma transação. Um owner existente
    /// porém não ativo não é recriado silenciosamente — a recuperação exige
    /// o fluxo local explícito (`identity recover`).
    pub fn init_owner(
        &self,
        display_name: &str,
        token_hash: &str,
        now: f64,
    ) -> Result<OwnerBootstrap, IdentityError> {
        validate_now(now)?;
        validate_hash("token_hash", token_hash)?;
        validate_display_name(display_name)?;

        let mut conn = self.lock();
        let tx = conn.transaction()?;
        let existing = tx
            .query_row(
                &format!(
                    "SELECT {PRINCIPAL_COLUMNS} FROM principals
                     WHERE kind = 'user' AND role_id = 'owner'
                     ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                              created_at ASC, id ASC
                     LIMIT 1"
                ),
                [],
                principal_from_row,
            )
            .optional()?;

        let (principal, created_principal) = match existing {
            Some(principal) if principal.status == "active" => (principal, false),
            Some(principal) => return Err(IdentityError::PrincipalNotActive(principal.id)),
            None => (
                self.create_principal_conn(
                    &tx,
                    &NewPrincipal::new("user", display_name, "owner", 4)
                        .with_metadata("{\"source\":\"local_bootstrap\"}"),
                    now,
                )?,
                true,
            ),
        };

        let current = tx
            .query_row(
                &format!(
                    "SELECT {CREDENTIAL_COLUMNS} FROM credentials
                     WHERE principal_id = ?1 AND kind = 'local_owner' AND status = 'active'
                     ORDER BY created_at DESC LIMIT 1"
                ),
                params![principal.id],
                credential_from_row,
            )
            .optional()?;

        let (credential, created_credential) = match current {
            Some(credential)
                if credential
                    .secret_hash
                    .as_deref()
                    .is_some_and(|hash| hashes_equal(hash, token_hash)) =>
            {
                (credential, false)
            }
            other => {
                if let Some(previous) = other {
                    tx.execute(
                        "UPDATE credentials SET status = 'revoked', revoked_at = ?2
                         WHERE id = ?1 AND status != 'revoked'",
                        params![previous.id, now],
                    )?;
                }
                let mut new = NewCredential::new(&principal.id, "local_owner");
                new.secret_hash = Some(token_hash.to_string());
                new.metadata = "{\"source\":\"local\"}".to_string();
                (self.issue_credential_conn(&tx, &new, now)?, true)
            }
        };
        tx.commit()?;

        Ok(OwnerBootstrap {
            principal,
            credential_id: credential.id,
            created_principal,
            created_credential,
        })
    }

    /// Resolve o owner local a partir do hash do token, sem criar nada.
    ///
    /// Retorna `None` quando o token não corresponde a uma credencial
    /// `local_owner` ativa de um principal ativo.
    pub fn local_owner_status(
        &self,
        token_hash: &str,
        now: f64,
    ) -> Result<Option<LocalOwner>, IdentityError> {
        validate_now(now)?;
        let credential = self.credential_by_hash(token_hash, now)?;
        let Some(credential) = credential else {
            return Ok(None);
        };
        if credential.kind != "local_owner" {
            return Ok(None);
        }
        let principal = self.get_principal(&credential.principal_id)?;
        if principal.status != "active" || principal.role_id != "owner" || principal.kind != "user"
        {
            return Ok(None);
        }
        Ok(Some(LocalOwner {
            credential_id: credential.id,
            principal,
        }))
    }

    // -- escopos efetivos --------------------------------------------------

    pub fn effective_scopes(
        &self,
        principal_id: &str,
        now: f64,
    ) -> Result<BTreeSet<String>, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        self.effective_scopes_conn(&conn, principal_id, now, &mut BTreeSet::new())
    }

    fn effective_scopes_conn(
        &self,
        conn: &Connection,
        principal_id: &str,
        now: f64,
        stack: &mut BTreeSet<String>,
    ) -> Result<BTreeSet<String>, IdentityError> {
        if !stack.insert(principal_id.to_string()) {
            return Err(IdentityError::Invalid(format!(
                "ciclo de parentesco envolvendo {principal_id}"
            )));
        }
        let result = self.effective_scopes_inner(conn, principal_id, now, stack);
        stack.remove(principal_id);
        result
    }

    fn effective_scopes_inner(
        &self,
        conn: &Connection,
        principal_id: &str,
        now: f64,
        stack: &mut BTreeSet<String>,
    ) -> Result<BTreeSet<String>, IdentityError> {
        let principal = load_principal_conn(conn, principal_id)?;
        if principal.status != "active" {
            return Err(IdentityError::PrincipalNotActive(principal_id.to_string()));
        }
        let mut effective = role_scopes_conn(conn, self.catalog, &principal.role_id)?;

        let grants = {
            let mut statement = conn.prepare(
                "SELECT scope, expires_at FROM principal_scopes WHERE principal_id = ?1",
            )?;
            let rows = statement
                .query_map(params![principal_id], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, Option<f64>>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            rows
        };
        if !grants.is_empty() {
            let mut narrowed = BTreeSet::new();
            for (scope, expires_at) in grants {
                if !effective.contains(&scope) {
                    return Err(IdentityError::PrivilegeEscalation(format!(
                        "grant {scope:?} fora do papel {:?}",
                        principal.role_id
                    )));
                }
                if expires_at.is_some_and(|expires| expires <= now) {
                    continue;
                }
                narrowed.insert(scope);
            }
            effective = narrowed;
        }

        if let Some(parent_id) = &principal.parent_id {
            let parent_scopes = self.effective_scopes_conn(conn, parent_id, now, stack)?;
            effective.retain(|scope| parent_scopes.contains(scope));
        }

        if principal.kind == "device" {
            if let Some(raw) = device_constraints_conn(conn, principal_id)? {
                let constraints = constraints_from_raw(&raw)?;
                if let Some(scopes) = constraints.scopes {
                    let mut allowed = BTreeSet::new();
                    for scope in scopes {
                        self.catalog.require_scope(&scope)?;
                        allowed.insert(scope);
                    }
                    effective.retain(|scope| allowed.contains(scope));
                }
            }
        }
        if principal.kind == "agent" {
            // Defesa em profundidade: mesmo que o papel conceda decisão de
            // aprovação a um humano, o agente nunca recebe esses escopos.
            effective.retain(|scope| !agent_effective_forbidden(scope));
        }
        Ok(effective)
    }

    pub fn effective_autonomy(
        &self,
        principal_id: &str,
        machine_cap: u8,
    ) -> Result<u8, IdentityError> {
        let conn = self.lock();
        self.effective_autonomy_conn(&conn, principal_id, machine_cap, &mut BTreeSet::new())
    }

    fn effective_autonomy_conn(
        &self,
        conn: &Connection,
        principal_id: &str,
        machine_cap: u8,
        stack: &mut BTreeSet<String>,
    ) -> Result<u8, IdentityError> {
        if !stack.insert(principal_id.to_string()) {
            return Err(IdentityError::Invalid(format!(
                "ciclo de parentesco envolvendo {principal_id}"
            )));
        }
        let result = (|| {
            let principal = load_principal_conn(conn, principal_id)?;
            if principal.status != "active" {
                return Err(IdentityError::PrincipalNotActive(principal_id.to_string()));
            }
            let mut autonomy = principal.max_autonomy.min(machine_cap.min(4));
            if let Some(parent_id) = &principal.parent_id {
                let parent = self.effective_autonomy_conn(conn, parent_id, machine_cap, stack)?;
                autonomy = autonomy.min(parent);
            }
            if principal.kind == "device" {
                if let Some(raw) = device_constraints_conn(conn, principal_id)? {
                    let constraints = constraints_from_raw(&raw)?;
                    if let Some(max) = constraints.max_autonomy {
                        autonomy = autonomy.min(max.min(4));
                    }
                }
            }
            Ok(autonomy)
        })();
        stack.remove(principal_id);
        result
    }

    pub fn grant_scope(
        &self,
        principal_id: &str,
        scope: &str,
        granted_by: &str,
        expires_at: Option<f64>,
        now: f64,
    ) -> Result<(), IdentityError> {
        validate_now(now)?;
        self.catalog.require_scope(scope)?;
        if let Some(expires) = expires_at {
            if !expires.is_finite() || expires <= now {
                return Err(IdentityError::Invalid(
                    "expires_at do grant deve estar no futuro".to_string(),
                ));
            }
        }
        let conn = self.lock();
        let target = load_principal_conn(&conn, principal_id)?;
        if target.status != "active" {
            return Err(IdentityError::PrincipalNotActive(principal_id.to_string()));
        }
        if target.kind == "agent" && agent_effective_forbidden(scope) {
            return Err(IdentityError::PrivilegeEscalation(format!(
                "agente {principal_id} não pode receber o escopo {scope:?}"
            )));
        }
        let role_scopes = role_scopes_conn(&conn, self.catalog, &target.role_id)?;
        if !role_scopes.contains(scope) {
            return Err(IdentityError::PrivilegeEscalation(format!(
                "grant {scope:?} fora do papel {:?}",
                target.role_id
            )));
        }
        let grantor_scopes =
            self.effective_scopes_conn(&conn, granted_by, now, &mut BTreeSet::new())?;
        if !grantor_scopes.contains("identity.principals.manage") {
            return Err(IdentityError::MissingScope {
                principal_id: granted_by.to_string(),
                scope: "identity.principals.manage".to_string(),
            });
        }
        if !grantor_scopes.contains(scope) {
            return Err(IdentityError::PrivilegeEscalation(format!(
                "grantor {granted_by} não possui {scope:?}"
            )));
        }
        if let Some(parent_id) = &target.parent_id {
            let parent_scopes =
                self.effective_scopes_conn(&conn, parent_id, now, &mut BTreeSet::new())?;
            if !parent_scopes.contains(scope) {
                return Err(IdentityError::PrivilegeEscalation(format!(
                    "grant {scope:?} excede o pai {parent_id}"
                )));
            }
        }
        conn.execute(
            "INSERT INTO principal_scopes (principal_id, scope, granted_by, granted_at, expires_at)
             VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(principal_id, scope) DO UPDATE SET
                granted_by = excluded.granted_by,
                granted_at = excluded.granted_at,
                expires_at = excluded.expires_at",
            params![principal_id, scope, granted_by, now, expires_at],
        )?;
        Ok(())
    }

    pub fn role_scopes(&self, role_id: &str) -> Result<BTreeSet<String>, IdentityError> {
        let conn = self.lock();
        role_scopes_conn(&conn, self.catalog, role_id)
    }

    // -- credenciais -------------------------------------------------------

    pub fn issue_credential(
        &self,
        new: &NewCredential,
        now: f64,
    ) -> Result<Credential, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        self.issue_credential_conn(&conn, new, now)
    }

    fn issue_credential_conn(
        &self,
        conn: &Connection,
        new: &NewCredential,
        now: f64,
    ) -> Result<Credential, IdentityError> {
        let principal = load_principal_conn(conn, &new.principal_id)?;
        if principal.status != "active" {
            return Err(IdentityError::PrincipalNotActive(new.principal_id.clone()));
        }
        validate_credential(new, now)?;
        let metadata = normalize_metadata(&new.metadata)?;
        let id = format!("cred_{}", Uuid::new_v4().simple());
        conn.execute(
            "INSERT INTO credentials (
                id, principal_id, kind, secret_hash, public_key, algorithm, family_id,
                rotated_from, status, created_at, last_used_at, expires_at, revoked_at, metadata
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'active', ?9, NULL, ?10, NULL, ?11)",
            params![
                id,
                new.principal_id,
                new.kind,
                new.secret_hash,
                new.public_key,
                new.algorithm,
                new.family_id,
                new.rotated_from,
                now,
                new.expires_at,
                metadata,
            ],
        )?;
        load_credential_conn(conn, &id)
    }

    pub fn get_credential(&self, credential_id: &str) -> Result<Credential, IdentityError> {
        let conn = self.lock();
        load_credential_conn(&conn, credential_id)
    }

    pub fn credential_by_hash(
        &self,
        secret_hash: &str,
        now: f64,
    ) -> Result<Option<Credential>, IdentityError> {
        validate_now(now)?;
        validate_hash("secret_hash", secret_hash)?;
        let conn = self.lock();
        conn.query_row(
            &format!(
                "SELECT {CREDENTIAL_COLUMNS} FROM credentials
                 WHERE secret_hash = ?1 AND status = 'active'
                   AND (expires_at IS NULL OR expires_at > ?2)
                 ORDER BY created_at DESC LIMIT 1"
            ),
            params![secret_hash, now],
            credential_from_row,
        )
        .optional()
        .map_err(IdentityError::from)
    }

    pub fn revoke_credential(&self, credential_id: &str, now: f64) -> Result<bool, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        let updated = conn.execute(
            "UPDATE credentials SET status = 'revoked', revoked_at = ?2
             WHERE id = ?1 AND status != 'revoked'",
            params![credential_id, now],
        )?;
        Ok(updated > 0)
    }

    /// Rotaciona um refresh ativo e detecta reuse.
    ///
    /// Um refresh ativo vira `rotated` e um novo refresh da mesma família é
    /// emitido na mesma transação. Se o hash apresentado pertencer a um
    /// refresh já rotacionado, a família inteira e o device do principal são
    /// revogados antes de retornar [`IdentityError::RefreshReuseDetected`].
    pub fn rotate_refresh(
        &self,
        presented_hash: &str,
        new_hash: &str,
        new_expires_at: f64,
        now: f64,
    ) -> Result<Credential, IdentityError> {
        validate_now(now)?;
        validate_hash("presented_hash", presented_hash)?;
        validate_hash("new_hash", new_hash)?;
        if !new_expires_at.is_finite() || new_expires_at <= now {
            return Err(IdentityError::Invalid(
                "expires_at do novo refresh deve estar no futuro".to_string(),
            ));
        }
        if hashes_equal(presented_hash, new_hash) {
            return Err(IdentityError::Invalid(
                "novo refresh deve ter hash diferente do apresentado".to_string(),
            ));
        }

        let mut conn = self.lock();
        let tx = conn.transaction()?;
        let presented = tx
            .query_row(
                &format!(
                    "SELECT {CREDENTIAL_COLUMNS} FROM credentials
                     WHERE secret_hash = ?1 AND kind = 'refresh'
                     ORDER BY created_at DESC LIMIT 1"
                ),
                params![presented_hash],
                credential_from_row,
            )
            .optional()?
            .ok_or_else(|| IdentityError::CredentialNotFound(presented_hash.to_string()))?;

        if presented.status == "rotated" {
            let family_id = presented.family_id.clone().ok_or_else(|| {
                IdentityError::Invalid("refresh rotacionado sem family_id".to_string())
            })?;
            let principal_id = presented.principal_id.clone();
            tx.execute(
                "UPDATE credentials SET status = 'revoked', revoked_at = ?2
                 WHERE family_id = ?1 AND status != 'revoked'",
                params![family_id, now],
            )?;
            tx.execute(
                "UPDATE credentials SET status = 'revoked', revoked_at = ?2
                 WHERE principal_id = ?1 AND status != 'revoked'",
                params![principal_id, now],
            )?;
            tx.execute(
                "UPDATE devices SET status = 'revoked', revoked_at = ?2
                 WHERE principal_id = ?1 AND status != 'revoked'",
                params![principal_id, now],
            )?;
            let principal = load_principal_conn(&tx, &principal_id)?;
            if principal.kind == "device" {
                tx.execute(
                    "UPDATE principals SET status = 'revoked', revoked_at = ?2, updated_at = ?2
                     WHERE id = ?1 AND status != 'revoked'",
                    params![principal_id, now],
                )?;
            }
            tx.commit()?;
            return Err(IdentityError::RefreshReuseDetected {
                family_id,
                principal_id,
            });
        }
        if presented.status != "active" {
            return Err(IdentityError::Invalid(format!(
                "refresh não está ativo: {}",
                presented.status
            )));
        }
        if presented.expires_at.is_some_and(|expires| expires <= now) {
            tx.execute(
                "UPDATE credentials SET status = 'expired' WHERE id = ?1",
                params![presented.id],
            )?;
            tx.commit()?;
            return Err(IdentityError::Invalid("refresh expirado".to_string()));
        }

        let family_id = presented
            .family_id
            .clone()
            .ok_or_else(|| IdentityError::Invalid("refresh ativo sem family_id".to_string()))?;
        let rotated = tx.execute(
            "UPDATE credentials SET status = 'rotated', last_used_at = ?2
             WHERE id = ?1 AND status = 'active'",
            params![presented.id, now],
        )?;
        if rotated == 0 {
            return Err(IdentityError::Invalid(
                "refresh mudou de estado durante a rotação".to_string(),
            ));
        }
        let id = format!("cred_{}", Uuid::new_v4().simple());
        tx.execute(
            "INSERT INTO credentials (
                id, principal_id, kind, secret_hash, public_key, algorithm, family_id,
                rotated_from, status, created_at, last_used_at, expires_at, revoked_at, metadata
             ) VALUES (?1, ?2, 'refresh', ?3, NULL, NULL, ?4, ?5, 'active', ?6, NULL, ?7, NULL, ?8)",
            params![
                id,
                presented.principal_id,
                new_hash,
                family_id,
                presented.id,
                now,
                new_expires_at,
                presented.metadata,
            ],
        )?;
        let credential = load_credential_conn(&tx, &id)?;
        tx.commit()?;
        Ok(credential)
    }

    // -- devices -----------------------------------------------------------

    pub fn register_device(
        &self,
        principal_id: &str,
        platform: &str,
        model: Option<&str>,
        app_version: Option<&str>,
        public_key_fingerprint: &str,
        constraints: &str,
        now: f64,
    ) -> Result<Device, IdentityError> {
        validate_now(now)?;
        validate_short("platform", platform, 64)?;
        if let Some(model) = model {
            validate_short("model", model, 128)?;
        }
        if let Some(app_version) = app_version {
            validate_short("app_version", app_version, 64)?;
        }
        validate_short("public_key_fingerprint", public_key_fingerprint, 256)?;
        let conn = self.lock();
        let principal = load_principal_conn(&conn, principal_id)?;
        if principal.kind != "device" {
            return Err(IdentityError::Invalid(
                "register_device exige um principal device".to_string(),
            ));
        }
        if principal.status != "active" {
            return Err(IdentityError::PrincipalNotActive(principal_id.to_string()));
        }
        let role_scopes = role_scopes_conn(&conn, self.catalog, &principal.role_id)?;
        let constraints = normalize_constraints(constraints, self.catalog, &role_scopes)?;
        let id = format!("dev_{}", Uuid::new_v4().simple());
        let result = conn.execute(
            "INSERT INTO devices (
                id, principal_id, platform, model, app_version, public_key_fingerprint,
                constraints, status, paired_at, last_seen_at, revoked_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'active', ?8, NULL, NULL)",
            params![
                id,
                principal_id,
                platform,
                model,
                app_version,
                public_key_fingerprint,
                constraints,
                now,
            ],
        );
        match result {
            Ok(_) => load_device_conn(&conn, &id),
            Err(rusqlite::Error::SqliteFailure(error, _))
                if error.code == ErrorCode::ConstraintViolation =>
            {
                Err(IdentityError::Conflict(format!(
                    "principal {principal_id} já possui device"
                )))
            }
            Err(error) => Err(IdentityError::Sqlite(error)),
        }
    }

    pub fn get_device(&self, device_id: &str) -> Result<Device, IdentityError> {
        let conn = self.lock();
        load_device_conn(&conn, device_id)
    }

    pub fn list_devices(&self, principal_id: Option<&str>) -> Result<Vec<Device>, IdentityError> {
        let conn = self.lock();
        let mut statement = conn.prepare(&format!(
            "SELECT {DEVICE_COLUMNS} FROM devices
             WHERE (?1 IS NULL OR principal_id = ?1)
             ORDER BY paired_at ASC, id ASC"
        ))?;
        let rows = statement.query_map(params![principal_id], device_from_row)?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    pub fn revoke_device(
        &self,
        device_id: &str,
        actor_id: &str,
        reason: &str,
        now: f64,
    ) -> Result<RevocationSummary, IdentityError> {
        let principal_id = {
            let conn = self.lock();
            load_device_conn(&conn, device_id)?.principal_id
        };
        self.revoke_principal_scoped(
            &principal_id,
            actor_id,
            "identity.devices.manage",
            reason,
            now,
        )
    }

    // -- pairing -----------------------------------------------------------

    pub fn create_pairing_session(
        &self,
        new: &NewPairingSession,
        now: f64,
    ) -> Result<PairingSession, IdentityError> {
        validate_now(now)?;
        if !new.ttl_s.is_finite() || new.ttl_s <= 0.0 || new.ttl_s > MAX_PAIRING_TTL_S {
            return Err(IdentityError::Invalid(format!(
                "ttl de pairing deve estar entre 0 e {MAX_PAIRING_TTL_S}s"
            )));
        }
        validate_hash("secret_hash", &new.secret_hash)?;
        if let Some(code_hash) = &new.code_hash {
            validate_hash("code_hash", code_hash)?;
        }
        let conn = self.lock();
        let creator_scopes =
            self.effective_scopes_conn(&conn, &new.created_by, now, &mut BTreeSet::new())?;
        if !creator_scopes.contains("identity.pairing.create") {
            return Err(IdentityError::MissingScope {
                principal_id: new.created_by.clone(),
                scope: "identity.pairing.create".to_string(),
            });
        }
        let role = self.catalog.role(&new.role_id)?;
        let constraints = normalize_constraints(&new.constraints, self.catalog, &role.scopes)?;
        let id = format!("pair_{}", Uuid::new_v4().simple());
        conn.execute(
            "INSERT INTO pairing_sessions (
                id, secret_hash, code_hash, role_id, created_by, constraints, status,
                attempts, max_attempts, created_at, expires_at, consumed_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'pending', 0, ?7, ?8, ?9, NULL)",
            params![
                id,
                new.secret_hash,
                new.code_hash,
                new.role_id,
                new.created_by,
                constraints,
                DEFAULT_PAIRING_ATTEMPTS,
                now,
                now + new.ttl_s,
            ],
        )?;
        load_pairing_conn(&conn, &id)
    }

    pub fn get_pairing_session(&self, session_id: &str) -> Result<PairingSession, IdentityError> {
        let conn = self.lock();
        load_pairing_conn(&conn, session_id)
    }

    /// Consome uma sessão de pairing de forma atômica e single-use.
    ///
    /// Aceita o segredo do QR ou o código de fallback. Prova inválida
    /// incrementa `attempts`; ao atingir `max_attempts`, a sessão expira.
    pub fn consume_pairing_session(
        &self,
        session_id: &str,
        secret_hash: Option<&str>,
        code_hash: Option<&str>,
        now: f64,
    ) -> Result<PairingSession, IdentityError> {
        validate_now(now)?;
        let mut conn = self.lock();
        let tx = conn.transaction()?;
        let mut session = load_pairing_conn(&tx, session_id)?;
        if session.status != "pending" {
            return match session.status.as_str() {
                "consumed" => Err(IdentityError::PairingConsumed),
                "expired" => Err(IdentityError::PairingExpired),
                other => Err(IdentityError::PairingUnavailable(other.to_string())),
            };
        }
        if session.expires_at <= now {
            tx.execute(
                "UPDATE pairing_sessions SET status = 'expired' WHERE id = ?1",
                params![session_id],
            )?;
            tx.commit()?;
            return Err(IdentityError::PairingExpired);
        }
        if session.attempts >= session.max_attempts {
            tx.execute(
                "UPDATE pairing_sessions SET status = 'expired' WHERE id = ?1",
                params![session_id],
            )?;
            tx.commit()?;
            return Err(IdentityError::PairingExpired);
        }

        let secret_ok = secret_hash
            .map(|hash| hashes_equal(hash, &session.secret_hash))
            .unwrap_or(false);
        let code_ok = match (code_hash, session.code_hash.as_deref()) {
            (Some(provided), Some(stored)) => hashes_equal(provided, stored),
            _ => false,
        };
        if !secret_ok && !code_ok {
            let attempts = session.attempts + 1;
            let status = if attempts >= session.max_attempts {
                "expired"
            } else {
                "pending"
            };
            tx.execute(
                "UPDATE pairing_sessions SET attempts = ?2, status = ?3 WHERE id = ?1",
                params![session_id, attempts, status],
            )?;
            tx.commit()?;
            return if status == "expired" {
                Err(IdentityError::PairingExpired)
            } else {
                Err(IdentityError::PairingInvalidProof)
            };
        }

        let updated = tx.execute(
            "UPDATE pairing_sessions SET status = 'consumed', consumed_at = ?2
             WHERE id = ?1 AND status = 'pending'",
            params![session_id, now],
        )?;
        if updated == 0 {
            return Err(IdentityError::PairingConsumed);
        }
        session.status = "consumed".to_string();
        session.consumed_at = Some(now);
        tx.commit()?;
        Ok(session)
    }

    pub fn cancel_pairing_session(
        &self,
        session_id: &str,
        now: f64,
    ) -> Result<bool, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        let updated = conn.execute(
            "UPDATE pairing_sessions SET status = 'cancelled'
             WHERE id = ?1 AND status = 'pending'",
            params![session_id],
        )?;
        Ok(updated > 0)
    }

    pub fn expire_pairing_sessions(&self, now: f64) -> Result<usize, IdentityError> {
        validate_now(now)?;
        let conn = self.lock();
        let updated = conn.execute(
            "UPDATE pairing_sessions SET status = 'expired'
             WHERE status = 'pending' AND expires_at <= ?1",
            params![now],
        )?;
        Ok(updated)
    }

    // -- revogação ---------------------------------------------------------

    pub fn revoke_principal(
        &self,
        principal_id: &str,
        actor_id: &str,
        reason: &str,
        now: f64,
    ) -> Result<RevocationSummary, IdentityError> {
        self.revoke_principal_scoped(
            principal_id,
            actor_id,
            "identity.principals.manage",
            reason,
            now,
        )
    }

    fn revoke_principal_scoped(
        &self,
        principal_id: &str,
        actor_id: &str,
        required_scope: &str,
        reason: &str,
        now: f64,
    ) -> Result<RevocationSummary, IdentityError> {
        validate_now(now)?;
        validate_reason(reason)?;
        let mut conn = self.lock();
        let actor_scopes =
            self.effective_scopes_conn(&conn, actor_id, now, &mut BTreeSet::new())?;
        if !actor_scopes.contains(required_scope) {
            return Err(IdentityError::MissingScope {
                principal_id: actor_id.to_string(),
                scope: required_scope.to_string(),
            });
        }

        let tx = conn.transaction()?;
        load_principal_conn(&tx, principal_id)?;
        let ids: Vec<String> = {
            let mut statement = tx.prepare(
                "WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM principals WHERE id = ?1
                    UNION ALL
                    SELECT p.id FROM principals p
                    JOIN descendants d ON p.parent_id = d.id
                 )
                 SELECT id FROM descendants",
            )?;
            let rows = statement
                .query_map(params![principal_id], |row| row.get(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            rows
        };

        let mut principals = 0;
        let mut credentials = 0;
        let mut devices = 0;
        let mut approvals = 0;
        let mut pairing_sessions = 0;
        for id in &ids {
            principals += tx.execute(
                "UPDATE principals SET status = 'revoked', revoked_at = ?2, updated_at = ?2
                 WHERE id = ?1 AND status != 'revoked'",
                params![id, now],
            )?;
            credentials += tx.execute(
                "UPDATE credentials SET status = 'revoked', revoked_at = ?2
                 WHERE principal_id = ?1 AND status != 'revoked'",
                params![id, now],
            )?;
            devices += tx.execute(
                "UPDATE devices SET status = 'revoked', revoked_at = ?2
                 WHERE principal_id = ?1 AND status != 'revoked'",
                params![id, now],
            )?;
            pairing_sessions += tx.execute(
                "UPDATE pairing_sessions SET status = 'cancelled'
                 WHERE created_by = ?1 AND status = 'pending'",
                params![id],
            )?;
            approvals += tx.execute(
                "UPDATE approvals SET status = 'DENIED', decided_at = ?2,
                        decided_by = ?3, decided_by_principal_id = ?3
                 WHERE principal_id = ?1 AND status = 'PENDING'",
                params![id, now, actor_id],
            )?;
            approvals += tx.execute(
                "UPDATE approvals SET status = 'REVOKED', decided_at = ?2,
                        decided_by = ?3, decided_by_principal_id = ?3
                 WHERE principal_id = ?1 AND status = 'APPROVED'",
                params![id, now, actor_id],
            )?;
        }
        tx.commit()?;
        Ok(RevocationSummary {
            principal_ids: ids,
            principals,
            credentials,
            devices,
            approvals,
            pairing_sessions,
        })
    }
}

fn configure(conn: &Connection) -> Result<(), rusqlite::Error> {
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA synchronous = NORMAL;
         PRAGMA busy_timeout = 5000;
         PRAGMA foreign_keys = ON;",
    )
}

fn principal_from_row(row: &Row<'_>) -> rusqlite::Result<Principal> {
    Ok(Principal {
        id: row.get(0)?,
        kind: row.get(1)?,
        display_name: row.get(2)?,
        role_id: row.get(3)?,
        parent_id: row.get(4)?,
        status: row.get(5)?,
        max_autonomy: row.get(6)?,
        created_at: row.get(7)?,
        updated_at: row.get(8)?,
        revoked_at: row.get(9)?,
        metadata: row.get(10)?,
    })
}

fn credential_from_row(row: &Row<'_>) -> rusqlite::Result<Credential> {
    Ok(Credential {
        id: row.get(0)?,
        principal_id: row.get(1)?,
        kind: row.get(2)?,
        secret_hash: row.get(3)?,
        public_key: row.get(4)?,
        algorithm: row.get(5)?,
        family_id: row.get(6)?,
        rotated_from: row.get(7)?,
        status: row.get(8)?,
        created_at: row.get(9)?,
        last_used_at: row.get(10)?,
        expires_at: row.get(11)?,
        revoked_at: row.get(12)?,
        metadata: row.get(13)?,
    })
}

fn device_from_row(row: &Row<'_>) -> rusqlite::Result<Device> {
    Ok(Device {
        id: row.get(0)?,
        principal_id: row.get(1)?,
        platform: row.get(2)?,
        model: row.get(3)?,
        app_version: row.get(4)?,
        public_key_fingerprint: row.get(5)?,
        constraints: row.get(6)?,
        status: row.get(7)?,
        paired_at: row.get(8)?,
        last_seen_at: row.get(9)?,
        revoked_at: row.get(10)?,
    })
}

fn pairing_from_row(row: &Row<'_>) -> rusqlite::Result<PairingSession> {
    Ok(PairingSession {
        id: row.get(0)?,
        secret_hash: row.get(1)?,
        code_hash: row.get(2)?,
        role_id: row.get(3)?,
        created_by: row.get(4)?,
        constraints: row.get(5)?,
        status: row.get(6)?,
        attempts: row.get(7)?,
        max_attempts: row.get(8)?,
        created_at: row.get(9)?,
        expires_at: row.get(10)?,
        consumed_at: row.get(11)?,
    })
}

fn load_principal_conn(conn: &Connection, principal_id: &str) -> Result<Principal, IdentityError> {
    conn.query_row(
        &format!("SELECT {PRINCIPAL_COLUMNS} FROM principals WHERE id = ?1"),
        params![principal_id],
        principal_from_row,
    )
    .optional()?
    .ok_or_else(|| IdentityError::PrincipalNotFound(principal_id.to_string()))
}

fn load_credential_conn(
    conn: &Connection,
    credential_id: &str,
) -> Result<Credential, IdentityError> {
    conn.query_row(
        &format!("SELECT {CREDENTIAL_COLUMNS} FROM credentials WHERE id = ?1"),
        params![credential_id],
        credential_from_row,
    )
    .optional()?
    .ok_or_else(|| IdentityError::CredentialNotFound(credential_id.to_string()))
}

fn load_device_conn(conn: &Connection, device_id: &str) -> Result<Device, IdentityError> {
    conn.query_row(
        &format!("SELECT {DEVICE_COLUMNS} FROM devices WHERE id = ?1"),
        params![device_id],
        device_from_row,
    )
    .optional()?
    .ok_or_else(|| IdentityError::DeviceNotFound(device_id.to_string()))
}

fn load_pairing_conn(conn: &Connection, session_id: &str) -> Result<PairingSession, IdentityError> {
    conn.query_row(
        &format!("SELECT {PAIRING_COLUMNS} FROM pairing_sessions WHERE id = ?1"),
        params![session_id],
        pairing_from_row,
    )
    .optional()?
    .ok_or_else(|| IdentityError::PairingNotFound(session_id.to_string()))
}

fn role_scopes_conn(
    conn: &Connection,
    catalog: &Catalog,
    role_id: &str,
) -> Result<BTreeSet<String>, IdentityError> {
    let role = catalog.role(role_id)?;
    let mut statement = conn.prepare("SELECT scope FROM role_scopes WHERE role_id = ?1")?;
    let stored: Vec<String> = statement
        .query_map(params![role_id], |row| row.get(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if stored.is_empty() {
        return Err(IdentityError::Invalid(format!(
            "papel {role_id:?} sem escopos no banco"
        )));
    }
    let mut result = BTreeSet::new();
    for scope in stored {
        if !role.scopes.contains(&scope) {
            return Err(IdentityError::PrivilegeEscalation(format!(
                "papel {role_id:?} tem escopo fora do catálogo: {scope:?}"
            )));
        }
        result.insert(scope);
    }
    Ok(result)
}

fn device_constraints_conn(
    conn: &Connection,
    principal_id: &str,
) -> Result<Option<String>, IdentityError> {
    Ok(conn
        .query_row(
            "SELECT constraints FROM devices WHERE principal_id = ?1",
            params![principal_id],
            |row| row.get(0),
        )
        .optional()?)
}

fn constraints_from_raw(raw: &str) -> Result<Constraints, IdentityError> {
    serde_json::from_str(raw)
        .map_err(|error| IdentityError::Invalid(format!("constraints JSON inválido: {error}")))
}

fn normalize_constraints(
    raw: &str,
    catalog: &Catalog,
    role_scopes: &BTreeSet<String>,
) -> Result<String, IdentityError> {
    let normalized = normalize_metadata(raw)?;
    let constraints = constraints_from_raw(&normalized)?;
    if let Some(scopes) = &constraints.scopes {
        for scope in scopes {
            catalog.require_scope(scope)?;
            if !role_scopes.contains(scope) {
                return Err(IdentityError::PrivilegeEscalation(format!(
                    "constraint {scope:?} fora do papel"
                )));
            }
        }
    }
    if let Some(max) = constraints.max_autonomy {
        validate_autonomy(max)?;
    }
    Ok(normalized)
}

/// Escopos de gestão de identidade que impedem a criação de um agente.
///
/// `identity.pairing.create` fica de fora de `*.manage`, mas também permite
/// criar um novo principal e por isso é proibido para agentes.
fn agent_creation_forbidden(scope: &str) -> bool {
    scope == "identity.pairing.create"
        || (scope.starts_with("identity.") && scope.ends_with(".manage"))
}

/// Escopos que um agente nunca pode efetivar.
///
/// Inclui decisão de aprovação, que o papel `family` concede apenas a
/// humanos. A remoção no cálculo de escopo efetivo garante a invariante I4
/// mesmo para papéis que contenham `security.approvals.decide.*`.
fn agent_effective_forbidden(scope: &str) -> bool {
    agent_creation_forbidden(scope) || scope.starts_with("security.approvals.decide.")
}

fn validate_now(now: f64) -> Result<(), IdentityError> {
    if !now.is_finite() || now < 0.0 {
        return Err(IdentityError::Invalid(format!("timestamp inválido: {now}")));
    }
    Ok(())
}

fn validate_kind(kind: &str) -> Result<(), IdentityError> {
    if matches!(kind, "user" | "device" | "agent" | "service") {
        Ok(())
    } else {
        Err(IdentityError::Invalid(format!("kind inválido: {kind:?}")))
    }
}

fn validate_status(status: &str) -> Result<(), IdentityError> {
    if matches!(status, "active" | "suspended" | "revoked") {
        Ok(())
    } else {
        Err(IdentityError::Invalid(format!(
            "status inválido: {status:?}"
        )))
    }
}

fn validate_display_name(value: &str) -> Result<(), IdentityError> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.chars().count() > MAX_DISPLAY_NAME {
        return Err(IdentityError::Invalid(format!(
            "display_name deve ter 1..={MAX_DISPLAY_NAME} caracteres"
        )));
    }
    if trimmed.chars().any(char::is_control) {
        return Err(IdentityError::Invalid(
            "display_name não pode ter caracteres de controle".to_string(),
        ));
    }
    Ok(())
}

fn validate_autonomy(value: u8) -> Result<(), IdentityError> {
    if value <= 4 {
        Ok(())
    } else {
        Err(IdentityError::Invalid(format!(
            "max_autonomy deve estar entre 0 e 4, recebido {value}"
        )))
    }
}

fn validate_reason(value: &str) -> Result<(), IdentityError> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > 512 || trimmed.chars().any(char::is_control) {
        return Err(IdentityError::Invalid(
            "reason deve ter 1..=512 caracteres sem controle".to_string(),
        ));
    }
    Ok(())
}

fn validate_short(field: &str, value: &str, max: usize) -> Result<(), IdentityError> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > max || trimmed.chars().any(char::is_control) {
        return Err(IdentityError::Invalid(format!(
            "{field} deve ter 1..={max} caracteres sem controle"
        )));
    }
    Ok(())
}

fn validate_hash(field: &str, value: &str) -> Result<(), IdentityError> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(IdentityError::Invalid(format!(
            "{field} deve ser SHA-256 em hex (64 caracteres)"
        )));
    }
    Ok(())
}

fn normalize_metadata(raw: &str) -> Result<String, IdentityError> {
    let value: serde_json::Value =
        serde_json::from_str(if raw.trim().is_empty() { "{}" } else { raw })
            .map_err(|error| IdentityError::Invalid(format!("metadata JSON inválido: {error}")))?;
    if !value.is_object() {
        return Err(IdentityError::Invalid(
            "metadata deve ser um objeto JSON".to_string(),
        ));
    }
    let normalized = value.to_string();
    if normalized.len() > MAX_METADATA_BYTES {
        return Err(IdentityError::Invalid(format!(
            "metadata excede {MAX_METADATA_BYTES} bytes"
        )));
    }
    Ok(normalized)
}

fn validate_credential(new: &NewCredential, now: f64) -> Result<(), IdentityError> {
    if let Some(expires) = new.expires_at {
        if !expires.is_finite() || expires <= now {
            return Err(IdentityError::Invalid(
                "expires_at da credencial deve estar no futuro".to_string(),
            ));
        }
    }
    let algorithm_ok = matches!(new.algorithm.as_deref(), Some("p256" | "ed25519"));
    match new.kind.as_str() {
        "device_key" => {
            if new.secret_hash.is_some() {
                return Err(IdentityError::Invalid(
                    "device_key não usa secret_hash".to_string(),
                ));
            }
            let public_key = new
                .public_key
                .as_deref()
                .ok_or_else(|| IdentityError::Invalid("device_key exige public_key".to_string()))?;
            if public_key.trim().is_empty() || public_key.len() > 2048 {
                return Err(IdentityError::Invalid(
                    "public_key inválida para device_key".to_string(),
                ));
            }
            if !algorithm_ok {
                return Err(IdentityError::Invalid(
                    "device_key exige algorithm p256 ou ed25519".to_string(),
                ));
            }
        }
        "access" | "refresh" | "agent" | "local_owner" => {
            if new.public_key.is_some() || new.algorithm.is_some() {
                return Err(IdentityError::Invalid(
                    "credencial simétrica não usa public_key/algorithm".to_string(),
                ));
            }
            let secret_hash = new
                .secret_hash
                .as_deref()
                .ok_or_else(|| IdentityError::Invalid(format!("{} exige secret_hash", new.kind)))?;
            validate_hash("secret_hash", secret_hash)?;
            if new.kind == "refresh" {
                let family = new
                    .family_id
                    .as_deref()
                    .ok_or_else(|| IdentityError::Invalid("refresh exige family_id".to_string()))?;
                validate_short("family_id", family, 128)?;
            }
        }
        other => {
            return Err(IdentityError::Invalid(format!(
                "kind de credencial inválido: {other:?}"
            )))
        }
    }
    Ok(())
}

fn hashes_equal(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (a, b) in left.iter().zip(right) {
        difference |= a ^ b;
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control_schema::{SchemaError, APPROVALS_DDL};
    use rusqlite::params;

    const NOW: f64 = 100.0;

    fn store() -> IdentityStore {
        IdentityStore::open_in_memory().expect("identity store")
    }

    fn owner(store: &IdentityStore) -> Principal {
        store
            .create_principal(&NewPrincipal::new("user", "Owner", "owner", 4), NOW)
            .expect("owner")
    }

    fn family_user(store: &IdentityStore, name: &str) -> Principal {
        store
            .create_principal(&NewPrincipal::new("user", name, "family", 1), NOW)
            .expect("family user")
    }

    fn device_for(
        store: &IdentityStore,
        parent: &Principal,
        max_autonomy: u8,
    ) -> (Principal, Device) {
        let principal = store
            .create_principal(
                &NewPrincipal::new("device", "Xiaomi", "family", max_autonomy)
                    .with_parent(parent.id.clone()),
                NOW,
            )
            .expect("device principal");
        let device = store
            .register_device(
                &principal.id,
                "android",
                Some("Xiaomi"),
                Some("1.0"),
                "fingerprint-1",
                "{}",
                NOW,
            )
            .expect("device");
        (principal, device)
    }

    fn hash(seed: char) -> String {
        seed.to_string().repeat(64)
    }

    #[test]
    fn migrations_create_schema_and_seed_builtin_roles() {
        let store = store();
        assert_eq!(store.schema_version().expect("schema"), 2);
        assert_eq!(store.catalog_version().expect("catalog"), "1.0.0");
        assert_eq!(store.role_scopes("owner").expect("owner").len(), 28);
        assert_eq!(store.role_scopes("family").expect("family").len(), 14);
        assert_eq!(store.role_scopes("guest").expect("guest").len(), 6);
        assert_eq!(store.role_scopes("service").expect("service").len(), 6);
    }

    #[test]
    fn migration_is_idempotent_and_adds_binding_columns() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("control.db");
        {
            let store = IdentityStore::open(&path).expect("open");
            assert_eq!(store.schema_version().expect("schema"), 2);
        }
        {
            let store = IdentityStore::open(&path).expect("reopen");
            assert_eq!(store.count_principals().expect("count"), 0);
        }
        let conn = Connection::open(&path).expect("db");
        let versions: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_migrations", [], |row| {
                row.get(0)
            })
            .expect("versions");
        assert_eq!(versions, 2);
        let roles: i64 = conn
            .query_row("SELECT COUNT(*) FROM roles", [], |row| row.get(0))
            .expect("roles");
        let scopes: i64 = conn
            .query_row("SELECT COUNT(*) FROM role_scopes", [], |row| row.get(0))
            .expect("scopes");
        assert_eq!(roles, 4);
        assert_eq!(scopes, 28 + 14 + 6 + 6);
        let mut statement = conn
            .prepare("PRAGMA table_info(approvals)")
            .expect("table_info");
        let columns: Vec<String> = statement
            .query_map([], |row| row.get(1))
            .expect("columns")
            .collect::<rusqlite::Result<Vec<_>>>()
            .expect("collect");
        for column in [
            "principal_id",
            "device_id",
            "required_scope",
            "decided_by_principal_id",
        ] {
            assert!(columns.contains(&column.to_string()), "coluna {column}");
        }
    }

    #[test]
    fn legacy_approval_db_is_adopted_as_version_one() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("legacy.db");
        {
            let conn = Connection::open(&path).expect("legacy db");
            conn.execute_batch(APPROVALS_DDL).expect("t6 ddl");
        }
        let store = IdentityStore::open(&path).expect("migrate legacy");
        assert_eq!(store.schema_version().expect("schema"), 2);
        drop(store);
        let conn = Connection::open(&path).expect("db");
        let version: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_migrations", [], |row| {
                row.get(0)
            })
            .expect("version");
        assert_eq!(version, 2);
    }

    #[test]
    fn newer_schema_is_rejected() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("future.db");
        {
            let conn = Connection::open(&path).expect("db");
            conn.execute_batch(
                "CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at REAL NOT NULL
                 );
                 INSERT INTO schema_migrations VALUES (99, 'future', 0.0);",
            )
            .expect("future schema");
        }
        let error = match IdentityStore::open(&path) {
            Ok(_) => panic!("schema futuro deveria ser rejeitado"),
            Err(error) => error,
        };
        assert!(matches!(
            error,
            IdentityError::Schema(SchemaError::UnsupportedVersion { found: 99, .. })
        ));
    }

    #[test]
    fn owner_creation_and_effective_scopes() {
        let store = store();
        let owner = owner(&store);
        assert_eq!(owner.kind, "user");
        assert_eq!(owner.status, "active");
        assert_eq!(owner.max_autonomy, 4);
        let scopes = store.effective_scopes(&owner.id, NOW).expect("scopes");
        assert_eq!(scopes.len(), 28);
        assert!(scopes.contains("identity.principals.manage"));
        assert_eq!(store.count_principals().expect("count"), 1);
    }

    #[test]
    fn owner_bootstrap_is_idempotent_and_binds_local_token() {
        let store = store();
        let first = store
            .init_owner("Owner", &hash('a'), NOW)
            .expect("primeiro init");
        assert!(first.created_principal);
        assert!(first.created_credential);
        assert_eq!(first.principal.kind, "user");
        assert_eq!(first.principal.role_id, "owner");
        assert_eq!(first.principal.status, "active");

        let second = store
            .init_owner("Outro Nome", &hash('a'), NOW + 1.0)
            .expect("segundo init");
        assert!(!second.created_principal);
        assert!(!second.created_credential);
        assert_eq!(second.principal.id, first.principal.id);
        assert_eq!(second.credential_id, first.credential_id);
        assert_eq!(store.count_principals().expect("count"), 1);

        let resolved = store
            .local_owner_status(&hash('a'), NOW + 2.0)
            .expect("lookup")
            .expect("owner");
        assert_eq!(resolved.principal.id, first.principal.id);
        assert_eq!(resolved.credential_id, first.credential_id);
        assert!(store
            .local_owner_status(&hash('b'), NOW)
            .expect("lookup ausente")
            .is_none());
    }

    #[test]
    fn owner_bootstrap_rebinds_rotated_token_once() {
        let store = store();
        let first = store.init_owner("Owner", &hash('a'), NOW).expect("init");
        let rebound = store
            .init_owner("Owner", &hash('b'), NOW + 1.0)
            .expect("rebind");
        assert!(!rebound.created_principal);
        assert!(rebound.created_credential);
        assert_eq!(rebound.principal.id, first.principal.id);
        assert_ne!(rebound.credential_id, first.credential_id);
        assert_eq!(
            store
                .get_credential(&first.credential_id)
                .expect("antiga")
                .status,
            "revoked"
        );
        assert!(store
            .local_owner_status(&hash('a'), NOW + 2.0)
            .expect("lookup antigo")
            .is_none());
        assert_eq!(
            store
                .local_owner_status(&hash('b'), NOW + 2.0)
                .expect("lookup novo")
                .expect("owner")
                .principal
                .id,
            first.principal.id
        );

        let again = store
            .init_owner("Owner", &hash('b'), NOW + 3.0)
            .expect("idempotente");
        assert!(!again.created_credential);
        assert_eq!(again.credential_id, rebound.credential_id);
    }

    #[test]
    fn owner_bootstrap_refuses_revoked_owner() {
        let store = store();
        let owner = owner(&store);
        store
            .revoke_principal(&owner.id, &owner.id, "teste", NOW + 1.0)
            .expect("revogar");
        assert!(matches!(
            store.init_owner("Owner", &hash('a'), NOW + 2.0),
            Err(IdentityError::PrincipalNotActive(_))
        ));
        assert!(store
            .local_owner_status(&hash('a'), NOW + 2.0)
            .expect("lookup")
            .is_none());
    }

    #[test]
    fn invalid_principal_inputs_are_rejected() {
        let store = store();
        let cases = [
            NewPrincipal::new("root", "Bad", "owner", 0),
            NewPrincipal::new("user", "", "owner", 0),
            NewPrincipal::new("user", "bad\nname", "owner", 0),
            NewPrincipal::new("user", "Bad", "missing-role", 0),
            NewPrincipal::new("user", "Bad", "owner", 5),
            NewPrincipal::new("user", "Bad", "owner", 0).with_parent("prn_parent"),
        ];
        for input in cases {
            assert!(
                store.create_principal(&input, NOW).is_err(),
                "deveria rejeitar {input:?}"
            );
        }
    }

    #[test]
    fn parent_rules_are_enforced() {
        let store = store();
        let owner = owner(&store);
        let (device, _) = device_for(&store, &owner, 2);

        let no_parent = NewPrincipal::new("device", "Órfão", "family", 0);
        assert!(store.create_principal(&no_parent, NOW).is_err());

        let device_under_device =
            NewPrincipal::new("agent", "Agent", "family", 0).with_parent(device.id.clone());
        assert!(store.create_principal(&device_under_device, NOW).is_err());

        let user_with_parent =
            NewPrincipal::new("user", "User", "family", 0).with_parent(owner.id.clone());
        assert!(store.create_principal(&user_with_parent, NOW).is_err());

        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Agent", "family", 0).with_parent(owner.id.clone()),
                NOW,
            )
            .expect("agent sob user");
        assert_eq!(agent.kind, "agent");
    }

    #[test]
    fn child_role_cannot_exceed_parent() {
        let store = store();
        let wife = family_user(&store, "Wife");
        let too_wide =
            NewPrincipal::new("device", "Tablet", "owner", 0).with_parent(wife.id.clone());
        assert!(matches!(
            store.create_principal(&too_wide, NOW).unwrap_err(),
            IdentityError::PrivilegeEscalation(_)
        ));

        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Helper", "family", 0).with_parent(wife.id.clone()),
                NOW,
            )
            .expect("agent");
        let scopes = store.effective_scopes(&agent.id, NOW).expect("scopes");
        let expected: BTreeSet<String> = store
            .role_scopes("family")
            .expect("family")
            .into_iter()
            .filter(|scope| !scope.starts_with("security.approvals.decide."))
            .collect();
        assert_eq!(scopes, expected);
    }

    #[test]
    fn agents_cannot_manage_identity_or_decide_approvals() {
        let store = store();
        let owner = owner(&store);

        let owner_agent =
            NewPrincipal::new("agent", "AdminAgent", "owner", 0).with_parent(owner.id.clone());
        assert!(matches!(
            store.create_principal(&owner_agent, NOW),
            Err(IdentityError::PrivilegeEscalation(_))
        ));

        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Helper", "family", 1).with_parent(owner.id.clone()),
                NOW,
            )
            .expect("agente family");
        let scopes = store.effective_scopes(&agent.id, NOW).expect("scopes");
        assert!(scopes.contains("agents.spawn"));
        assert!(scopes.contains("tools.read"));
        assert!(!scopes
            .iter()
            .any(|scope| scope.starts_with("security.approvals.decide.")));
        assert!(!scopes.contains("identity.pairing.create"));
        assert!(!scopes
            .iter()
            .any(|scope| scope.starts_with("identity.") && scope.ends_with(".manage")));

        let grant = store.grant_scope(
            &agent.id,
            "security.approvals.decide.self",
            &owner.id,
            None,
            NOW,
        );
        assert!(matches!(grant, Err(IdentityError::PrivilegeEscalation(_))));

        let pairing = store.create_pairing_session(
            &NewPairingSession {
                role_id: "guest".to_string(),
                created_by: agent.id.clone(),
                secret_hash: hash('a'),
                code_hash: None,
                constraints: "{}".to_string(),
                ttl_s: 60.0,
            },
            NOW,
        );
        assert!(matches!(pairing, Err(IdentityError::MissingScope { .. })));
    }

    #[test]
    fn device_registration_and_constraints() {
        let store = store();
        let wife = family_user(&store, "Wife");
        let principal = store
            .create_principal(
                &NewPrincipal::new("device", "Xiaomi", "family", 2).with_parent(wife.id.clone()),
                NOW,
            )
            .expect("device");
        let device = store
            .register_device(
                &principal.id,
                "android",
                Some("Xiaomi"),
                Some("1.0"),
                "fingerprint",
                r#"{"scopes":["tools.read"],"max_autonomy":1}"#,
                NOW,
            )
            .expect("register");
        assert_eq!(device.status, "active");
        assert_eq!(store.list_devices(None).expect("devices").len(), 1);
        assert_eq!(
            store
                .list_devices(Some(&principal.id))
                .expect("devices")
                .len(),
            1
        );
        let scopes = store.effective_scopes(&principal.id, NOW).expect("scopes");
        assert_eq!(scopes.len(), 1);
        assert!(scopes.contains("tools.read"));

        let duplicate =
            store.register_device(&principal.id, "android", None, None, "other", "{}", NOW);
        assert!(matches!(duplicate, Err(IdentityError::Conflict(_))));

        let (other, _) = device_for(&store, &wife, 2);
        let escalation = store.register_device(
            &other.id,
            "android",
            None,
            None,
            "fingerprint-2",
            r#"{"scopes":["system.admin"]}"#,
            NOW,
        );
        assert!(matches!(
            escalation,
            Err(IdentityError::PrivilegeEscalation(_))
        ));
    }

    #[test]
    fn grants_intersect_and_cannot_escalate() {
        let store = store();
        let owner = owner(&store);
        let wife = family_user(&store, "Wife");
        store
            .grant_scope(&wife.id, "memory.read.shared", &owner.id, None, NOW)
            .expect("grant");
        let scopes = store.effective_scopes(&wife.id, NOW).expect("scopes");
        assert_eq!(scopes, BTreeSet::from(["memory.read.shared".to_string()]));

        let escalation = store.grant_scope(&wife.id, "system.admin", &owner.id, None, NOW);
        assert!(matches!(
            escalation,
            Err(IdentityError::PrivilegeEscalation(_))
        ));

        let guest = store
            .create_principal(&NewPrincipal::new("user", "Guest", "guest", 0), NOW)
            .expect("guest");
        let missing = store.grant_scope(&guest.id, "tools.read", &wife.id, None, NOW);
        assert!(matches!(missing, Err(IdentityError::MissingScope { .. })));
    }

    #[test]
    fn expired_grants_narrow_to_empty() {
        let store = store();
        let owner = owner(&store);
        let wife = family_user(&store, "Wife");
        store
            .grant_scope(&wife.id, "tools.read", &owner.id, Some(150.0), NOW)
            .expect("grant");
        assert_eq!(
            store
                .effective_scopes(&wife.id, 120.0)
                .expect("ativo")
                .len(),
            1
        );
        assert!(store
            .effective_scopes(&wife.id, 160.0)
            .expect("expirado")
            .is_empty());
    }

    #[test]
    fn effective_autonomy_is_minimum_of_caps() {
        let store = store();
        let owner = owner(&store);
        let wife = family_user(&store, "Wife");
        let (device, _) = device_for(&store, &wife, 2);
        store
            .lock()
            .execute(
                "UPDATE devices SET constraints = ?2 WHERE principal_id = ?1",
                params![device.id, r#"{"max_autonomy":1}"#],
            )
            .expect("constraints");
        assert_eq!(store.effective_autonomy(&device.id, 3).expect("device"), 1);
        assert_eq!(store.effective_autonomy(&owner.id, 2).expect("owner"), 2);

        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Helper", "family", 4).with_parent(wife.id.clone()),
                NOW,
            )
            .expect("agent");
        assert_eq!(store.effective_autonomy(&agent.id, 4).expect("agent"), 1);
    }

    #[test]
    fn credential_validation_and_lookup() {
        let store = store();
        let owner = owner(&store);
        let access_hash = hash('a');
        let mut input = NewCredential::new(&owner.id, "access");
        input.secret_hash = Some(access_hash.clone());
        input.expires_at = Some(200.0);
        let credential = store.issue_credential(&input, NOW).expect("access");
        assert_eq!(credential.kind, "access");
        let stored = store.get_credential(&credential.id).expect("stored");
        assert_eq!(stored.secret_hash.as_deref(), Some(access_hash.as_str()));
        assert!(store
            .credential_by_hash(&access_hash, 150.0)
            .expect("lookup")
            .is_some());
        assert!(store
            .credential_by_hash(&access_hash, 250.0)
            .expect("expired")
            .is_none());

        let mut device_key = NewCredential::new(&owner.id, "device_key");
        device_key.public_key = Some("public-key".to_string());
        device_key.algorithm = Some("p256".to_string());
        assert!(store.issue_credential(&device_key, NOW).is_ok());

        let missing_public = NewCredential::new(&owner.id, "device_key");
        assert!(store.issue_credential(&missing_public, NOW).is_err());

        let missing_hash = NewCredential::new(&owner.id, "access");
        assert!(store.issue_credential(&missing_hash, NOW).is_err());

        let mut refresh = NewCredential::new(&owner.id, "refresh");
        refresh.secret_hash = Some(hash('b'));
        assert!(store.issue_credential(&refresh, NOW).is_err());

        assert!(store
            .revoke_credential(&credential.id, 150.0)
            .expect("revoke"));
        assert!(store
            .credential_by_hash(&access_hash, 150.0)
            .expect("revoked")
            .is_none());
    }

    #[test]
    fn refresh_rotation_emits_new_credential_in_same_family() {
        let store = store();
        let owner = owner(&store);
        let mut refresh = NewCredential::new(&owner.id, "refresh");
        refresh.secret_hash = Some(hash('a'));
        refresh.family_id = Some("family-1".to_string());
        refresh.expires_at = Some(1000.0);
        let first = store.issue_credential(&refresh, NOW).expect("refresh");

        let second = store
            .rotate_refresh(&hash('a'), &hash('b'), 2000.0, NOW + 1.0)
            .expect("rotate");
        assert_eq!(second.kind, "refresh");
        assert_eq!(second.principal_id, owner.id);
        assert_eq!(second.family_id.as_deref(), Some("family-1"));
        assert_eq!(second.rotated_from.as_deref(), Some(first.id.as_str()));
        assert_eq!(second.expires_at, Some(2000.0));
        assert_eq!(
            store.get_credential(&first.id).expect("old").status,
            "rotated"
        );
        assert!(store
            .credential_by_hash(&hash('a'), NOW + 2.0)
            .expect("old hash")
            .is_none());
        assert!(store
            .credential_by_hash(&hash('b'), NOW + 2.0)
            .expect("new hash")
            .is_some());

        assert!(matches!(
            store.rotate_refresh(&hash('a'), &hash('a'), 3000.0, NOW + 3.0),
            Err(IdentityError::Invalid(_))
        ));
    }

    #[test]
    fn rotated_refresh_reuse_revokes_family_and_device() {
        let store = store();
        let owner = owner(&store);
        let (device_principal, device) = device_for(&store, &owner, 2);
        let mut refresh = NewCredential::new(&device_principal.id, "refresh");
        refresh.secret_hash = Some(hash('a'));
        refresh.family_id = Some("family-1".to_string());
        refresh.expires_at = Some(1000.0);
        let first = store.issue_credential(&refresh, NOW).expect("refresh");
        let mut access = NewCredential::new(&device_principal.id, "access");
        access.secret_hash = Some(hash('b'));
        access.expires_at = Some(1000.0);
        let access = store.issue_credential(&access, NOW).expect("access");

        let second = store
            .rotate_refresh(&hash('a'), &hash('c'), 2000.0, NOW + 1.0)
            .expect("rotate");
        let error = match store.rotate_refresh(&hash('a'), &hash('d'), 3000.0, NOW + 2.0) {
            Ok(_) => panic!("reuse deveria falhar"),
            Err(error) => error,
        };
        match error {
            IdentityError::RefreshReuseDetected {
                family_id,
                principal_id,
            } => {
                assert_eq!(family_id, "family-1");
                assert_eq!(principal_id, device_principal.id);
            }
            other => panic!("erro inesperado: {other}"),
        }
        assert_eq!(
            store.get_credential(&first.id).expect("first").status,
            "revoked"
        );
        assert_eq!(
            store.get_credential(&second.id).expect("second").status,
            "revoked"
        );
        assert_eq!(
            store.get_credential(&access.id).expect("access").status,
            "revoked"
        );
        assert_eq!(
            store.get_device(&device.id).expect("device").status,
            "revoked"
        );
        assert_eq!(
            store
                .get_principal(&device_principal.id)
                .expect("principal")
                .status,
            "revoked"
        );
        assert!(store
            .credential_by_hash(&hash('c'), NOW + 3.0)
            .expect("lookup")
            .is_none());
    }

    #[test]
    fn refresh_cannot_rotate_after_expiry_or_revocation() {
        let store = store();
        let owner = owner(&store);
        let mut refresh = NewCredential::new(&owner.id, "refresh");
        refresh.secret_hash = Some(hash('a'));
        refresh.family_id = Some("family-1".to_string());
        refresh.expires_at = Some(150.0);
        let first = store.issue_credential(&refresh, NOW).expect("refresh");
        assert!(matches!(
            store.rotate_refresh(&hash('a'), &hash('b'), 500.0, 200.0),
            Err(IdentityError::Invalid(_))
        ));
        assert_eq!(
            store.get_credential(&first.id).expect("first").status,
            "expired"
        );

        let mut other = NewCredential::new(&owner.id, "refresh");
        other.secret_hash = Some(hash('c'));
        other.family_id = Some("family-2".to_string());
        other.expires_at = Some(1000.0);
        let revoked = store.issue_credential(&other, NOW).expect("refresh");
        assert!(store
            .revoke_credential(&revoked.id, NOW + 1.0)
            .expect("revoke"));
        assert!(matches!(
            store.rotate_refresh(&hash('c'), &hash('d'), 2000.0, NOW + 2.0),
            Err(IdentityError::Invalid(_))
        ));
    }

    #[test]
    fn duplicate_secret_hash_is_rejected() {
        let store = store();
        let owner = owner(&store);
        let mut first = NewCredential::new(&owner.id, "access");
        first.secret_hash = Some(hash('a'));
        store.issue_credential(&first, NOW).expect("first");
        let mut second = NewCredential::new(&owner.id, "access");
        second.secret_hash = Some(hash('a'));
        assert!(store.issue_credential(&second, NOW).is_err());
    }

    #[test]
    fn pairing_session_is_single_use() {
        let store = store();
        let owner = owner(&store);
        let session = store
            .create_pairing_session(
                &NewPairingSession {
                    role_id: "family".to_string(),
                    created_by: owner.id.clone(),
                    secret_hash: hash('a'),
                    code_hash: Some(hash('b')),
                    constraints: "{}".to_string(),
                    ttl_s: 60.0,
                },
                NOW,
            )
            .expect("pairing");
        assert_eq!(session.status, "pending");
        assert_eq!(session.attempts, 0);

        let wrong = store.consume_pairing_session(&session.id, Some(&hash('c')), None, NOW + 1.0);
        assert!(matches!(wrong, Err(IdentityError::PairingInvalidProof)));
        assert_eq!(
            store
                .get_pairing_session(&session.id)
                .expect("session")
                .attempts,
            1
        );

        let consumed = store
            .consume_pairing_session(&session.id, Some(&hash('a')), None, NOW + 2.0)
            .expect("consume");
        assert_eq!(consumed.status, "consumed");
        assert_eq!(consumed.consumed_at, Some(NOW + 2.0));
        let replay = store.consume_pairing_session(&session.id, Some(&hash('a')), None, NOW + 3.0);
        assert!(matches!(replay, Err(IdentityError::PairingConsumed)));
    }

    #[test]
    fn pairing_attempts_expire_session() {
        let store = store();
        let owner = owner(&store);
        let session = store
            .create_pairing_session(
                &NewPairingSession {
                    role_id: "family".to_string(),
                    created_by: owner.id.clone(),
                    secret_hash: hash('a'),
                    code_hash: None,
                    constraints: "{}".to_string(),
                    ttl_s: 60.0,
                },
                NOW,
            )
            .expect("pairing");
        for attempt in 0..4 {
            let error = store
                .consume_pairing_session(&session.id, Some(&hash('b')), None, NOW + 1.0)
                .unwrap_err();
            assert!(
                matches!(error, IdentityError::PairingInvalidProof),
                "{attempt}"
            );
        }
        let error = store
            .consume_pairing_session(&session.id, Some(&hash('b')), None, NOW + 1.0)
            .unwrap_err();
        assert!(matches!(error, IdentityError::PairingExpired));
        assert_eq!(
            store
                .get_pairing_session(&session.id)
                .expect("session")
                .status,
            "expired"
        );
    }

    #[test]
    fn pairing_expiry_and_cancel() {
        let store = store();
        let owner = owner(&store);
        let input = |ttl_s: f64| NewPairingSession {
            role_id: "family".to_string(),
            created_by: owner.id.clone(),
            secret_hash: hash('a'),
            code_hash: None,
            constraints: "{}".to_string(),
            ttl_s,
        };
        let expired = store
            .create_pairing_session(&input(10.0), NOW)
            .expect("pairing");
        assert!(matches!(
            store.consume_pairing_session(&expired.id, Some(&hash('a')), None, NOW + 11.0),
            Err(IdentityError::PairingExpired)
        ));

        let cancelled = store
            .create_pairing_session(&input(60.0), NOW)
            .expect("pairing");
        assert!(store
            .cancel_pairing_session(&cancelled.id, NOW + 1.0)
            .expect("cancel"));
        assert!(matches!(
            store.consume_pairing_session(&cancelled.id, Some(&hash('a')), None, NOW + 2.0),
            Err(IdentityError::PairingUnavailable(_))
        ));

        let pending = store
            .create_pairing_session(&input(10.0), NOW)
            .expect("pairing");
        assert_eq!(
            store.expire_pairing_sessions(NOW + 20.0).expect("expire"),
            1
        );
        assert_eq!(
            store
                .get_pairing_session(&pending.id)
                .expect("pending")
                .status,
            "expired"
        );
    }

    #[test]
    fn pairing_requires_scope_and_valid_constraints() {
        let store = store();
        let owner = owner(&store);
        let guest = store
            .create_principal(&NewPrincipal::new("user", "Guest", "guest", 0), NOW)
            .expect("guest");
        let unauthorized = store.create_pairing_session(
            &NewPairingSession {
                role_id: "family".to_string(),
                created_by: guest.id.clone(),
                secret_hash: hash('a'),
                code_hash: None,
                constraints: "{}".to_string(),
                ttl_s: 60.0,
            },
            NOW,
        );
        assert!(matches!(
            unauthorized,
            Err(IdentityError::MissingScope { .. })
        ));

        let escalation = store.create_pairing_session(
            &NewPairingSession {
                role_id: "family".to_string(),
                created_by: owner.id.clone(),
                secret_hash: hash('a'),
                code_hash: None,
                constraints: r#"{"scopes":["system.admin"]}"#.to_string(),
                ttl_s: 60.0,
            },
            NOW,
        );
        assert!(matches!(
            escalation,
            Err(IdentityError::PrivilegeEscalation(_))
        ));

        let too_long = store.create_pairing_session(
            &NewPairingSession {
                role_id: "family".to_string(),
                created_by: owner.id.clone(),
                secret_hash: hash('a'),
                code_hash: None,
                constraints: "{}".to_string(),
                ttl_s: 301.0,
            },
            NOW,
        );
        assert!(matches!(too_long, Err(IdentityError::Invalid(_))));
    }

    #[test]
    fn revocation_cascades_and_denies_pending_approvals() {
        let store = store();
        let owner = owner(&store);
        let wife = family_user(&store, "Wife");
        let (device_principal, device) = device_for(&store, &wife, 2);
        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Helper", "family", 0).with_parent(wife.id.clone()),
                NOW,
            )
            .expect("agent");
        let mut credential = NewCredential::new(&device_principal.id, "access");
        credential.secret_hash = Some(hash('d'));
        store
            .issue_credential(&credential, NOW)
            .expect("credential");
        store
            .lock()
            .execute(
                "INSERT INTO approvals (
                    id, task_id, plan_id, tool_id, params_hash, access_level,
                    execution_mode, policy_version, status, created_at, expires_at,
                    principal_id
                 ) VALUES (
                    'appr-1', 'task', 'plan', 'tool', ?1, 'READ',
                    'HOST', '1.0', 'PENDING', ?2, ?3, ?4
                 )",
                params![hash('e'), NOW, 200.0, device_principal.id],
            )
            .expect("approval");

        let summary = store
            .revoke_principal(&wife.id, &owner.id, "device perdido", NOW + 1.0)
            .expect("revoke");
        assert_eq!(summary.principals, 3);
        assert_eq!(summary.credentials, 1);
        assert_eq!(summary.devices, 1);
        assert_eq!(summary.approvals, 1);
        assert_eq!(summary.principal_ids.len(), 3);
        assert_eq!(
            store.get_principal(&wife.id).expect("wife").status,
            "revoked"
        );
        assert_eq!(
            store.get_principal(&agent.id).expect("agent").status,
            "revoked"
        );
        assert_eq!(
            store.get_device(&device.id).expect("device").status,
            "revoked"
        );
        assert!(matches!(
            store.effective_scopes(&agent.id, NOW + 2.0),
            Err(IdentityError::PrincipalNotActive(_))
        ));
        assert!(store
            .credential_by_hash(&hash('d'), NOW + 2.0)
            .expect("credential")
            .is_none());
        let approval_status: String = store
            .lock()
            .query_row(
                "SELECT status FROM approvals WHERE id = 'appr-1'",
                [],
                |row| row.get(0),
            )
            .expect("approval status");
        assert_eq!(approval_status, "DENIED");

        let again = store
            .revoke_principal(&wife.id, &owner.id, "repetição", NOW + 3.0)
            .expect("idempotente");
        assert_eq!(again.principals, 0);
        assert_eq!(again.credentials, 0);
        assert_eq!(again.devices, 0);
        assert_eq!(again.approvals, 0);
    }

    #[test]
    fn revocation_requires_manage_scope_and_reason() {
        let store = store();
        let owner = owner(&store);
        let guest = store
            .create_principal(&NewPrincipal::new("user", "Guest", "guest", 0), NOW)
            .expect("guest");
        assert!(matches!(
            store.revoke_principal(&owner.id, &guest.id, "tentativa", NOW),
            Err(IdentityError::MissingScope { .. })
        ));
        assert!(matches!(
            store.revoke_principal(&owner.id, &owner.id, "", NOW),
            Err(IdentityError::Invalid(_))
        ));
    }

    #[test]
    fn device_can_be_revoked_by_device_id() {
        let store = store();
        let owner = owner(&store);
        let (_, device) = device_for(&store, &owner, 2);
        let summary = store
            .revoke_device(&device.id, &owner.id, "perdido", NOW + 1.0)
            .expect("revoke device");
        assert_eq!(summary.principals, 1);
        assert_eq!(summary.devices, 1);
        assert_eq!(
            store.get_device(&device.id).expect("device").status,
            "revoked"
        );
        assert!(matches!(
            store.effective_scopes(&device.principal_id, NOW + 2.0),
            Err(IdentityError::PrincipalNotActive(_))
        ));
    }

    #[test]
    fn file_store_persists_principal_across_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("identity.db");
        let principal_id = {
            let store = IdentityStore::open(&path).expect("open");
            owner(&store).id
        };
        let store = IdentityStore::open(&path).expect("reopen");
        let principal = store.get_principal(&principal_id).expect("persisted");
        assert_eq!(principal.display_name, "Owner");
        assert_eq!(store.schema_version().expect("schema"), 2);
    }

    #[test]
    fn tampered_role_scope_is_rejected() {
        let store = store();
        let guest = store
            .create_principal(&NewPrincipal::new("user", "Guest", "guest", 0), NOW)
            .expect("guest");
        store
            .lock()
            .execute(
                "INSERT INTO role_scopes (role_id, scope) VALUES ('guest', 'llm.remote')",
                [],
            )
            .expect("tamper");
        assert!(matches!(
            store.effective_scopes(&guest.id, NOW),
            Err(IdentityError::PrivilegeEscalation(_))
        ));
    }

    #[test]
    fn parent_cycle_is_rejected() {
        let store = store();
        let owner = owner(&store);
        let agent = store
            .create_principal(
                &NewPrincipal::new("agent", "Agent", "family", 0).with_parent(owner.id.clone()),
                NOW,
            )
            .expect("agent");
        store
            .lock()
            .execute(
                "UPDATE principals SET parent_id = id WHERE id = ?1",
                params![agent.id],
            )
            .expect("cycle");
        assert!(matches!(
            store.effective_scopes(&agent.id, NOW),
            Err(IdentityError::Invalid(_))
        ));
    }

    #[test]
    fn list_filters_and_metadata_validation() {
        let store = store();
        owner(&store);
        let wife = family_user(&store, "Wife");
        let _ = device_for(&store, &wife, 1);
        assert_eq!(
            store
                .list_principals(Some("device"), None)
                .expect("device")
                .len(),
            1
        );
        assert_eq!(
            store
                .list_principals(None, Some("active"))
                .expect("active")
                .len(),
            3
        );
        assert!(store.list_principals(Some("root"), None).is_err());

        let bad_metadata = NewPrincipal::new("user", "Bad", "family", 0).with_metadata("[]");
        assert!(store.create_principal(&bad_metadata, NOW).is_err());
        let big = format!("{{\"x\":\"{}\"}}", "a".repeat(MAX_METADATA_BYTES));
        let too_big = NewPrincipal::new("user", "Big", "family", 0).with_metadata(big);
        assert!(store.create_principal(&too_big, NOW).is_err());
        let good = NewPrincipal::new("user", "Good", "family", 0).with_metadata(r#"{ "a": 1 }"#);
        let stored = store.create_principal(&good, NOW).expect("metadata");
        assert_eq!(stored.metadata, r#"{"a":1}"#);
    }
}
