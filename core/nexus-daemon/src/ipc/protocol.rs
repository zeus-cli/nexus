use serde::{Deserialize, Serialize};

use crate::contracts::{
    ApprovalDecision, ApprovalRecord, ApprovalRequest, AuthorizationRequest, AuthorizationResult,
};
use crate::events::types::Event;
use crate::health::check::HealthStatus;

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
    ApprovalRequest { request: ApprovalRequest },
    /// Cliente registra uma decisão humana.
    ApprovalDecision { decision: ApprovalDecision },
    /// Cliente consulta o estado de uma aprovação.
    ApprovalStatus { approval_id: String },
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
}
