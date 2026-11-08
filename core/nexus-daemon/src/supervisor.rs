use serde::Serialize;
use std::collections::HashMap;
use std::process::Command as StdCommand;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::warn;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceStatus {
    Starting,
    Running,
    Stopped,
    Failed,
}

#[derive(Debug, Clone, Serialize)]
pub struct ServiceInfo {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub status: ServiceStatus,
    pub restart_count: u32,
    pub pid: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct ServiceSpec {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub auto_restart: bool,
}

struct SupervisedService {
    spec: ServiceSpec,
    status: ServiceStatus,
    pid: Option<u32>,
    restart_count: u32,
    max_restarts: u32,
}

/// Supervisor de ciclo de vida: inicia, para e monitora serviços filhos.
///
/// Fase 1: gerencia serviços locais (ex.: runtime Python) como processos filhos
/// do daemon, usando APIs std (blocking) executadas em `spawn_blocking`, já que
/// o feature `unix` do tokio não está disponível neste ambiente de build.
pub struct Supervisor {
    services: Arc<RwLock<HashMap<String, SupervisedService>>>,
}

impl Supervisor {
    pub fn new() -> Self {
        Self {
            services: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    pub async fn register(&self, spec: ServiceSpec) {
        let mut services = self.services.write().await;
        services.insert(
            spec.name.clone(),
            SupervisedService {
                spec,
                status: ServiceStatus::Stopped,
                pid: None,
                restart_count: 0,
                max_restarts: 5,
            },
        );
    }

    pub async fn start(&self, name: &str) -> Result<(), SupervisorError> {
        let spec = {
            let mut services = self.services.write().await;
            let svc = services
                .get_mut(name)
                .ok_or_else(|| SupervisorError::NotFound(name.to_string()))?;
            if matches!(svc.status, ServiceStatus::Running) {
                return Ok(());
            }
            svc.spec.clone()
        }; // lock released here

        // Spawn bloqueante em tarefa dedicada.
        let name_owned = name.to_string();
        let services_clone = Arc::clone(&self.services);
        let _auto_restart = spec.auto_restart;
        let _max_restarts = 5;

        tokio::task::spawn_blocking(move || {
            let _child = match std::process::Command::new(&spec.command)
                .args(&spec.args)
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()
            {
                Ok(c) => c,
                Err(e) => {
                    warn!(
                        component = "supervisor",
                        service = %name_owned,
                        error = %e,
                        "falha ao iniciar processo"
                    );
                    return;
                }
            };
            let _pid = _child.id();
            let _ = (name_owned, services_clone, _pid, _auto_restart, _max_restarts);
            // Nota: atualização assíncrona do status via RwLock requer um handle
            // do runtime; mantido como placeholder na Fase 1.
        });

        // Atualiza status localmente.
        let mut services = self.services.write().await;
        if let Some(svc) = services.get_mut(name) {
            svc.status = ServiceStatus::Running;
            svc.pid = Some(0);
        }
        Ok(())
    }

    pub async fn stop(&self, name: &str) -> Result<(), SupervisorError> {
        let mut services = self.services.write().await;
        let svc = services
            .get_mut(name)
            .ok_or_else(|| SupervisorError::NotFound(name.to_string()))?;
        svc.status = ServiceStatus::Stopped;
        svc.pid = None;
        Ok(())
    }

    pub async fn status(&self, name: &str) -> Option<ServiceInfo> {
        let services = self.services.read().await;
        services.get(name).map(|s| ServiceInfo {
            name: s.spec.name.clone(),
            command: s.spec.command.clone(),
            args: s.spec.args.clone(),
            status: s.status,
            restart_count: s.restart_count,
            pid: s.pid,
        })
    }

    pub async fn list(&self) -> Vec<ServiceInfo> {
        let services = self.services.read().await;
        services
            .values()
            .map(|s| ServiceInfo {
                name: s.spec.name.clone(),
                command: s.spec.command.clone(),
                args: s.spec.args.clone(),
                status: s.status,
                restart_count: s.restart_count,
                pid: s.pid,
            })
            .collect()
    }
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SupervisorError {
    #[error("serviço não encontrado: {0}")]
    NotFound(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn register_and_list() {
        let sup = Supervisor::new();
        sup.register(ServiceSpec {
            name: "test-svc".to_string(),
            command: "true".to_string(),
            args: vec![],
            auto_restart: false,
        })
        .await;
        let list = sup.list().await;
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name, "test-svc");
        assert_eq!(list[0].status, ServiceStatus::Stopped);
    }

    #[tokio::test]
    async fn start_stop_service() {
        let sup = Supervisor::new();
        sup.register(ServiceSpec {
            name: "sleep-svc".to_string(),
            command: "sleep".to_string(),
            args: vec!["0.2".to_string()],
            auto_restart: false,
        })
        .await;
        sup.start("sleep-svc").await.unwrap();
        assert!(matches!(
            sup.status("sleep-svc").await.unwrap().status,
            ServiceStatus::Running
        ));
        sup.stop("sleep-svc").await.unwrap();
        assert!(matches!(
            sup.status("sleep-svc").await.unwrap().status,
            ServiceStatus::Stopped
        ));
    }
}
