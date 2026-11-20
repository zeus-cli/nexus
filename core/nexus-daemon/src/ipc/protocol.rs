use serde::{Deserialize, Serialize};

use crate::approval_store::ExecutionRecord;
use crate::contracts::{
    ApprovalDecision, ApprovalRecord, ApprovalRequest, AuthorizationRequest, AuthorizationResult,
};
use crate::events::types::Event;
use crate::health::check::HealthStatus;
use crate::identity::store::{Device, PairingSession, Principal, RevocationSummary};

/// Versão do protocolo IPC de controle.
///
/// O contrato de evento continua em `contracts/` v1.0.0. O handshake negocia
/// apenas a compatibilidade do protocolo do soquete; um cliente de outra major
/// é rejeitado antes de qualquer comando.
pub const IPC_PROTOCOL_VERSION: &str = "1.0.0";
pub const IPC_PROTOCOL_MAJOR: u32 = 1;

/// Limite de frame aceito por linha (1 MiB, igual ao cliente Python).
pub const MAX_FRAME_BYTES: usize = 1_048_576;

/// Extrai a major de uma versão `MAJOR[.MINOR[.PATCH]]`.
pub fn protocol_major(version: &str) -> Option<u32> {
    let major = version.split('.').next()?;
    if major.is_empty() || !major.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    major.parse::<u32>().ok()
}

pub fn protocol_is_compatible(version: &str) -> bool {
    protocol_major(version) == Some(IPC_PROTOCOL_MAJOR)
}

/// Frame enviado pelo cliente ao daemon (nova linha JSON).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "body")]
pub enum IpcFrame {
    /// Primeiro frame obrigatório: negocia versão e identifica o componente.
    Hello {
        protocol_version: String,
        component_id: String,
    },
    /// Cliente publica um evento no barramento.
    Event { event: Event },
    /// Cliente se inscreve em padrões de evento.
    Subscribe { patterns: Vec<String> },
    /// Cliente cancela uma inscrição pelo id.
    Unsubscribe { id: String },
    /// Cliente solicita health check.
    Health,
    /// Cliente pede decisão de autorização ao Execution Gate.
    Authorize { request: AuthorizationRequest },
    /// Cliente cria uma solicitação de aprovação HITL.
    ApprovalRequest {
        request: ApprovalRequest,
        #[serde(default)]
        risk_level: Option<String>,
        #[serde(default)]
        ttl_s: Option<f64>,
    },
    /// Cliente registra uma decisão humana.
    ApprovalDecision { decision: ApprovalDecision },
    /// Cliente consulta o estado de uma aprovação.
    ApprovalStatus { approval_id: String },
    /// Cliente revoga uma aprovação `PENDING` ou `APPROVED`.
    ApprovalRevoke { approval_id: String },
    /// Cliente lista aprovações com filtro e paginação.
    ApprovalList {
        #[serde(default)]
        status: Option<String>,
        #[serde(default)]
        limit: u32,
        #[serde(default)]
        offset: u32,
    },
    /// Cliente verifica o vínculo por hash sem consumir a aprovação.
    ApprovalVerify {
        approval_id: String,
        request: ApprovalRequest,
    },
    /// Cliente reivindica uma aprovação (`APPROVED → CLAIMED`).
    ///
    /// Quando `request` está presente, o hash é verificado na mesma transação;
    /// sem ele, o chamador já validou o vínculo e a operação é local ao UID.
    ApprovalClaim {
        approval_id: String,
        #[serde(default)]
        request: Option<ApprovalRequest>,
    },
    /// Cliente expira solicitações vencidas e devolve a contagem.
    ApprovalExpire,
    /// Cliente inicia a execução: `CLAIMED → CONSUMED` + `STARTING`.
    ExecutionStart { approval_id: String },
    /// Cliente conclui a execução (`COMPLETED`, `FAILED` ou `INTERRUPTED`).
    ExecutionComplete {
        approval_id: String,
        status: String,
        #[serde(default)]
        summary: Option<String>,
    },
    /// Cliente consulta a aprovação e a execução vinculada.
    ExecutionStatus { approval_id: String },
    /// Cliente marca execuções órfãs como `UNKNOWN`.
    ExecutionRecover { timeout_s: f64 },

    // -- identidade (T8.3) -------------------------------------------------
    /// Lista principals. Exige `identity.principals.read` do ator.
    IdentityList {
        actor_id: String,
        #[serde(default)]
        kind: Option<String>,
        #[serde(default)]
        status: Option<String>,
    },
    /// Consulta um principal. Exige `identity.principals.read` do ator.
    IdentityGet {
        actor_id: String,
        principal_id: String,
    },
    /// Cria um principal. Exige `identity.principals.manage` do ator.
    IdentityCreatePrincipal {
        actor_id: String,
        kind: String,
        display_name: String,
        role_id: String,
        #[serde(default)]
        parent_id: Option<String>,
        #[serde(default)]
        max_autonomy: u8,
    },
    /// Revoga um principal e sua cascata. Exige `identity.principals.manage`.
    IdentityRevokePrincipal {
        actor_id: String,
        principal_id: String,
        reason: String,
    },
    /// Calcula os escopos efetivos de um principal (consulta do gate).
    IdentityEffectiveScopes { principal_id: String },
    /// Verifica fail-closed se um principal possui um escopo.
    IdentityPermissionCheck { principal_id: String, scope: String },
    /// Lista devices. Exige `identity.devices.read` do ator.
    IdentityDeviceList {
        actor_id: String,
        #[serde(default)]
        principal_id: Option<String>,
    },
    /// Revoga um device e sua cascata. Exige `identity.devices.manage`.
    IdentityDeviceRevoke {
        actor_id: String,
        device_id: String,
        reason: String,
    },
    /// Cria uma sessão de pairing. O cliente envia apenas hashes; o segredo
    /// bruto nunca cruza o IPC. Exige `identity.pairing.create` do ator.
    IdentityPairStart {
        actor_id: String,
        role_id: String,
        #[serde(default)]
        constraints: Option<serde_json::Value>,
        #[serde(default)]
        ttl_s: Option<f64>,
        secret_hash: String,
        #[serde(default)]
        code_hash: Option<String>,
    },
    /// Bootstrap idempotente do owner local (T8.4). Mesmo UID; o cliente
    /// envia apenas o SHA-256 do token local.
    IdentityInitOwner {
        #[serde(default)]
        display_name: Option<String>,
        token_hash: String,
    },
    /// Resolve o owner local pelo hash do token, sem criar nada.
    IdentityOwnerStatus { token_hash: String },
}

/// Mensagem de saída direcionada a um cliente específico (envio pelo socket).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", content = "body")]
pub enum OutgoingFrame {
    Welcome {
        protocol_version: String,
        server_version: String,
        component_id: String,
        peer_uid: u32,
    },
    VersionMismatch {
        supported: String,
        received: String,
    },
    Ack {
        ok: bool,
        event_id: Option<String>,
        error: Option<String>,
    },
    Subscribed {
        id: String,
    },
    Delivered {
        event: Event,
    },
    HealthResponse {
        status: HealthStatus,
        protocol_version: String,
    },
    AuthorizationResponse {
        result: AuthorizationResult,
    },
    ApprovalResponse {
        approval: ApprovalRecord,
    },
    ApprovalListResponse {
        approvals: Vec<ApprovalRecord>,
    },
    ApprovalVerifyResponse {
        valid: bool,
    },
    ApprovalExpireResponse {
        expired: usize,
    },
    ExecutionResponse {
        approval: ApprovalRecord,
        execution: ExecutionRecord,
    },
    ExecutionRecoverResponse {
        recovered: usize,
    },
    IdentityPrincipalsResponse {
        principals: Vec<Principal>,
    },
    IdentityPrincipalResponse {
        principal: Principal,
    },
    IdentityScopesResponse {
        principal_id: String,
        scopes: Vec<String>,
    },
    IdentityPermissionResponse {
        principal_id: String,
        scope: String,
        allowed: bool,
        reason: Option<String>,
    },
    IdentityDevicesResponse {
        devices: Vec<Device>,
    },
    IdentityPairingResponse {
        session: PairingSession,
    },
    IdentityOwnerResponse {
        initialized: bool,
        principal: Option<Principal>,
        credential_id: Option<String>,
        created_principal: bool,
        created_credential: bool,
    },
    IdentityRevocationResponse {
        summary: RevocationSummary,
    },
    Error {
        error: String,
    },
}

impl IpcFrame {
    pub fn serialize(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_frame_roundtrip() {
        let frame = IpcFrame::Event {
            event: crate::events::types::Event::new(
                "system.started",
                "test",
                serde_json::json!({}),
            ),
        };
        let json = frame.serialize().unwrap();
        let back: IpcFrame = serde_json::from_str(&json).unwrap();
        match back {
            IpcFrame::Event { event } => assert_eq!(event.kind, "system.started"),
            _ => panic!("esperado Event"),
        }
    }

    #[test]
    fn hello_frame_roundtrip() {
        let frame = IpcFrame::Hello {
            protocol_version: IPC_PROTOCOL_VERSION.to_string(),
            component_id: "python-runtime".to_string(),
        };
        let json = frame.serialize().unwrap();
        assert!(json.contains("\"Hello\""));
        let back: IpcFrame = serde_json::from_str(&json).unwrap();
        match back {
            IpcFrame::Hello {
                protocol_version,
                component_id,
            } => {
                assert_eq!(protocol_version, IPC_PROTOCOL_VERSION);
                assert_eq!(component_id, "python-runtime");
            }
            _ => panic!("esperado Hello"),
        }
    }

    #[test]
    fn protocol_major_is_parsed_strictly() {
        assert_eq!(protocol_major("1.0.0"), Some(1));
        assert_eq!(protocol_major("1"), Some(1));
        assert_eq!(protocol_major("2.3.4"), Some(2));
        assert_eq!(protocol_major(""), None);
        assert_eq!(protocol_major("v1"), None);
        assert_eq!(protocol_major("1beta"), None);
        assert!(protocol_is_compatible("1.9.9"));
        assert!(!protocol_is_compatible("2.0.0"));
    }

    #[test]
    fn identity_frames_roundtrip_and_serialize_responses() {
        let frame = IpcFrame::IdentityCreatePrincipal {
            actor_id: "prn_owner".to_string(),
            kind: "user".to_string(),
            display_name: "Wife".to_string(),
            role_id: "family".to_string(),
            parent_id: None,
            max_autonomy: 1,
        };
        let json = frame.serialize().unwrap();
        assert!(json.contains("\"IdentityCreatePrincipal\""));
        let back: IpcFrame = serde_json::from_str(&json).unwrap();
        match back {
            IpcFrame::IdentityCreatePrincipal {
                actor_id,
                role_id,
                max_autonomy,
                ..
            } => {
                assert_eq!(actor_id, "prn_owner");
                assert_eq!(role_id, "family");
                assert_eq!(max_autonomy, 1);
            }
            _ => panic!("esperado IdentityCreatePrincipal"),
        }

        let response = OutgoingFrame::IdentityPermissionResponse {
            principal_id: "prn_guest".to_string(),
            scope: "tools.write".to_string(),
            allowed: false,
            reason: Some("escopo ausente".to_string()),
        };
        let json = serde_json::to_string(&response).unwrap();
        assert!(json.contains("\"IdentityPermissionResponse\""));
        assert!(json.contains("\"allowed\":false"));
    }
}
