use super::protocol::{IpcFrame, OutgoingFrame};
use crate::events::bus::Pattern;
use crate::events::types::Event;
use crate::events::{EventBus, EventFabric};
use crate::health::check::HealthChecker;
use std::collections::HashMap;
use std::io::Write;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;
use tracing::{error, info, warn};

/// Contexto de um cliente conectado: ids de inscrições ativas.
pub struct ClientContext {
    pub subscription_ids: Vec<String>,
}

/// Servidor IPC sobre Unix Domain Socket (threads dedicadas, bloqueantes).
///
/// Aceita conexões locais, expõe o barramento de eventos e o health check via
/// protocolo JSON por linha. Cada cliente recebe um thread dedicado de leitura
/// e um canal de saída (mpmc) para entrega de eventos assíncrona.
pub struct IpcServer {
    socket_path: String,
    bus: Arc<EventBus>,
    fabric: Arc<EventFabric>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    handle: tokio::runtime::Handle,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
}

impl IpcServer {
    pub fn new(
        socket_path: String,
        fabric: Arc<EventFabric>,
        health: Arc<HealthChecker>,
        security: Arc<Mutex<crate::security::SecurityGuard>>,
        handle: tokio::runtime::Handle,
    ) -> Self {
        Self {
            socket_path,
            bus: Arc::clone(fabric.bus()),
            fabric,
            health,
            security,
            handle,
            clients: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Faz bind e inicia a thread de accept, retornando imediatamente.
    ///
    /// O bind acontece antes do retorno, então testes e clientes podem
    /// conectar assim que o método conclui.
    pub fn spawn(self) -> Result<(), std::io::Error> {
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
        let fabric = Arc::clone(&self.fabric);
        let health = Arc::clone(&self.health);
        let security = Arc::clone(&self.security);
        let clients = Arc::clone(&self.clients);
        let handle = self.handle.clone();

        // Thread de accept: aceita conexões e spawns uma thread de leitura por cliente.
        thread::spawn(move || {
            for stream_res in listener.incoming() {
                match stream_res {
                    Ok(stream) => {
                        let stream = stream;
                        // Sem read timeout: uma assinatura ociosa é normal e não
                        // deve gerar frames de erro espúrios. O write timeout
                        // permanece para não bloquear em clientes que pararam
                        // de ler.
                        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(5)));

                        let bus = Arc::clone(&bus);
                        let fabric = Arc::clone(&fabric);
                        let health = Arc::clone(&health);
                        let security = Arc::clone(&security);
                        let clients = Arc::clone(&clients);
                        let handle = handle.clone();

                        thread::spawn(move || {
                            if let Err(e) = handle_connection(
                                stream, bus, fabric, health, security, clients, handle,
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

        Ok(())
    }

    /// Mantém a thread principal viva (servidor em produção).
    #[allow(dead_code)]
    pub fn run(self) -> Result<(), std::io::Error> {
        self.spawn()?;
        std::thread::park();
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_connection(
    stream: UnixStream,
    bus: Arc<EventBus>,
    fabric: Arc<EventFabric>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
    handle: tokio::runtime::Handle,
) -> Result<(), String> {
    let client_id = uuid::Uuid::new_v4().to_string();
    let (out_tx, out_rx) = std::sync::mpsc::channel::<OutgoingFrame>();

    {
        let mut c = clients.lock().unwrap();
        c.insert(
            client_id.clone(),
            ClientContext {
                subscription_ids: Vec::new(),
            },
        );
    }

    // Thread de escrita: consome o canal e escreve no socket.
    let write_stream = stream.try_clone().map_err(|e| e.to_string())?;
    let clients_w = Arc::clone(&clients);
    let client_id_w = client_id.clone();
    let write_handle = thread::spawn(move || {
        let mut writer = write_stream;
        for frame in out_rx {
            let line = serde_json::to_string(&frame).unwrap_or_default();
            if writer.write_all(format!("{line}\n").as_bytes()).is_err() {
                break;
            }
        }
        // Cliente desconectou: remove do mapa (a limpeza de assinaturas já
        // aconteceu no fim da thread de leitura).
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
                break;
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
            Arc::clone(&fabric),
            Arc::clone(&health),
            Arc::clone(&security),
            Arc::clone(&clients),
            handle.clone(),
            out_tx.clone(),
        ) {
            Ok(()) => {}
            Err(e) => {
                let _ = out_tx.send(OutgoingFrame::Error {
                    error: e.to_string(),
                });
            }
        }
    }

    // Limpeza ao desconectar: remove assinaturas do barramento antes de
    // fechar o canal de saída, senão as threads de entrega ficariam órfãs.
    let subscription_ids = {
        let mut c = clients.lock().unwrap();
        c.remove(&client_id)
            .map(|ctx| ctx.subscription_ids)
            .unwrap_or_default()
    };
    for id in subscription_ids {
        handle.block_on(bus.unsubscribe(&id));
    }
    drop(out_tx);
    let _ = write_handle.join();
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn handle_frame(
    frame: &IpcFrame,
    client_id: &str,
    bus: Arc<EventBus>,
    fabric: Arc<EventFabric>,
    health: Arc<HealthChecker>,
    _security: Arc<Mutex<crate::security::SecurityGuard>>,
    clients: Arc<Mutex<HashMap<String, ClientContext>>>,
    handle: tokio::runtime::Handle,
    out_tx: std::sync::mpsc::Sender<OutgoingFrame>,
) -> Result<(), String> {
    match frame {
        IpcFrame::Event { event } => {
            let dedup_window = 24 * 60 * 60; // 24h em segundos
            handle
                .block_on(fabric.emit(event.clone(), dedup_window))
                .map_err(|e| e.to_string())?;
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
                Pattern::new(pattern).map_err(|e| e.to_string())?;
                let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Event>();
                let id = handle.block_on(bus.subscribe(pattern, tx));
                ids.push(id.clone());

                // Confirma a assinatura antes de iniciar o forwarding, para que
                // o cliente não receba Delivered no meio do handshake.
                let _ = out_tx.send(OutgoingFrame::Subscribed { id });

                let forward_tx = out_tx.clone();
                thread::spawn(move || {
                    while let Some(event) = rx.blocking_recv() {
                        if forward_tx.send(OutgoingFrame::Delivered { event }).is_err() {
                            break;
                        }
                    }
                });
            }
            {
                let mut c = clients.lock().unwrap();
                if let Some(ctx) = c.get_mut(client_id) {
                    ctx.subscription_ids.extend(ids);
                }
            }
            Ok(())
        }
        IpcFrame::Unsubscribe { id } => {
            {
                let mut c = clients.lock().unwrap();
                if let Some(ctx) = c.get_mut(client_id) {
                    ctx.subscription_ids.retain(|x| x != id);
                }
            }
            handle.block_on(bus.unsubscribe(id));
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
    use crate::events::types::Event;
    use crate::events::{EventBus, EventFabric, EventStore};
    use crate::health::check::HealthChecker;
    use crate::security::SecurityGuard;
    use serde_json::{json, Value};
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixStream;
    use std::sync::Arc;
    use std::time::Duration;

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

    fn send(stream: &mut UnixStream, value: Value) {
        stream
            .write_all(format!("{value}\n").as_bytes())
            .expect("frame enviado");
    }

    fn read(reader: &mut BufReader<UnixStream>) -> Value {
        let mut line = String::new();
        reader.read_line(&mut line).expect("frame lido");
        serde_json::from_str(&line).expect("frame JSON")
    }

    fn event_frame(event_id: &str) -> Value {
        json!({
            "type": "Event",
            "body": {
                "event": {
                    "event_id": event_id,
                    "kind": "bridge.ping",
                    "source": "test",
                    "severity": "info",
                    "payload": {"text": "ação", "n": 1},
                    "correlation_id": "corr-1",
                    "created_at": "2026-11-14T12:00:00Z",
                    "dedup": true
                }
            }
        })
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn publish_subscribe_roundtrip_and_dedup() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test.sock");
        let store = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = Arc::new(EventFabric::new(Arc::clone(&store), Arc::clone(&bus)));
        let server = IpcServer::new(
            socket_path.to_string_lossy().to_string(),
            Arc::clone(&fabric),
            Arc::new(HealthChecker::new("test".to_string())),
            Arc::new(Mutex::new(SecurityGuard::new())),
            tokio::runtime::Handle::current(),
        );
        server.spawn().unwrap();

        let path = socket_path.clone();
        let client = tokio::task::spawn_blocking(move || {
            let mut stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());

            send(
                &mut stream,
                json!({"type": "Subscribe", "body": {"patterns": ["bridge.*"]}}),
            );
            let subscribed = read(&mut reader);
            assert_eq!(subscribed["type"], "Subscribed");
            let subscription_id = subscribed["body"]["id"].as_str().unwrap().to_string();

            send(&mut stream, event_frame("0123456789abcdef0123456789abcdef"));

            let mut ack = None;
            let mut delivered = None;
            for _ in 0..2 {
                let frame = read(&mut reader);
                match frame["type"].as_str().unwrap() {
                    "Ack" => ack = Some(frame),
                    "Delivered" => delivered = Some(frame),
                    other => panic!("frame inesperado: {other}"),
                }
            }
            let ack = ack.expect("Ack recebido");
            let delivered = delivered.expect("Delivered recebido");
            assert_eq!(ack["body"]["ok"], true);
            assert_eq!(
                delivered["body"]["event"]["event_id"],
                "0123456789abcdef0123456789abcdef"
            );
            assert_eq!(delivered["body"]["event"]["payload"]["text"], "ação");

            // Duplicata exata: store deduplica e o barramento não republica.
            send(&mut stream, event_frame("0123456789abcdef0123456789abcdef"));
            let duplicate_ack = read(&mut reader);
            assert_eq!(duplicate_ack["type"], "Ack");
            stream
                .set_read_timeout(Some(Duration::from_millis(300)))
                .unwrap();
            let mut unexpected = String::new();
            let result = reader.read_line(&mut unexpected);
            assert!(
                result.is_err() || unexpected.is_empty(),
                "duplicata não deveria gerar Delivered: {unexpected}"
            );

            send(
                &mut stream,
                json!({"type": "Unsubscribe", "body": {"id": subscription_id}}),
            );
            subscription_id
        })
        .await
        .unwrap();

        assert!(!client.is_empty());
        assert_eq!(store.count().unwrap(), 1);
        assert_eq!(bus.stats().await.published, 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn invalid_pattern_is_rejected_without_panicking() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-invalid.sock");
        let store = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = Arc::new(EventFabric::new(store, bus));
        let server = IpcServer::new(
            socket_path.to_string_lossy().to_string(),
            fabric,
            Arc::new(HealthChecker::new("test".to_string())),
            Arc::new(Mutex::new(SecurityGuard::new())),
            tokio::runtime::Handle::current(),
        );
        server.spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let mut stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            send(
                &mut stream,
                json!({"type": "Subscribe", "body": {"patterns": ["UPPER"]}}),
            );
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"]
                .as_str()
                .unwrap()
                .contains("inválido"));
        })
        .await
        .unwrap();
    }
}
