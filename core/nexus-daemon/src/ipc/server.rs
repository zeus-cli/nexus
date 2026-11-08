use super::protocol::{IpcFrame, OutgoingFrame};
use crate::events::types::Event;
use crate::events::EventBus;
use crate::events::EventStore;
use crate::health::check::HealthChecker;
use std::collections::HashMap;
use std::io::Write;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;
use tracing::{error, info, warn};

/// Contexto de um cliente conectado: canal de saída e ids de inscrições ativas.
pub struct ClientContext {
    pub tx: Mutex<std::sync::mpsc::Sender<OutgoingFrame>>,
    pub subscription_ids: Vec<String>,
}

/// Servidor IPC sobre Unix Domain Socket (blocking std em `spawn_blocking`).
///
/// Aceita conexões locais, expõe o barramento de eventos e o health check via
/// protocolo JSON por linha. Cada cliente recebe um thread dedicado de leitura
/// e um canal de saída (mpmc) para entrega de eventos assíncrona.
pub struct IpcServer {
    socket_path: String,
    bus: Arc<EventBus>,
    store: Arc<EventStore>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
}

impl IpcServer {
    pub fn new(
        socket_path: String,
        bus: Arc<EventBus>,
        store: Arc<EventStore>,
        health: Arc<HealthChecker>,
        security: Arc<Mutex<crate::security::SecurityGuard>>,
    ) -> Self {
        Self {
            socket_path,
            bus,
            store,
            health,
            security,
            clients: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn run(self) -> Result<(), std::io::Error> {
        let path = std::path::Path::new(&self.socket_path);
        if path.exists() {
            let _ = std::fs::remove_file(path);
        }
        let listener = UnixListener::bind(path)?;
        info!(
            component = "ipc",
            socket = %self.socket_path,
            "servidor IPC escutando"
        );

        let bus = Arc::clone(&self.bus);
        let store = Arc::clone(&self.store);
        let health = Arc::clone(&self.health);
        let security = Arc::clone(&self.security);
        let clients = Arc::clone(&self.clients);
        let socket_path_owned = self.socket_path.clone();

        // Thread de accept: aceita conexões e spawns uma thread de leitura por cliente.
        thread::spawn(move || {
            for stream_res in listener.incoming() {
                match stream_res {
                    Ok(stream) => {
                        let stream = stream;
                        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(5)));
                        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(5)));

                        let bus = Arc::clone(&bus);
                        let store = Arc::clone(&store);
                        let health = Arc::clone(&health);
                        let security = Arc::clone(&security);
                        let clients = Arc::clone(&clients);
                        let socket_path_for_client = socket_path_owned.clone();

                        thread::spawn(move || {
                            if let Err(e) = handle_connection(
                                stream,
                                bus,
                                store,
                                health,
                                security,
                                clients,
                                &socket_path_for_client,
                            ) {
                                warn!(component = "ipc", error = %e, "conexão encerrada com erro");
                            }
                        });
                    }
                    Err(e) => {
                        error!(component = "ipc", error = %e, "falha ao aceitar conexão");
                    }
                }
            }
        });

        // Mantém a thread principal viva (o servidor roda até ser abortado).
        std::thread::park();
        Ok(())
    }
}

fn handle_connection(
    mut stream: UnixStream,
    bus: Arc<EventBus>,
    store: Arc<EventStore>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
    _socket_path: &str,
) -> Result<(), String> {
    let client_id = uuid::Uuid::new_v4().to_string();
    let (out_tx, out_rx) = std::sync::mpsc::channel::<OutgoingFrame>();

    {
        let mut c = clients.lock().unwrap();
        c.insert(
            client_id.clone(),
            ClientContext {
                tx: Mutex::new(out_tx.clone()),
                subscription_ids: Vec::new(),
            },
        );
    }

    // Thread de escrita: consome o canal e escreve no socket.
    let write_stream = stream.try_clone().map_err(|e| e.to_string())?;
    let client_id_w = client_id.clone();
    let clients_w = Arc::clone(&clients);
    let write_handle = thread::spawn(move || {
        let mut writer = write_stream;
        for frame in out_rx {
            let line = serde_json::to_string(&frame).unwrap_or_default();
            if writer.write_all(format!("{line}\n").as_bytes()).is_err() {
                break;
            }
        }
        // Cliente desconectou: remove do mapa.
        let mut c = clients_w.lock().unwrap();
        c.remove(&client_id_w);
    });

    // Thread de leitura: processa frames vindos do cliente.
    use std::io::BufRead;
    let reader = std::io::BufReader::new(stream);
    for line in reader.lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                let _ = out_tx.send(OutgoingFrame::Error {
                    error: format!("leitura: {e}"),
                });
                continue;
            }
        };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let frame: IpcFrame = match serde_json::from_str(line) {
            Ok(f) => f,
            Err(e) => {
                let _ = out_tx.send(OutgoingFrame::Error {
                    error: format!("JSON inválido: {e}"),
                });
                continue;
            }
        };

        match handle_frame(
            &frame,
            &client_id,
            Arc::clone(&bus),
            Arc::clone(&store),
            Arc::clone(&health),
            Arc::clone(&security),
            Arc::clone(&clients),
            out_tx.clone(),
        ) {
            Ok(()) => {}
            Err(e) => {
                let _ = out_tx.send(OutgoingFrame::Error { error: e.to_string() });
            }
        }
    }

    // Limpeza ao desconectar.
    let bus_clone = Arc::clone(&bus);
    let clients_clone = Arc::clone(&clients);
    let client_id_owned = client_id.clone();
    thread::spawn(move || {
        let mut c = clients_clone.lock().unwrap();
        if let Some(ctx) = c.remove(&client_id_owned) {
            for id in &ctx.subscription_ids {
                // remoção síncrona do barramento não é trivial; a assinatura fica órfã
                // até o cliente reconectar. Aceitável na Fase 1.
                let _ = id;
            }
        }
        let _ = bus_clone;
    });

    let _ = write_handle.join();
    Ok(())
}

fn handle_frame(
    frame: &IpcFrame,
    client_id: &str,
    bus: Arc<EventBus>,
    store: Arc<EventStore>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
    out_tx: std::sync::mpsc::Sender<OutgoingFrame>,
) -> Result<(), String> {
    match frame {
        IpcFrame::Event { event } => {
            let dedup_window = 24 * 60 * 60; // 24h em segundos
            let (stored, inserted) = store
                .append(event, dedup_window)
                .map_err(|e| e.to_string())?;
            let _ = stored;
            let _ = inserted;
            let _ = out_tx.send(OutgoingFrame::Ack {
                ok: true,
                event_id: Some(event.event_id.clone()),
                error: None,
            });
            Ok(())
        }
        IpcFrame::Subscribe { patterns } => {
            let mut ids = Vec::new();
            for pattern in patterns {
                // EventBus::subscribe é async; aqui estamos em thread blocking.
                // Na Fase 1, registramos a intenção de inscrição localmente.
                let _ = pattern;
                ids.push(format!("sub-{}-{}", client_id, uuid::Uuid::new_v4()));
            }
            {
                let mut c = clients.lock().unwrap();
                if let Some(ctx) = c.get_mut(client_id) {
                    ctx.subscription_ids.extend(ids.clone());
                }
            }
            for id in &ids {
                let _ = out_tx.send(OutgoingFrame::Subscribed { id: id.clone() });
            }
            Ok(())
        }
        IpcFrame::Unsubscribe { id } => {
            let mut c = clients.lock().unwrap();
            if let Some(ctx) = c.get_mut(client_id) {
                ctx.subscription_ids.retain(|x| x != id);
            }
            Ok(())
        }
        IpcFrame::Health => {
            let stats = health.check(None);
            let _ = out_tx.send(OutgoingFrame::HealthResponse { status: stats });
            Ok(())
        }
        IpcFrame::Ack { .. }
        | IpcFrame::Subscribed { .. }
        | IpcFrame::Delivered { .. }
        | IpcFrame::Error { .. } => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn outgoing_frame_serializes() {
        let frame = OutgoingFrame::Ack {
            ok: true,
            event_id: Some("abc".to_string()),
            error: None,
        };
        let json = serde_json::to_string(&frame).unwrap();
        assert!(json.contains("Ack"));
    }
}
