use crate::approval::{
    ApprovalManager, DEFAULT_APPROVAL_TTL_SECONDS, DEFAULT_ORPHAN_TIMEOUT_SECONDS,
};
use crate::config::Config;
use crate::events::{EventFabric, EventStore};
use crate::health::check::HealthChecker;
use crate::identity::IdentityStore;
use crate::resources::monitor::ResourceMonitor;
use crate::security::SecurityGuard;
use crate::supervisor::Supervisor;
use std::path::Path;
use std::sync::{Arc, Mutex};
use tracing::{info, warn};

/// Contexto completo do runtime do daemon (espelho do `NexusRuntime` Python).
pub struct DaemonRuntime {
    pub config: Config,
    // Reservados para o T4 (outbox/offsets por assinante no Control Plane).
    #[allow(dead_code)]
    pub store: Arc<EventStore>,
    #[allow(dead_code)]
    pub bus: Arc<crate::events::EventBus>,
    pub fabric: Arc<EventFabric>,
    pub health: Arc<HealthChecker>,
    pub supervisor: Arc<Supervisor>,
    pub security: Arc<Mutex<SecurityGuard>>,
    pub resources: ResourceMonitor,
    pub approvals: Arc<ApprovalManager>,
    #[allow(dead_code)] // consumido pelo IPC/CLI de identidade no T8.3
    pub identity: Arc<IdentityStore>,
    pub started_at: std::time::SystemTime,
}

impl DaemonRuntime {
    #[allow(dead_code)] // usado por health checks futuros do Control Plane
    pub fn health(
        &self,
        resource_stats: Option<&crate::resources::monitor::ResourceStats>,
    ) -> crate::health::check::HealthStatus {
        self.health.check(resource_stats)
    }
}

/// Sequência obrigatória de bootstrap (Fase 1):
/// 1. carregar e validar configuração;
/// 2. configurar logging estruturado;
/// 3. abrir SQLite (event store) e aplicar schema;
/// 4. instanciar barramento de eventos, health checker, supervisor, security guard;
/// 5. registrar serviços supervisionados;
/// 6. iniciar servidor IPC.
///
/// Qualquer falha nesses passos impede a subida do serviço: um runtime meio
/// inicializado é mais perigoso que um serviço parado.
pub async fn bootstrap(config_path: &Path) -> Result<DaemonRuntime, BootstrapError> {
    // 1. Configuração
    let config = crate::config::load_config(config_path)
        .map_err(|e| BootstrapError::Config(e.to_string()))?;
    info!(
        component = "bootstrap",
        version = env!("CARGO_PKG_VERSION"),
        environment = %config.nexus.environment,
        "configuração carregada"
    );

    // 2. Logging
    crate::logging::init(&config.logging.level, config.logging.json)
        .map_err(BootstrapError::Logging)?;
    info!(component = "bootstrap", "logging estruturado inicializado");

    // 3. Event store (SQLite WAL) e approvals persistentes (SQLite WAL)
    let databases_dir = Path::new(&config.paths.data_dir).join("databases");
    std::fs::create_dir_all(&databases_dir).map_err(|e| BootstrapError::Io(e.to_string()))?;
    let db_path = databases_dir.join("nexus_daemon_events.db");
    let store = EventStore::open(&db_path).map_err(BootstrapError::Store)?;
    info!(
        component = "bootstrap",
        db = %db_path.display(),
        "event store aberto"
    );

    let approvals_path = databases_dir.join("nexus_approvals.db");
    let approvals = Arc::new(
        ApprovalManager::open(&approvals_path, DEFAULT_APPROVAL_TTL_SECONDS)
            .map_err(|error| BootstrapError::Approvals(error.to_string()))?,
    );
    let recovered = approvals.recover_orphans(DEFAULT_ORPHAN_TIMEOUT_SECONDS);
    info!(
        component = "bootstrap",
        db = %approvals_path.display(),
        recovered_orphans = recovered,
        "approval store aberto"
    );
    if recovered > 0 {
        warn!(
            component = "bootstrap",
            recovered_orphans = recovered,
            "execuções órfãs foram marcadas como UNKNOWN"
        );
    }

    let identity = Arc::new(
        IdentityStore::open(&approvals_path)
            .map_err(|error| BootstrapError::Identity(error.to_string()))?,
    );
    info!(
        component = "bootstrap",
        db = %approvals_path.display(),
        catalog_version = %identity.catalog().catalog_version,
        schema_version = identity.schema_version().unwrap_or(-1),
        "identity store aberto"
    );

    // 4. Barramento, health, supervisor, segurança
    let bus = Arc::new(crate::events::EventBus::new());
    let store = Arc::new(store);
    let fabric = Arc::new(EventFabric::new(Arc::clone(&store), Arc::clone(&bus)));
    let health = Arc::new(HealthChecker::new(env!("CARGO_PKG_VERSION").to_string()));
    let supervisor = Arc::new(Supervisor::new());
    let security = Arc::new(Mutex::new(SecurityGuard::new()));
    security.lock().unwrap().register_trusted("nexus-daemon");

    // 5. Recursos
    let resources = ResourceMonitor::default();

    // 6. Recuperação de falhas anteriores (best-effort)
    let recovery = crate::recovery::RecoveryManager::new(Arc::clone(&supervisor));
    let _recovered = recovery.recover().await;

    Ok(DaemonRuntime {
        config,
        store,
        bus,
        fabric,
        health,
        supervisor,
        security,
        resources,
        approvals,
        identity,
        started_at: std::time::SystemTime::now(),
    })
}

#[derive(Debug, thiserror::Error)]
pub enum BootstrapError {
    #[error("configuração: {0}")]
    Config(String),
    #[error("logging: {0}")]
    Logging(String),
    #[error("event store: {0}")]
    Store(rusqlite::Error),
    #[error("approvals: {0}")]
    Approvals(String),
    #[error("identity: {0}")]
    Identity(String),
    #[error("IO: {0}")]
    Io(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bootstrap_creates_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("nexus.toml");
        let data_dir = dir.path().join("data");
        std::fs::write(
            &config_path,
            format!(
                r#"
[nexus]
name = "NEXUS"

[paths]
data_dir = "{}"

[logging]
level = "INFO"
json = true

[ipc]
socket_path = "{}"
"#,
                data_dir.display(),
                data_dir.join("run/nexus-daemon.sock").display()
            ),
        )
        .unwrap();
        let runtime = bootstrap(&config_path).await.unwrap();
        assert!(data_dir.join("databases/nexus_approvals.db").is_file());
        assert_eq!(runtime.approvals.len(), 0);
        assert_eq!(runtime.identity.count_principals().unwrap(), 0);
        assert_eq!(runtime.identity.schema_version().unwrap(), 2);
        assert_eq!(runtime.identity.catalog().roles.len(), 4);
    }
}
