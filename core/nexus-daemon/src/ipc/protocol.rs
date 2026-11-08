use serde::{Deserialize, Serialize};

/// Frame enviado/recebido pelo soquete Unix (nova linha JSON).
/// Espelha o protocolo do sentinel Rust (linha JSON com `source`, `kind`, ...).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "body")]
pub enum IpcFrame {
    /// Cliente publica um evento no barramento.
    Event { event: crate::events::types::Event },
    /// Cliente se inscreve em padrões de evento.
    Subscribe { patterns: Vec<String> },
    /// Cliente cancela uma inscrição pelo id.
    Unsubscribe { id: String },
    /// Cliente solicita health check.
    Health,
    /// Ack de evento publicado.
    Ack { ok: bool, event_id: Option<String>, error: Option<String> },
    /// Confirmação de inscrição.
    Subscribed { id: String },
    /// Evento entregue a um assinante.
    Delivered { event: crate::events::types::Event },
    /// Erro geral.
    Error { error: String },
}

/// Mensagem de saída direcionada a um cliente específico (envio pelo socket).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", content = "body")]
pub enum OutgoingFrame {
    Ack { ok: bool, event_id: Option<String>, error: Option<String> },
    Subscribed { id: String },
    Delivered { event: crate::events::types::Event },
    HealthResponse { status: crate::health::check::HealthStatus },
    Error { error: String },
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
            event: crate::events::types::Event::new("system.started", "test", serde_json::json!({})),
        };
        let json = frame.serialize().unwrap();
        let back: IpcFrame = serde_json::from_str(&json).unwrap();
        match back {
            IpcFrame::Event { event } => assert_eq!(event.kind, "system.started"),
            _ => panic!("esperado Event"),
        }
    }
}
