mod bootstrap;
mod config;
#[cfg(test)]
mod contracts;
mod events;
mod execution;
mod health;
mod ipc;
mod logging;
mod policy;
mod recovery;
mod resources;
mod security;
mod supervisor;

use crate::bootstrap::bootstrap;
use crate::events::types::{Event, Severity};
use crate::ipc::IpcServer;
use std::path::Path;
use std::sync::Arc;
use tokio::time::{sleep, Duration};
use tracing::{error, info, warn};

const HEARTBEAT_INTERVAL_SEC: u64 = 60;

/// Ponto de entrada do daemon.
///
/// Fluxo:
/// 1. bootstrap (config, logging, store, bus, saúde, supervisor, segurança);
/// 2. registra `system.started`;
/// 3. inicia servidor IPC (UDS) com `spawn` (bind antes do retorno);
/// 4. inicia loop de heartbeat + monitoramento de recursos;
/// 5. aguarda encerramento (sinal ou variável `NEXUS_DAEMON_ONCE`);
/// 6. shutdown gracioso: para heartbeat e serviços supervisionados, registra
///    `system.stopped`; o servidor IPC termina com o processo.
#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() {
    if let Err(e) = run().await {
        error!(component = "nexus-daemon", error = %e, "daemon encerrado com erro");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let config_path = std::env::var_os("NEXUS_CONFIG")
        .map(|v| Path::new(&v).to_path_buf())
        .unwrap_or_else(|| Path::new("config/nexus.toml").to_path_buf());

    let runtime = bootstrap(&config_path).await?;
    info!(
        component = "nexus-daemon",
        version = env!("CARGO_PKG_VERSION"),
        "daemon inicializado"
    );

    // Registra evento de início via fabric: store antes de publicar no bus.
    let _ = runtime
        .fabric
        .emit(
            Event::new(
                "system.started",
                "nexus-daemon",
                serde_json::json!({
                    "version": env!("CARGO_PKG_VERSION"),
                    "environment": runtime.config.nexus.environment,
                }),
            )
            .with_severity(Severity::Info)
            .with_dedup(false),
            86400,
        )
        .await;

    // Inicia servidor IPC (UDS). `spawn` faz o bind antes de retornar, então
    // clientes podem conectar assim que esta chamada conclui.
    let server = IpcServer::new(
        runtime.config.ipc.socket_path.clone(),
        Arc::clone(&runtime.fabric),
        Arc::clone(&runtime.health),
        Arc::clone(&runtime.security),
        tokio::runtime::Handle::current(),
    );
    server.spawn()?;

    // Loop de heartbeat + monitoramento de recursos.
    let heartbeat_handle = {
        let fabric = Arc::clone(&runtime.fabric);
        let resources = runtime.resources.clone();
        let started_at = runtime.started_at;
        tokio::spawn(async move {
            loop {
                sleep(Duration::from_secs(HEARTBEAT_INTERVAL_SEC)).await;

                let stats = resources.get_stats();
                let uptime_s = started_at.elapsed().map(|d| d.as_secs_f64()).unwrap_or(0.0);
                let _ = fabric
                    .emit(
                        Event::new(
                            "system.heartbeat",
                            "nexus-daemon",
                            serde_json::json!({
                                "uptime_s": uptime_s,
                                "pressure": stats.pressure,
                                "ram_percent": stats.ram.percent,
                                "swap_percent": stats.swap.percent,
                            }),
                        )
                        .with_severity(Severity::Info)
                        .with_dedup(false),
                        86400,
                    )
                    .await;

                if stats.pressure == "CRITICAL" {
                    warn!(component = "nexus-daemon", pressao = %stats.pressure, "pressão crítica de recursos detectada");
                }
            }
        })
    };

    // Condição de parada: variável NEXUS_DAEMON_ONCE (executa 1 ciclo e sai)
    // ou aguarda indefinidamente (produção). `NEXUS_DAEMON_HOLD_MS` estende a
    // janela do modo once para testes de integração com clientes IPC.
    let once = std::env::var("NEXUS_DAEMON_ONCE").is_ok();
    if once {
        info!(
            component = "nexus-daemon",
            modo = "once",
            "executando um ciclo de heartbeat"
        );
        sleep(Duration::from_secs(2)).await;
        if let Ok(raw) = std::env::var("NEXUS_DAEMON_HOLD_MS") {
            match raw.parse::<u64>() {
                Ok(ms) if ms > 0 => {
                    info!(
                        component = "nexus-daemon",
                        hold_ms = ms,
                        "modo once aguardando clientes IPC"
                    );
                    sleep(Duration::from_millis(ms)).await;
                }
                Ok(_) => {}
                Err(_) => warn!(
                    component = "nexus-daemon",
                    value = %raw,
                    "NEXUS_DAEMON_HOLD_MS inválido; ignorado"
                ),
            }
        }
    } else {
        // Produção: aguarda indefinidamente. O encerramento ocorre por sinal do SO
        // (SIGTERM/SIGINT) — o tratamento de sinais fica para quando o tokio
        // `unix` feature estiver disponível neste ambiente.
        std::future::pending::<()>().await;
    }

    // Shutdown gracioso.
    heartbeat_handle.abort();

    // Para serviços supervisionados.
    for info in runtime.supervisor.list().await {
        if let Err(e) = runtime.supervisor.stop(&info.name).await {
            warn!(component = "nexus-daemon", service = %info.name, erro = %e, "falha ao parar serviço");
        }
    }

    // Registra evento de parada via fabric.
    let _ = runtime
        .fabric
        .emit(
            Event::new("system.stopped", "nexus-daemon", serde_json::json!({
                "uptime_s": runtime.started_at.elapsed().map(|d| d.as_secs_f64()).unwrap_or(0.0),
            }))
            .with_severity(Severity::Info)
            .with_dedup(false),
            86400,
        )
        .await;

    info!(component = "nexus-daemon", "shutdown concluído");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_interval_constant() {
        assert_eq!(HEARTBEAT_INTERVAL_SEC, 60);
    }
}
