use crate::supervisor::Supervisor;
use tracing::{info, warn};

/// Recuperação de falhas da Fase 1.
///
/// Detecta serviços em estado `Failed` e tenta reiniciá-los (respeitando o
/// limite de reinícios do supervisor). Também pode ser usado para recuperar
/// aprovações órfãs (delegado ao Python na Fase 1, integrado na Fase 3).
pub struct RecoveryManager {
    supervisor: std::sync::Arc<crate::supervisor::Supervisor>,
}

impl RecoveryManager {
    pub fn new(supervisor: std::sync::Arc<crate::supervisor::Supervisor>) -> Self {
        Self { supervisor }
    }

    /// Tenta recuperar serviços com falha. Retorna quantos foram reiniciados.
    pub async fn recover(&self) -> u32 {
        let list = self.supervisor.list().await;
        let mut recovered = 0u32;
        for info in list {
            if matches!(info.status, crate::supervisor::ServiceStatus::Failed) {
                warn!(
                    component = "recovery",
                    service = %info.name,
                    "serviço com falha detectado; tentando reiniciar"
                );
                if self.supervisor.start(&info.name).await.is_ok() {
                    info!(
                        component = "recovery",
                        service = %info.name,
                        "serviço reiniciado com sucesso"
                    );
                    recovered += 1;
                } else {
                    warn!(
                        component = "recovery",
                        service = %info.name,
                        "falha ao reiniciar serviço"
                    );
                }
            }
        }
        recovered
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn recover_no_failed_services() {
        let sup = std::sync::Arc::new(crate::supervisor::Supervisor::new());
        sup.register(crate::supervisor::ServiceSpec {
            name: "svc".to_string(),
            command: "true".to_string(),
            args: vec![],
            auto_restart: false,
        })
        .await;
        let rec = RecoveryManager::new(sup);
        assert_eq!(rec.recover().await, 0);
    }
}
