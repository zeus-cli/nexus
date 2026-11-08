use crate::config::Config;
use crate::events::EventStore;
use crate::health::check::HealthChecker;
use crate::ipc::IpcServer;
use crate::resources::monitor::ResourceMonitor;
use crate::security::SecurityGuard;
use crate::supervisor::Supervisor;
use std::path::Path;
use std::sync::{Arc, Mutex};
use tracing::{error, info};

/// Contexto completo do runtime do daemon (espelho do `NexusRuntime` Python).
pub struct DaemonRuntime {
    pub config: Config,
    pub store: Arc<EventStore>,
    pub bus: Arc<crate::events::EventBus>,
    pub health: Arc<HealthChecker>,
    pub supervisor: Arc<Supervisor>,
    pub security: Arc<Mutex<SecurityGuard>>,
    pub resources: ResourceMonitor,
    pub started_at: std::time::SystemTime,
}

impl DaemonRuntime {
    pub fn health(&self, resource_stats: Option<&crate::resources::monitor::ResourceStats>) -> crate::health::check::HealthStatus {
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

    // 3. Event store (SQLite WAL)
    let db_path = Path::new(&config.paths.data_dir).join("databases").join("nexus_daemon_events.db");
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| BootstrapError::Io(e.to_string()))?;
    }
    let store = EventStore::open(&db_path).map_err(BootstrapError::Store)?;
    info!(
        component = "bootstrap",
        db = %db_path.display(),
        "event store aberto"
    );

    // 4. Barramento, health, supervisor, segurança
    let bus = Arc::new(crate::events::EventBus::new());
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
        store: Arc::new(store),
        bus,
        health,
        supervisor,
        security,
        resources,
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
        std::fs::write(
            &config_path,
            r#"
[nexus]
name = "NEXUS"

[paths]
data_dir = "data"

[logging]
level = "INFO"
json = true

[ipc]
socket_path = "data/run/nexus-daemon.sock"
"#,
        )
        .unwrap();
        let _rt = bootstrap(&config_path).await.unwrap();
    }
}
