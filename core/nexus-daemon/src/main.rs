mod bootstrap;
mod config;
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

use crate::bootstrap::{bootstrap, DaemonRuntime};
use crate::events::types::{Event, Severity};
use crate::ipc::IpcServer;
use crate::resources::monitor::ResourceMonitor;
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
/// 3. inicia servidor IPC (UDS) numa thread dedicada;
/// 4. inicia loop de heartbeat + monitoramento de recursos;
/// 5. aguarda encerramento (sinal ou variável `NEXUS_DAEMON_ONCE`);
/// 6. shutdown gracioso: para heartbeat, servidor IPC, serviços supervisionados,
///    registra `system.stopped`.
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

    // Registra evento de início no store.
    let _ = runtime.store.append(
        &Event::new("system.started", "nexus-daemon", serde_json::json!({
            "version": env!("CARGO_PKG_VERSION"),
            "environment": runtime.config.nexus.environment,
        }))
        .with_severity(Severity::Info)
        .with_dedup(false),
        86400,
    );

    // Inicia servidor IPC (UDS) numa thread dedicada (blocking std).
    let socket_path = runtime.config.ipc.socket_path.clone();
    let bus = Arc::clone(&runtime.bus);
    let store = Arc::clone(&runtime.store);
    let health = Arc::clone(&runtime.health);
    let security = Arc::clone(&runtime.security);
    let ipc_handle = std::thread::spawn(move || {
        let server = IpcServer::new(socket_path, bus, store, health, security);
        let _ = server.run();
    });

    // Loop de heartbeat + monitoramento de recursos.
    let heartbeat_handle = {
        let store = Arc::clone(&runtime.store);
        let bus = Arc::clone(&runtime.bus);
        let resources = runtime.resources.clone();
        let started_at = runtime.started_at;
        tokio::spawn(async move {
            let mut interval = sleep(Duration::from_secs(HEARTBEAT_INTERVAL_SEC));
            loop {
                interval = sleep(Duration::from_secs(HEARTBEAT_INTERVAL_SEC));
                interval.await;

                let stats = resources.get_stats();
                let uptime_s = started_at.elapsed().map(|d| d.as_secs_f64()).unwrap_or(0.0);
                let _ = store.append(
                    &Event::new("system.heartbeat", "nexus-daemon", serde_json::json!({
                        "uptime_s": uptime_s,
                        "pressure": stats.pressure,
                        "ram_percent": stats.ram.percent,
                        "swap_percent": stats.swap.percent,
                    }))
                    .with_severity(Severity::Info)
                    .with_dedup(false),
                    86400,
                );
                let _ = bus.publish(Event::new("system.heartbeat", "nexus-daemon", serde_json::json!({}))).await;

                if stats.pressure == "CRITICAL" {
                    warn!(component = "nexus-daemon", pressao = %stats.pressure, "pressão crítica de recursos detectada");
                }
            }
        })
    };

    // Condição de parada: variável NEXUS_DAEMON_ONTE (executa 1 ciclo e sai)
    // ou aguarda indefinidamente (produção).
    let once = std::env::var("NEXUS_DAEMON_ONCE").is_ok();
    if once {
        info!(component = "nexus-daemon", modo = "once", "executando um ciclo de heartbeat");
        sleep(Duration::from_secs(2)).await;
    } else {
        // Produção: aguarda indefinidamente. O encerramento ocorre por sinal do SO
        // (SIGTERM/SIGINT) — o tratamento de sinais fica para quando o tokio
        // `unix` feature estiver disponível neste ambiente.
        std::future::pending::<()>().await;
    }

    // Shutdown gracioso.
    heartbeat_handle.abort();

    // O servidor IPC roda em thread própria; termina com o processo.
    let _ = ipc_handle;

    // Para serviços supervisionados.
    for info in runtime.supervisor.list().await {
        if let Err(e) = runtime.supervisor.stop(&info.name).await {
            warn!(component = "nexus-daemon", service = %info.name, erro = %e, "falha ao parar serviço");
        }
    }

    // Registra evento de parada.
    let _ = runtime.store.append(
        &Event::new("system.stopped", "nexus-daemon", serde_json::json!({
            "uptime_s": runtime.started_at.elapsed().map(|d| d.as_secs_f64()).unwrap_or(0.0),
        }))
        .with_severity(Severity::Info)
        .with_dedup(false),
        86400,
    );

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
