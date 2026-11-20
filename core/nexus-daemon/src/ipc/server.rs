//! Servidor IPC local sobre Unix Domain Socket.
//!
//! O T4 adiciona ao barramento de eventos um plano de controle mínimo:
//! handshake versionado, autenticação por `SO_PEERCRED`/UID, comandos
//! `Authorize`, `ApprovalRequest`, `ApprovalDecision`, `ApprovalStatus`,
//! `ApprovalRevoke`, `Health` e `Event`. O T5 fecha a paridade de policy e do
//! ciclo de aprovação com o Python. A autoridade final continua no runtime
//! Python até o T6; aqui a decisão é determinística e fail-closed.

use super::auth::{self, PeerCredentials};
use super::protocol::{
    protocol_is_compatible, IpcFrame, OutgoingFrame, IPC_PROTOCOL_VERSION, MAX_FRAME_BYTES,
};
use crate::approval::ApprovalManager;
use crate::control_schema::unix_now;
use crate::events::bus::Pattern;
use crate::events::{EventBus, EventFabric};
use crate::execution::ExecutionGate;
use crate::health::check::HealthChecker;
use crate::identity::store::{IdentityError, NewPairingSession, NewPrincipal};
use crate::identity::IdentityStore;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
use tracing::{error, info, warn};

pub const DEFAULT_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
const DEDUP_WINDOW_SECONDS: i64 = 24 * 60 * 60;

/// Contexto de um cliente autenticado: identidade e inscrições ativas.
pub struct ClientContext {
    pub component_id: String,
    pub subscription_ids: Vec<String>,
}

struct ServerState {
    bus: Arc<EventBus>,
    fabric: Arc<EventFabric>,
    health: Arc<HealthChecker>,
    security: Arc<Mutex<crate::security::SecurityGuard>>,
    gate: ExecutionGate,
    approvals: Arc<ApprovalManager>,
    identity: Arc<IdentityStore>,
    clients: Mutex<HashMap<String, ClientContext>>,
    handle: tokio::runtime::Handle,
}

/// Servidor IPC sobre Unix Domain Socket (threads dedicadas, bloqueantes).
pub struct IpcServer {
    socket_path: String,
    state: Arc<ServerState>,
    expected_uid: u32,
    handshake_timeout: Duration,
}

impl IpcServer {
    pub fn new(
        socket_path: String,
        fabric: Arc<EventFabric>,
        health: Arc<HealthChecker>,
        security: Arc<Mutex<crate::security::SecurityGuard>>,
        approvals: Arc<ApprovalManager>,
        identity: Arc<IdentityStore>,
        handle: tokio::runtime::Handle,
    ) -> Self {
        Self {
            socket_path,
            state: Arc::new(ServerState {
                bus: Arc::clone(fabric.bus()),
                fabric,
                health,
                security,
                gate: ExecutionGate::new(),
                approvals,
                identity,
                clients: Mutex::new(HashMap::new()),
                handle,
            }),
            expected_uid: auth::current_uid(),
            handshake_timeout: DEFAULT_HANDSHAKE_TIMEOUT,
        }
    }

    /// Ajusta o prazo máximo para o primeiro frame (`Hello`).
    pub fn with_handshake_timeout(mut self, timeout: Duration) -> Self {
        self.handshake_timeout = timeout;
        self
    }

    #[cfg(test)]
    pub fn with_expected_uid(mut self, uid: u32) -> Self {
        self.expected_uid = uid;
        self
    }

    /// Faz bind e inicia a thread de accept, retornando imediatamente.
    ///
    /// O bind acontece antes do retorno, então testes e clientes podem
    /// conectar assim que o método conclui. O soquete é restrito ao dono
    /// (`0600`) e cada conexão passa por `SO_PEERCRED` antes do handshake.
    pub fn spawn(self) -> Result<(), std::io::Error> {
        let path = std::path::Path::new(&self.socket_path);
        if path.exists() {
            let _ = std::fs::remove_file(path);
        }
        let listener = UnixListener::bind(path)?;
        auth::restrict_socket_permissions(path).map_err(|error| {
            std::io::Error::new(std::io::ErrorKind::PermissionDenied, error.to_string())
        })?;
        info!(
            component = "ipc",
            socket = %self.socket_path,
            protocol_version = IPC_PROTOCOL_VERSION,
            "servidor IPC escutando"
        );

        let state = Arc::clone(&self.state);
        let expected_uid = self.expected_uid;
        let handshake_timeout = self.handshake_timeout;

        thread::spawn(move || {
            for stream_res in listener.incoming() {
                match stream_res {
                    Ok(stream) => {
                        let state = Arc::clone(&state);
                        thread::spawn(move || {
                            let peer = match auth::peer_credentials(&stream) {
                                Ok(peer) => peer,
                                Err(error) => {
                                    warn!(component = "ipc", error = %error, "peer sem credenciais; conexão encerrada");
                                    return;
                                }
                            };
                            if let Err(error) = auth::authorize_peer(peer.uid, expected_uid) {
                                warn!(
                                    component = "ipc",
                                    uid = peer.uid,
                                    pid = peer.pid,
                                    error = %error,
                                    "peer UID não autorizado; conexão encerrada"
                                );
                                send_auth_error(stream, "peer UID não autorizado");
                                return;
                            }
                            if let Err(error) =
                                handle_connection(stream, state, peer, handshake_timeout)
                            {
                                warn!(component = "ipc", error = %error, "conexão encerrada com erro");
                            }
                        });
                    }
                    Err(error) => {
                        error!(component = "ipc", error = %error, "falha ao aceitar conexão");
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

fn send_auth_error(mut stream: UnixStream, message: &str) {
    let _ = stream.set_write_timeout(Some(WRITE_TIMEOUT));
    let frame = OutgoingFrame::Error {
        error: message.to_string(),
    };
    if let Ok(line) = serde_json::to_string(&frame) {
        let _ = stream.write_all(format!("{line}\n").as_bytes());
    }
}

fn handle_connection(
    stream: UnixStream,
    state: Arc<ServerState>,
    peer: PeerCredentials,
    handshake_timeout: Duration,
) -> Result<(), String> {
    let client_id = uuid::Uuid::new_v4().simple().to_string();
    stream
        .set_write_timeout(Some(WRITE_TIMEOUT))
        .map_err(|error| format!("write timeout: {error}"))?;
    let (out_tx, out_rx) = std::sync::mpsc::channel::<OutgoingFrame>();

    // Thread de escrita: consome o canal e escreve no socket. A limpeza de
    // assinaturas pertence à thread de leitura, para não deixar inscrições
    // órfãs quando o cliente desconecta.
    let write_stream = stream
        .try_clone()
        .map_err(|error| format!("clone do socket: {error}"))?;
    let write_handle = thread::spawn(move || {
        let mut writer = write_stream;
        for frame in out_rx {
            let line = match serde_json::to_string(&frame) {
                Ok(line) => line,
                Err(error) => {
                    warn!(component = "ipc", error = %error, "frame não serializável");
                    continue;
                }
            };
            if writer.write_all(format!("{line}\n").as_bytes()).is_err() {
                break;
            }
        }
    });

    let mut reader = BufReader::new(stream);
    reader
        .get_ref()
        .set_read_timeout(Some(handshake_timeout))
        .map_err(|error| format!("read timeout do handshake: {error}"))?;

    let component_id = match perform_handshake(&mut reader, &state, &peer, &out_tx)? {
        Some(component_id) => component_id,
        None => {
            drop(out_tx);
            let _ = write_handle.join();
            return Ok(());
        }
    };

    reader
        .get_ref()
        .set_read_timeout(None)
        .map_err(|error| format!("remoção do read timeout: {error}"))?;

    {
        let mut clients = state.clients.lock().expect("lock de clients");
        clients.insert(
            client_id.clone(),
            ClientContext {
                component_id: component_id.clone(),
                subscription_ids: Vec::new(),
            },
        );
    }
    info!(
        component = "ipc",
        client_id = %client_id,
        component_id = %component_id,
        uid = peer.uid,
        pid = peer.pid,
        "cliente autenticado"
    );

    loop {
        let line = match read_frame_line(&mut reader) {
            Ok(Some(line)) => line,
            Ok(None) => break,
            Err(error) => {
                let _ = out_tx.send(OutgoingFrame::Error { error });
                break;
            }
        };
        let frame: IpcFrame = match serde_json::from_str(&line) {
            Ok(frame) => frame,
            Err(error) => {
                let _ = out_tx.send(OutgoingFrame::Error {
                    error: format!("JSON inválido: {error}"),
                });
                continue;
            }
        };
        if let Err(error) = handle_frame(&frame, &state, &client_id, &out_tx) {
            let _ = out_tx.send(OutgoingFrame::Error { error });
        }
    }

    let subscription_ids = {
        let mut clients = state.clients.lock().expect("lock de clients");
        clients
            .remove(&client_id)
            .map(|context| context.subscription_ids)
            .unwrap_or_default()
    };
    for id in subscription_ids {
        state.handle.block_on(state.bus.unsubscribe(&id));
    }
    drop(out_tx);
    let _ = write_handle.join();
    Ok(())
}

fn perform_handshake(
    reader: &mut BufReader<UnixStream>,
    state: &ServerState,
    peer: &PeerCredentials,
    out_tx: &std::sync::mpsc::Sender<OutgoingFrame>,
) -> Result<Option<String>, String> {
    let line = match read_frame_line(reader) {
        Ok(Some(line)) => line,
        Ok(None) => return Ok(None),
        Err(error) => {
            let _ = out_tx.send(OutgoingFrame::Error { error });
            return Ok(None);
        }
    };
    let frame: IpcFrame = match serde_json::from_str(&line) {
        Ok(frame) => frame,
        Err(error) => {
            let _ = out_tx.send(OutgoingFrame::Error {
                error: format!("JSON inválido no handshake: {error}"),
            });
            return Ok(None);
        }
    };
    let (protocol_version, component_id) = match frame {
        IpcFrame::Hello {
            protocol_version,
            component_id,
        } => (protocol_version, component_id),
        _ => {
            let _ = out_tx.send(OutgoingFrame::Error {
                error: "handshake obrigatório: o primeiro frame deve ser Hello".to_string(),
            });
            return Ok(None);
        }
    };

    if !protocol_is_compatible(&protocol_version) {
        let _ = out_tx.send(OutgoingFrame::VersionMismatch {
            supported: IPC_PROTOCOL_VERSION.to_string(),
            received: protocol_version,
        });
        return Ok(None);
    }
    if !valid_component_id(&component_id) {
        let _ = out_tx.send(OutgoingFrame::Error {
            error: "component_id inválido".to_string(),
        });
        return Ok(None);
    }
    let trusted = state
        .security
        .lock()
        .expect("lock de security")
        .is_trusted(&component_id);
    let _ = out_tx.send(OutgoingFrame::Welcome {
        protocol_version: IPC_PROTOCOL_VERSION.to_string(),
        server_version: env!("CARGO_PKG_VERSION").to_string(),
        component_id: component_id.clone(),
        peer_uid: peer.uid,
    });
    info!(
        component = "ipc",
        component_id = %component_id,
        trusted,
        protocol_version = %protocol_version,
        "handshake aceito"
    );
    Ok(Some(component_id))
}

fn valid_component_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | ':'))
}

fn read_frame_line(reader: &mut BufReader<UnixStream>) -> Result<Option<String>, String> {
    loop {
        let mut line = String::new();
        let limit = (MAX_FRAME_BYTES + 2) as u64;
        match reader.by_ref().take(limit).read_line(&mut line) {
            Ok(0) => return Ok(None),
            Ok(_) => {
                let content = match line.strip_suffix('\n') {
                    Some(content) => content.strip_suffix('\r').unwrap_or(content),
                    None => return Err("frame sem terminador de linha".to_string()),
                };
                if content.len() > MAX_FRAME_BYTES {
                    return Err("frame excede o limite de 1 MiB".to_string());
                }
                let trimmed = content.trim();
                if trimmed.is_empty() {
                    continue;
                }
                return Ok(Some(trimmed.to_string()));
            }
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                return Err("timeout aguardando frame".to_string())
            }
            Err(error) if error.kind() == ErrorKind::TimedOut => {
                return Err("timeout aguardando frame".to_string())
            }
            Err(error) => return Err(format!("leitura: {error}")),
        }
    }
}

fn handle_frame(
    frame: &IpcFrame,
    state: &ServerState,
    client_id: &str,
    out_tx: &std::sync::mpsc::Sender<OutgoingFrame>,
) -> Result<(), String> {
    match frame {
        IpcFrame::Event { event } => {
            event
                .validate_contract()
                .map_err(|error| format!("evento inválido: {error}"))?;
            state
                .handle
                .block_on(state.fabric.emit(event.clone(), DEDUP_WINDOW_SECONDS))
                .map_err(|error| error.to_string())?;
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
                Pattern::new(pattern).map_err(|error| error.to_string())?;
                let (tx, mut rx) =
                    tokio::sync::mpsc::unbounded_channel::<crate::events::types::Event>();
                let id = state.handle.block_on(state.bus.subscribe(pattern, tx));
                ids.push(id.clone());

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
            let mut clients = state.clients.lock().expect("lock de clients");
            if let Some(context) = clients.get_mut(client_id) {
                context.subscription_ids.extend(ids);
            }
            Ok(())
        }
        IpcFrame::Unsubscribe { id } => {
            {
                let mut clients = state.clients.lock().expect("lock de clients");
                if let Some(context) = clients.get_mut(client_id) {
                    context.subscription_ids.retain(|item| item != id);
                }
            }
            state.handle.block_on(state.bus.unsubscribe(id));
            Ok(())
        }
        IpcFrame::Health => {
            let status = state.health.check(None);
            let _ = out_tx.send(OutgoingFrame::HealthResponse {
                status,
                protocol_version: IPC_PROTOCOL_VERSION.to_string(),
            });
            Ok(())
        }
        IpcFrame::Authorize { request } => {
            let result = state.gate.authorize_request(request, &state.approvals);
            let _ = out_tx.send(OutgoingFrame::AuthorizationResponse { result });
            Ok(())
        }
        IpcFrame::ApprovalRequest {
            request,
            risk_level,
            ttl_s,
        } => {
            let approval = match ttl_s {
                Some(ttl) => state
                    .approvals
                    .request_with_ttl(request, risk_level.as_deref(), *ttl),
                None => state.approvals.request(request, risk_level.as_deref()),
            }
            .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalResponse { approval });
            Ok(())
        }
        IpcFrame::ApprovalDecision { decision } => {
            let approval = state
                .approvals
                .decide(decision)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalResponse { approval });
            Ok(())
        }
        IpcFrame::ApprovalStatus { approval_id } => {
            let approval = state
                .approvals
                .status(approval_id)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalResponse { approval });
            Ok(())
        }
        IpcFrame::ApprovalRevoke { approval_id } => {
            let approval = state
                .approvals
                .revoke(&approval_id)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalResponse { approval });
            Ok(())
        }
        IpcFrame::ApprovalList {
            status,
            limit,
            offset,
        } => {
            let approvals = state
                .approvals
                .list(status.as_deref(), *limit, *offset)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalListResponse { approvals });
            Ok(())
        }
        IpcFrame::ApprovalVerify {
            approval_id,
            request,
        } => {
            let valid = state
                .approvals
                .verify_action(approval_id, request)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalVerifyResponse { valid });
            Ok(())
        }
        IpcFrame::ApprovalClaim {
            approval_id,
            request,
        } => {
            let approval = match request {
                Some(request) => state.approvals.claim(approval_id, request),
                None => state.approvals.claim_by_id(approval_id),
            }
            .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ApprovalResponse { approval });
            Ok(())
        }
        IpcFrame::ApprovalExpire => {
            let expired = state.approvals.expire_stale();
            let _ = out_tx.send(OutgoingFrame::ApprovalExpireResponse { expired });
            Ok(())
        }
        IpcFrame::ExecutionStart { approval_id } => {
            let (approval, execution) = state
                .approvals
                .start_execution(approval_id)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ExecutionResponse {
                approval,
                execution,
            });
            Ok(())
        }
        IpcFrame::ExecutionComplete {
            approval_id,
            status,
            summary,
        } => {
            let (approval, execution) = state
                .approvals
                .complete_execution(approval_id, status, summary.as_deref())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ExecutionResponse {
                approval,
                execution,
            });
            Ok(())
        }
        IpcFrame::ExecutionStatus { approval_id } => {
            let (approval, execution) = state
                .approvals
                .execution_status(approval_id)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::ExecutionResponse {
                approval,
                execution,
            });
            Ok(())
        }
        IpcFrame::ExecutionRecover { timeout_s } => {
            let recovered = state.approvals.recover_orphans(*timeout_s);
            let _ = out_tx.send(OutgoingFrame::ExecutionRecoverResponse { recovered });
            Ok(())
        }
        IpcFrame::IdentityList {
            actor_id,
            kind,
            status,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.principals.read")?;
            let principals = state
                .identity
                .list_principals(kind.as_deref(), status.as_deref())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityPrincipalsResponse { principals });
            Ok(())
        }
        IpcFrame::IdentityGet {
            actor_id,
            principal_id,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.principals.read")?;
            let principal = state
                .identity
                .get_principal(principal_id)
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityPrincipalResponse { principal });
            Ok(())
        }
        IpcFrame::IdentityCreatePrincipal {
            actor_id,
            kind,
            display_name,
            role_id,
            parent_id,
            max_autonomy,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.principals.manage")?;
            let mut new = NewPrincipal::new(
                kind.clone(),
                display_name.clone(),
                role_id.clone(),
                *max_autonomy,
            );
            if let Some(parent_id) = parent_id {
                new = new.with_parent(parent_id.clone());
            }
            let principal = state
                .identity
                .create_principal(&new, unix_now())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityPrincipalResponse { principal });
            Ok(())
        }
        IpcFrame::IdentityRevokePrincipal {
            actor_id,
            principal_id,
            reason,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.principals.manage")?;
            let summary = state
                .identity
                .revoke_principal(principal_id, actor_id, reason, unix_now())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityRevocationResponse { summary });
            Ok(())
        }
        IpcFrame::IdentityEffectiveScopes { principal_id } => {
            let scopes = state
                .identity
                .effective_scopes(principal_id, unix_now())
                .map_err(|error| error.to_string())?
                .into_iter()
                .collect();
            let _ = out_tx.send(OutgoingFrame::IdentityScopesResponse {
                principal_id: principal_id.clone(),
                scopes,
            });
            Ok(())
        }
        IpcFrame::IdentityPermissionCheck {
            principal_id,
            scope,
        } => {
            let (allowed, reason) = permission_allowed(&state.identity, principal_id, scope)?;
            let _ = out_tx.send(OutgoingFrame::IdentityPermissionResponse {
                principal_id: principal_id.clone(),
                scope: scope.clone(),
                allowed,
                reason,
            });
            Ok(())
        }
        IpcFrame::IdentityDeviceList {
            actor_id,
            principal_id,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.devices.read")?;
            let devices = state
                .identity
                .list_devices(principal_id.as_deref())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityDevicesResponse { devices });
            Ok(())
        }
        IpcFrame::IdentityDeviceRevoke {
            actor_id,
            device_id,
            reason,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.devices.manage")?;
            let summary = state
                .identity
                .revoke_device(device_id, actor_id, reason, unix_now())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityRevocationResponse { summary });
            Ok(())
        }
        IpcFrame::IdentityPairStart {
            actor_id,
            role_id,
            constraints,
            ttl_s,
            secret_hash,
            code_hash,
        } => {
            require_identity_scope(&state.identity, actor_id, "identity.pairing.create")?;
            let new = NewPairingSession {
                role_id: role_id.clone(),
                created_by: actor_id.clone(),
                secret_hash: secret_hash.clone(),
                code_hash: code_hash.clone(),
                constraints: normalize_pairing_constraints(constraints)?,
                ttl_s: ttl_s.unwrap_or(300.0),
            };
            let session = state
                .identity
                .create_pairing_session(&new, unix_now())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityPairingResponse { session });
            Ok(())
        }
        IpcFrame::IdentityInitOwner {
            display_name,
            token_hash,
        } => {
            let bootstrap = state
                .identity
                .init_owner(
                    display_name.as_deref().unwrap_or("Owner"),
                    token_hash,
                    unix_now(),
                )
                .map_err(|error| error.to_string())?;
            if bootstrap.created_principal {
                emit_identity_audit(
                    state,
                    "identity.principal.created",
                    serde_json::json!({
                        "principal_id": bootstrap.principal.id,
                        "kind": bootstrap.principal.kind,
                        "role": bootstrap.principal.role_id,
                        "actor_id": "local_bootstrap",
                    }),
                );
            }
            if bootstrap.created_credential {
                emit_identity_audit(
                    state,
                    "identity.credential.issued",
                    serde_json::json!({
                        "credential_id": bootstrap.credential_id,
                        "kind": "local_owner",
                        "expires_at": serde_json::Value::Null,
                    }),
                );
            }
            let _ = out_tx.send(OutgoingFrame::IdentityOwnerResponse {
                initialized: true,
                principal: Some(bootstrap.principal),
                credential_id: Some(bootstrap.credential_id),
                created_principal: bootstrap.created_principal,
                created_credential: bootstrap.created_credential,
            });
            Ok(())
        }
        IpcFrame::IdentityOwnerStatus { token_hash } => {
            let owner = state
                .identity
                .local_owner_status(token_hash, unix_now())
                .map_err(|error| error.to_string())?;
            let _ = out_tx.send(OutgoingFrame::IdentityOwnerResponse {
                initialized: owner.is_some(),
                principal: owner.as_ref().map(|owner| owner.principal.clone()),
                credential_id: owner.map(|owner| owner.credential_id),
                created_principal: false,
                created_credential: false,
            });
            Ok(())
        }
        IpcFrame::Hello { .. } => Err("Hello só é válido no handshake".to_string()),
    }
}

fn emit_identity_audit(state: &ServerState, kind: &str, payload: serde_json::Value) {
    let event = crate::events::types::Event::new(kind, "nexus-daemon", payload).with_dedup(false);
    if let Err(error) = state.handle.block_on(state.fabric.emit(event, 0)) {
        warn!("falha ao emitir evento de auditoria {kind}: {error}");
    }
}

fn require_identity_scope(
    identity: &IdentityStore,
    actor_id: &str,
    scope: &str,
) -> Result<(), String> {
    if actor_id.trim().is_empty() {
        return Err("actor_id obrigatório para comando de identidade".to_string());
    }
    let scopes = identity
        .effective_scopes(actor_id, unix_now())
        .map_err(|error| format!("identidade: {error}"))?;
    if scopes.contains(scope) {
        Ok(())
    } else {
        Err(format!("ator {actor_id} não possui o escopo {scope}"))
    }
}

fn permission_allowed(
    identity: &IdentityStore,
    principal_id: &str,
    scope: &str,
) -> Result<(bool, Option<String>), String> {
    if identity.catalog().scope(scope).is_err() {
        return Ok((false, Some(format!("escopo desconhecido: {scope}"))));
    }
    match identity.effective_scopes(principal_id, unix_now()) {
        Ok(scopes) if scopes.contains(scope) => Ok((true, None)),
        Ok(_) => Ok((false, Some("escopo ausente".to_string()))),
        Err(IdentityError::PrincipalNotFound(_)) => {
            Ok((false, Some("principal não encontrado".to_string())))
        }
        Err(IdentityError::PrincipalNotActive(_)) => {
            Ok((false, Some("principal não está ativo".to_string())))
        }
        Err(error @ IdentityError::Invalid(_))
        | Err(error @ IdentityError::PrivilegeEscalation(_)) => {
            Ok((false, Some(error.to_string())))
        }
        Err(error) => Err(error.to_string()),
    }
}

fn normalize_pairing_constraints(
    constraints: &Option<serde_json::Value>,
) -> Result<String, String> {
    match constraints {
        None | Some(serde_json::Value::Null) => Ok("{}".to_string()),
        Some(value @ serde_json::Value::Object(_)) => Ok(value.to_string()),
        Some(_) => Err("constraints deve ser um objeto JSON".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contracts::{ApprovalDecision, ApprovalRequest, AuthorizationRequest};
    use crate::events::{EventBus, EventFabric, EventStore};
    use crate::health::check::HealthChecker;
    use crate::security::SecurityGuard;
    use serde_json::{json, Value};
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixStream;
    use std::sync::Arc;
    use std::time::Duration;

    fn build_server(socket_path: &std::path::Path) -> IpcServer {
        let store = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = Arc::new(EventFabric::new(Arc::clone(&store), Arc::clone(&bus)));
        IpcServer::new(
            socket_path.to_string_lossy().to_string(),
            fabric,
            Arc::new(HealthChecker::new("test".to_string())),
            Arc::new(Mutex::new(SecurityGuard::new())),
            Arc::new(ApprovalManager::new()),
            Arc::new(IdentityStore::open_in_memory().unwrap()),
            tokio::runtime::Handle::current(),
        )
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

    fn hello_frame(version: &str, component_id: &str) -> Value {
        json!({
            "type": "Hello",
            "body": {
                "protocol_version": version,
                "component_id": component_id,
            }
        })
    }

    fn connect(path: &std::path::Path) -> (UnixStream, BufReader<UnixStream>) {
        let mut stream = UnixStream::connect(path).expect("conecta");
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        send(
            &mut stream,
            hello_frame(IPC_PROTOCOL_VERSION, "python-runtime"),
        );
        let welcome = read(&mut reader);
        assert_eq!(welcome["type"], "Welcome");
        assert_eq!(welcome["body"]["protocol_version"], IPC_PROTOCOL_VERSION);
        (stream, reader)
    }

    fn event_frame(event_id: &str) -> Value {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("relógio")
            .as_secs();
        let created_at = format!("{}Z", crate::events::types::chrono_naive_utc(now, 0));
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
                    "created_at": created_at,
                    "dedup": true
                }
            }
        })
    }

    fn authorization_request(risk_level: &str) -> Value {
        serde_json::to_value(AuthorizationRequest {
            component_id: "python-runtime".to_string(),
            tool_id: "sys.dangerous".to_string(),
            params: json!({"args": []}),
            task_id: "task-1".to_string(),
            plan_id: "plan-1".to_string(),
            risk_level: risk_level.to_string(),
            autonomy_level: 1,
            required_autonomy: 1,
            approval_id: None,
            execution_mode: "HOST".to_string(),
            access_level: "WRITE".to_string(),
            policy_version: "1.0".to_string(),
            required_capability: None,
            fs_scope: "DEFAULT".to_string(),
            net_scope: "NONE".to_string(),
            resource_limits: "default".to_string(),
            justification: "test".to_string(),
        })
        .unwrap()
    }

    fn authorize(
        stream: &mut UnixStream,
        reader: &mut BufReader<UnixStream>,
        request: Value,
    ) -> Value {
        send(
            stream,
            json!({"type": "Authorize", "body": {"request": request}}),
        );
        let frame = read(reader);
        assert_eq!(frame["type"], "AuthorizationResponse");
        frame["body"]["result"].clone()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn handshake_is_mandatory_and_versioned() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-handshake.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let mut stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            send(&mut stream, json!({"type": "Health"}));
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"].as_str().unwrap().contains("Hello"));
        })
        .await
        .unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let mut stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            send(&mut stream, hello_frame("2.0.0", "python-runtime"));
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "VersionMismatch");
            assert_eq!(frame["body"]["supported"], IPC_PROTOCOL_VERSION);
            assert_eq!(frame["body"]["received"], "2.0.0");
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn handshake_timeout_closes_idle_connection() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-timeout.sock");
        build_server(&socket_path)
            .with_handshake_timeout(Duration::from_millis(100))
            .spawn()
            .unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .expect("frame de erro");
            let frame: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"].as_str().unwrap().contains("timeout"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn oversized_and_unterminated_frames_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-frame-limit.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            stream
                .set_write_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut writer = stream.try_clone().unwrap();
            let oversized = format!("{}\n", "x".repeat(MAX_FRAME_BYTES + 1));
            let handle = thread::spawn(move || {
                let _ = writer.write_all(oversized.as_bytes());
            });
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"].as_str().unwrap().contains("1 MiB"));
            let _ = handle.join();
        })
        .await
        .unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            stream.write_all(b"{\"type\":\"Health\"}").unwrap();
            stream
                .shutdown(std::net::Shutdown::Write)
                .expect("half-close");
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"]
                .as_str()
                .unwrap()
                .contains("terminador"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn peer_uid_mismatch_is_rejected_before_handshake() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-uid.sock");
        let current = auth::current_uid();
        let unexpected = if current == u32::MAX {
            current - 1
        } else {
            current + 1
        };
        build_server(&socket_path)
            .with_expected_uid(unexpected)
            .spawn()
            .unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let stream = UnixStream::connect(&path).expect("conecta");
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .expect("frame de erro");
            let frame: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"].as_str().unwrap().contains("UID"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn identity_frames_require_scope_and_check_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-identity.sock");
        let store = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = Arc::new(EventFabric::new(Arc::clone(&store), Arc::clone(&bus)));
        let identity = Arc::new(IdentityStore::open_in_memory().unwrap());
        let owner = identity
            .create_principal(&NewPrincipal::new("user", "Owner", "owner", 4), unix_now())
            .unwrap();
        let family = identity
            .create_principal(&NewPrincipal::new("user", "Wife", "family", 1), unix_now())
            .unwrap();
        let server = IpcServer::new(
            socket_path.to_string_lossy().to_string(),
            fabric,
            Arc::new(HealthChecker::new("test".to_string())),
            Arc::new(Mutex::new(SecurityGuard::new())),
            Arc::new(ApprovalManager::new()),
            Arc::clone(&identity),
            tokio::runtime::Handle::current(),
        );
        server.spawn().unwrap();

        let path = socket_path.clone();
        let owner_id = owner.id.clone();
        let family_id = family.id.clone();
        let check = Arc::clone(&identity);
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);

            send(
                &mut stream,
                json!({
                    "type": "IdentityList",
                    "body": {"actor_id": family_id},
                }),
            );
            let denied = read(&mut reader);
            assert_eq!(denied["type"], "Error");
            assert!(denied["body"]["error"]
                .as_str()
                .unwrap()
                .contains("identity.principals.read"));

            send(
                &mut stream,
                json!({
                    "type": "IdentityList",
                    "body": {"actor_id": owner_id},
                }),
            );
            let listed = read(&mut reader);
            assert_eq!(listed["type"], "IdentityPrincipalsResponse");
            assert_eq!(listed["body"]["principals"].as_array().unwrap().len(), 2);

            send(
                &mut stream,
                json!({
                    "type": "IdentityCreatePrincipal",
                    "body": {
                        "actor_id": owner_id,
                        "kind": "user",
                        "display_name": "Guest",
                        "role_id": "guest",
                        "max_autonomy": 0,
                    }
                }),
            );
            let created = read(&mut reader);
            assert_eq!(created["type"], "IdentityPrincipalResponse");
            let guest_id = created["body"]["principal"]["id"]
                .as_str()
                .unwrap()
                .to_string();
            assert_eq!(created["body"]["principal"]["role_id"], "guest");

            send(
                &mut stream,
                json!({
                    "type": "IdentityPermissionCheck",
                    "body": {"principal_id": guest_id, "scope": "tools.write"},
                }),
            );
            let denied_write = read(&mut reader);
            assert_eq!(denied_write["type"], "IdentityPermissionResponse");
            assert_eq!(denied_write["body"]["allowed"], false);
            assert_eq!(denied_write["body"]["reason"], "escopo ausente");

            send(
                &mut stream,
                json!({
                    "type": "IdentityPermissionCheck",
                    "body": {"principal_id": guest_id, "scope": "tools.read"},
                }),
            );
            let allowed_read = read(&mut reader);
            assert_eq!(allowed_read["body"]["allowed"], true);

            send(
                &mut stream,
                json!({
                    "type": "IdentityPermissionCheck",
                    "body": {"principal_id": guest_id, "scope": "tools.nuke"},
                }),
            );
            let unknown = read(&mut reader);
            assert_eq!(unknown["body"]["allowed"], false);
            assert!(unknown["body"]["reason"]
                .as_str()
                .unwrap()
                .contains("desconhecido"));

            send(
                &mut stream,
                json!({
                    "type": "IdentityPairStart",
                    "body": {
                        "actor_id": owner_id,
                        "role_id": "guest",
                        "constraints": {"max_autonomy": 0},
                        "ttl_s": 60.0,
                        "secret_hash": "a".repeat(64),
                    }
                }),
            );
            let pairing = read(&mut reader);
            assert_eq!(pairing["type"], "IdentityPairingResponse");
            assert_eq!(pairing["body"]["session"]["status"], "pending");
            assert!(pairing["body"]["session"].get("secret_hash").is_none());

            send(
                &mut stream,
                json!({
                    "type": "IdentityRevokePrincipal",
                    "body": {
                        "actor_id": owner_id,
                        "principal_id": guest_id,
                        "reason": "teste de cascata",
                    }
                }),
            );
            let revoked = read(&mut reader);
            assert_eq!(revoked["type"], "IdentityRevocationResponse");
            assert_eq!(revoked["body"]["summary"]["principals"], 1);
        })
        .await
        .unwrap();

        let guest_id = check
            .list_principals(Some("user"), None)
            .unwrap()
            .into_iter()
            .find(|principal| principal.display_name == "Guest")
            .unwrap()
            .id;
        assert_eq!(check.get_principal(&guest_id).unwrap().status, "revoked");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn identity_init_owner_is_idempotent_and_audited() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-init-owner.sock");
        let events = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = Arc::new(EventFabric::new(Arc::clone(&events), Arc::clone(&bus)));
        let identity = Arc::new(IdentityStore::open_in_memory().unwrap());
        let server = IpcServer::new(
            socket_path.to_string_lossy().to_string(),
            fabric,
            Arc::new(HealthChecker::new("test".to_string())),
            Arc::new(Mutex::new(SecurityGuard::new())),
            Arc::new(ApprovalManager::new()),
            Arc::clone(&identity),
            tokio::runtime::Handle::current(),
        );
        server.spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let token_hash = "a".repeat(64);

            send(
                &mut stream,
                json!({
                    "type": "IdentityOwnerStatus",
                    "body": {"token_hash": token_hash},
                }),
            );
            let absent = read(&mut reader);
            assert_eq!(absent["type"], "IdentityOwnerResponse");
            assert_eq!(absent["body"]["initialized"], false);
            assert!(absent["body"]["principal"].is_null());

            send(
                &mut stream,
                json!({
                    "type": "IdentityInitOwner",
                    "body": {"display_name": "Owner", "token_hash": token_hash},
                }),
            );
            let first = read(&mut reader);
            assert_eq!(first["type"], "IdentityOwnerResponse");
            assert_eq!(first["body"]["created_principal"], true);
            assert_eq!(first["body"]["created_credential"], true);
            assert_eq!(first["body"]["principal"]["role_id"], "owner");
            let principal_id = first["body"]["principal"]["id"]
                .as_str()
                .unwrap()
                .to_string();

            send(
                &mut stream,
                json!({
                    "type": "IdentityInitOwner",
                    "body": {"display_name": "Ignorado", "token_hash": token_hash},
                }),
            );
            let second = read(&mut reader);
            assert_eq!(second["body"]["created_principal"], false);
            assert_eq!(second["body"]["created_credential"], false);
            assert_eq!(second["body"]["principal"]["id"], principal_id);

            send(
                &mut stream,
                json!({
                    "type": "IdentityOwnerStatus",
                    "body": {"token_hash": token_hash},
                }),
            );
            let present = read(&mut reader);
            assert_eq!(present["body"]["initialized"], true);
            assert_eq!(present["body"]["principal"]["id"], principal_id);
            assert!(present["body"]["credential_id"].is_string());
        })
        .await
        .unwrap();

        assert_eq!(identity.count_principals().unwrap(), 1);
        let created = events
            .query_by_kind("identity.principal.created", 10)
            .unwrap();
        assert_eq!(created.len(), 1);
        assert_eq!(created[0].payload["actor_id"], "local_bootstrap");
        assert_eq!(created[0].payload["role"], "owner");
        let issued = events
            .query_by_kind("identity.credential.issued", 10)
            .unwrap();
        assert_eq!(issued.len(), 1);
        assert_eq!(issued[0].payload["kind"], "local_owner");
        assert_eq!(events.count().unwrap(), 2);

        let owner = identity
            .local_owner_status(&"a".repeat(64), unix_now())
            .unwrap()
            .expect("owner local");
        assert_eq!(owner.principal.role_id, "owner");
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
            Arc::new(ApprovalManager::new()),
            Arc::new(IdentityStore::open_in_memory().unwrap()),
            tokio::runtime::Handle::current(),
        );
        server.spawn().unwrap();

        let path = socket_path.clone();
        let client = tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);

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
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
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

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn health_reports_protocol_version() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-health.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            send(&mut stream, json!({"type": "Health"}));
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "HealthResponse");
            assert_eq!(frame["body"]["status"]["status"], "ok");
            assert_eq!(frame["body"]["protocol_version"], IPC_PROTOCOL_VERSION);
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn authorize_low_risk_is_allowed() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-authorize.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let mut request = authorization_request("LOW");
            request["access_level"] = json!("READ");
            let result = authorize(&mut stream, &mut reader, request);
            assert_eq!(result["decision"], "ALLOWED");
            assert_eq!(result["approved"], true);
            assert_eq!(result["execution_id"], result["request_id"]);
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn authorize_high_risk_approval_flow_and_replay_rejection() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-approval.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let request = authorization_request("HIGH");
            let pending = authorize(&mut stream, &mut reader, request.clone());
            assert_eq!(pending["decision"], "REQUIRES_APPROVAL");
            assert_eq!(pending["approved"], false);
            assert!(pending["execution_id"].is_null());
            let approval_id = pending["metadata"]["approval_id"]
                .as_str()
                .expect("approval_id")
                .to_string();

            send(
                &mut stream,
                json!({
                    "type": "ApprovalStatus",
                    "body": {"approval_id": approval_id}
                }),
            );
            let status = read(&mut reader);
            assert_eq!(status["type"], "ApprovalResponse");
            assert_eq!(status["body"]["approval"]["status"], "PENDING");

            let timestamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64();
            send(
                &mut stream,
                json!({
                    "type": "ApprovalDecision",
                    "body": {
                        "decision": {
                            "approval_id": approval_id,
                            "decision": "APPROVED",
                            "approver": "operator",
                            "timestamp": timestamp
                        }
                    }
                }),
            );
            let decided = read(&mut reader);
            assert_eq!(decided["type"], "ApprovalResponse");
            assert_eq!(decided["body"]["approval"]["status"], "APPROVED");

            let mut approved_request = request;
            approved_request["approval_id"] = json!(approval_id);
            let allowed = authorize(&mut stream, &mut reader, approved_request.clone());
            assert_eq!(allowed["decision"], "ALLOWED");
            assert_eq!(allowed["metadata"]["approval_status"], "CLAIMED");

            let replay = authorize(&mut stream, &mut reader, approved_request);
            assert_eq!(replay["decision"], "DENIED");
            assert!(replay["reason"]
                .as_str()
                .unwrap()
                .contains("approval verification failed"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn approval_revoke_blocks_decision_and_claim() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-approval-revoke.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let request = authorization_request("HIGH");
            let pending = authorize(&mut stream, &mut reader, request.clone());
            let approval_id = pending["metadata"]["approval_id"]
                .as_str()
                .expect("approval_id")
                .to_string();

            send(
                &mut stream,
                json!({
                    "type": "ApprovalRevoke",
                    "body": {"approval_id": approval_id}
                }),
            );
            let revoked = read(&mut reader);
            assert_eq!(revoked["type"], "ApprovalResponse");
            assert_eq!(revoked["body"]["approval"]["status"], "REVOKED");

            let timestamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64();
            send(
                &mut stream,
                json!({
                    "type": "ApprovalDecision",
                    "body": {
                        "decision": {
                            "approval_id": approval_id,
                            "decision": "APPROVED",
                            "approver": "operator",
                            "timestamp": timestamp
                        }
                    }
                }),
            );
            let rejected = read(&mut reader);
            assert_eq!(rejected["type"], "Error");

            send(
                &mut stream,
                json!({
                    "type": "ApprovalStatus",
                    "body": {"approval_id": approval_id}
                }),
            );
            let status = read(&mut reader);
            assert_eq!(status["body"]["approval"]["status"], "REVOKED");

            let mut revoked_request = request;
            revoked_request["approval_id"] = json!(approval_id);
            let denied = authorize(&mut stream, &mut reader, revoked_request);
            assert_eq!(denied["decision"], "DENIED");
            assert!(denied["reason"]
                .as_str()
                .unwrap()
                .contains("approval verification failed"));

            send(
                &mut stream,
                json!({
                    "type": "ApprovalRevoke",
                    "body": {"approval_id": "missing"}
                }),
            );
            let missing = read(&mut reader);
            assert_eq!(missing["type"], "Error");
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn approval_request_command_rejects_non_ijson_params() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-approval-contract.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let request = ApprovalRequest {
                tool_id: "sys.dangerous".to_string(),
                params: json!({"n": 9_007_199_254_740_992u64}),
                task_id: "task-1".to_string(),
                plan_id: "plan-1".to_string(),
                execution_mode: "HOST".to_string(),
                access_level: "WRITE".to_string(),
                fs_scope: "DEFAULT".to_string(),
                net_scope: "NONE".to_string(),
                resource_limits: "default".to_string(),
                policy_version: "1.0".to_string(),
                justification: "test".to_string(),
            };
            send(
                &mut stream,
                json!({"type": "ApprovalRequest", "body": {"request": request}}),
            );
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"].as_str().unwrap().contains("I-JSON"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn approval_decision_contract_is_validated() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-approval-decision.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let decision = ApprovalDecision {
                approval_id: "missing".to_string(),
                decision: "MAYBE".to_string(),
                approver: "operator".to_string(),
                timestamp: 1.0,
            };
            send(
                &mut stream,
                json!({"type": "ApprovalDecision", "body": {"decision": decision}}),
            );
            let frame = read(&mut reader);
            assert_eq!(frame["type"], "Error");
            assert!(frame["body"]["error"]
                .as_str()
                .unwrap()
                .contains("decision"));
        })
        .await
        .unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn execution_cycle_claims_and_consumes_persisted_approval() {
        let dir = tempfile::tempdir().unwrap();
        let socket_path = dir.path().join("nexus-test-execution.sock");
        build_server(&socket_path).spawn().unwrap();

        let path = socket_path.clone();
        tokio::task::spawn_blocking(move || {
            let (mut stream, mut reader) = connect(&path);
            let approval_request = json!({
                "tool_id": "sys.dangerous",
                "params": {"args": []},
                "task_id": "task-1",
                "plan_id": "plan-1",
                "execution_mode": "HOST",
                "access_level": "WRITE",
                "fs_scope": "DEFAULT",
                "net_scope": "NONE",
                "resource_limits": "default",
                "policy_version": "1.0",
                "justification": "test"
            });
            send(
                &mut stream,
                json!({
                    "type": "ApprovalRequest",
                    "body": {
                        "request": approval_request.clone(),
                        "risk_level": "HIGH",
                        "ttl_s": 60.0
                    }
                }),
            );
            let created = read(&mut reader);
            assert_eq!(created["type"], "ApprovalResponse");
            assert_eq!(created["body"]["approval"]["status"], "PENDING");
            assert_eq!(created["body"]["approval"]["risk_level"], "HIGH");
            let approval_id = created["body"]["approval"]["approval_id"]
                .as_str()
                .unwrap()
                .to_string();

            send(
                &mut stream,
                json!({
                    "type": "ApprovalVerify",
                    "body": {
                        "approval_id": approval_id,
                        "request": approval_request
                    }
                }),
            );
            let verified = read(&mut reader);
            assert_eq!(verified["type"], "ApprovalVerifyResponse");
            assert_eq!(verified["body"]["valid"], true);

            send(
                &mut stream,
                json!({
                    "type": "ApprovalList",
                    "body": {"status": "PENDING", "limit": 10, "offset": 0}
                }),
            );
            let listed = read(&mut reader);
            assert_eq!(listed["type"], "ApprovalListResponse");
            assert_eq!(listed["body"]["approvals"].as_array().unwrap().len(), 1);

            let timestamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64();
            send(
                &mut stream,
                json!({
                    "type": "ApprovalDecision",
                    "body": {
                        "decision": {
                            "approval_id": approval_id,
                            "decision": "APPROVED",
                            "approver": "operator",
                            "timestamp": timestamp
                        }
                    }
                }),
            );
            let decided = read(&mut reader);
            assert_eq!(decided["body"]["approval"]["status"], "APPROVED");

            let mut request = authorization_request("HIGH");
            request["approval_id"] = json!(approval_id);
            let allowed = authorize(&mut stream, &mut reader, request);
            assert_eq!(allowed["decision"], "ALLOWED");
            assert_eq!(allowed["metadata"]["approval_status"], "CLAIMED");

            send(
                &mut stream,
                json!({"type": "ExecutionStart", "body": {"approval_id": approval_id}}),
            );
            let started = read(&mut reader);
            assert_eq!(started["type"], "ExecutionResponse");
            assert_eq!(started["body"]["approval"]["status"], "CONSUMED");
            assert!(started["body"]["approval"]["consumed_at"].is_number());
            assert_eq!(started["body"]["execution"]["status"], "STARTING");

            send(
                &mut stream,
                json!({
                    "type": "ExecutionComplete",
                    "body": {
                        "approval_id": approval_id,
                        "status": "COMPLETED",
                        "summary": "ok"
                    }
                }),
            );
            let completed = read(&mut reader);
            assert_eq!(completed["body"]["execution"]["status"], "COMPLETED");
            assert_eq!(completed["body"]["execution"]["result_summary"], "ok");

            send(
                &mut stream,
                json!({
                    "type": "ApprovalList",
                    "body": {"status": "CONSUMED", "limit": 10, "offset": 0}
                }),
            );
            let history = read(&mut reader);
            let items = history["body"]["approvals"].as_array().unwrap();
            assert_eq!(items.len(), 1);
            assert_eq!(items[0]["approval_id"], approval_id);

            send(
                &mut stream,
                json!({
                    "type": "ExecutionStatus",
                    "body": {"approval_id": approval_id}
                }),
            );
            let status = read(&mut reader);
            assert_eq!(status["type"], "ExecutionResponse");
            assert_eq!(status["body"]["execution"]["status"], "COMPLETED");
        })
        .await
        .unwrap();
    }

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
