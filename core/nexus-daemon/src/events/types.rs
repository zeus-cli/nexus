use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// Chave hierárquica de tipo de evento, ex.: ``agent.task.started``.
///
/// Segue o mesmo padrão do Python (`events.KIND_PATTERN`):
/// minúsculas, dígitos, underscore e ponto, começando com letra.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventKind(String);

impl EventKind {
    pub fn new(kind: &str) -> Result<Self, EventError> {
        if kind.is_empty() {
            return Err(EventError::InvalidKind(kind.to_string()));
        }
        let valid = kind
            .split('.')
            .all(|segment| {
                !segment.is_empty()
                    && segment
                        .chars()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
            });
        if !valid {
            return Err(EventError::InvalidKind(kind.to_string()));
        }
        Ok(EventKind(kind.to_string()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for EventKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Severidade de evento (mesmo conjunto do Python).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Debug,
    Info,
    Warning,
    Error,
    Critical,
}

impl std::fmt::Display for Severity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Severity::Debug => f.write_str("debug"),
            Severity::Info => f.write_str("info"),
            Severity::Warning => f.write_str("warning"),
            Severity::Error => f.write_str("error"),
            Severity::Critical => f.write_str("critical"),
        }
    }
}

/// Modelo de evento do NEXUS.
///
/// Compatível com o contrato Python (`events.Event.to_dict()`): todos os
/// campos são serializados com os mesmos nomes de chave para que Rust e
/// Python troquem eventos via JSON sem perda de informação.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub event_id: String,
    pub kind: String,
    pub source: String,
    #[serde(default)]
    pub payload: Value,
    #[serde(default = "default_severity")]
    pub severity: String,
    pub correlation_id: Option<String>,
    pub created_at: String,
    #[serde(default)]
    pub dedup: bool,
}

fn default_severity() -> String {
    "info".to_string()
}

impl Event {
    /// Cria um novo evento com identidade estável e timestamp UTC.
    pub fn new(
        kind: impl Into<String>,
        source: impl Into<String>,
        payload: impl Into<Value>,
    ) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("relógio do sistema anterior à época Unix");
        let secs = now.as_secs();
        let nanos = now.subsec_nanos();
        // Formato ISO-8601 com precisão de milissegundos e sufixo Z.
        let created_at = format!(
            "{}.{:03}Z",
            chrono_naive_utc(secs, nanos),
            nanos / 1_000_000
        );
        Self {
            event_id: Uuid::new_v4().to_string(),
            kind: kind.into(),
            source: source.into(),
            payload: payload.into(),
            severity: "info".to_string(),
            correlation_id: None,
            created_at,
            dedup: true,
        }
    }

    pub fn with_severity(mut self, severity: Severity) -> Self {
        self.severity = severity.to_string();
        self
    }

    pub fn with_correlation(mut self, correlation_id: impl Into<String>) -> Self {
        self.correlation_id = Some(correlation_id.into());
        self
    }

    pub fn with_dedup(mut self, dedup: bool) -> Self {
        self.dedup = dedup;
        self
    }

    /// Chave de idempotência: SHA-256(kind, source, severity, payload canônico).
    /// Mesma definição do Python (`dedup_key`).
    pub fn dedup_key(&self) -> Option<String> {
        if !self.dedup {
            return None;
        }
        let material = format!(
            "{}\x1f{}\x1f{}\x1f{}",
            self.kind,
            self.source,
            self.severity,
            serde_json::to_string(&self.payload).unwrap_or_default()
        );
        let mut hasher = Sha256::new();
        hasher.update(material.as_bytes());
        Some(hex::encode(hasher.finalize()))
    }

    /// Tamanho do payload serializado em bytes (para aplicação de limite).
    pub fn payload_size(&self) -> usize {
        serde_json::to_vec(&self.payload).map(|v| v.len()).unwrap_or(0)
    }
}

/// Constrói timestamp ISO-8601 a partir de segundos+nanos desde a época Unix,
/// sem depender do `chrono` (mantém o conjunto de dependências enxuto).
pub(crate) fn chrono_naive_utc(secs: u64, _nanos: u32) -> String {
    // Dias desde 1970-01-01.
    let mut days = secs / 86_400;
    let secs_of_day = secs % 86_400;
    let hour = secs_of_day / 3600;
    let minute = (secs_of_day % 3600) / 60;
    let second = secs_of_day % 60;

    // Conversão de dias para ano/mês/dia (algoritmo civil de Howard Hinnant).
    let z = days as i64 + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if m <= 2 { y + 1 } else { y };

    format!("{year:04}-{m:02}-{d:02}T{hour:02}:{minute:02}:{second:02}")
}

#[derive(Debug, thiserror::Error)]
pub enum EventError {
    #[error("tipo de evento inválido: {0}")]
    InvalidKind(String),
    #[error("falha de serialização JSON: {0}")]
    Serialization(#[from] serde_json::Error),
}

// `hex` é necessário para `dedup_key`. Adicionado ao Cargo.toml abaixo.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_roundtrip_json() {
        let ev = Event::new("system.started", "nexus-daemon", serde_json::json!({"pid": 1}))
            .with_severity(Severity::Info)
            .with_correlation("corr-123");
        let json = serde_json::to_string(&ev).unwrap();
        let back: Event = serde_json::from_str(&json).unwrap();
        assert_eq!(back.event_id, ev.event_id);
        assert_eq!(back.kind, "system.started");
        assert_eq!(back.severity, "info");
        assert_eq!(back.correlation_id, Some("corr-123".to_string()));
    }

    #[test]
    fn dedup_key_is_stable() {
        let ev = Event::new("agent.task.started", "cognitive_core", serde_json::json!({"task_id": "x"}));
        let k1 = ev.dedup_key().unwrap();
        let k2 = ev.dedup_key().unwrap();
        assert_eq!(k1, k2);
        assert_eq!(k1.len(), 64); // SHA-256 hex
    }

    #[test]
    fn dedup_opt_out_returns_none() {
        let ev = Event::new("system.heartbeat", "nexus-daemon", serde_json::json!({}))
            .with_dedup(false);
        assert!(ev.dedup_key().is_none());
    }

    #[test]
    fn invalid_kind_rejected() {
        assert!(EventKind::new("Invalid Kind").is_err());
        assert!(EventKind::new("UPPER").is_err());
        assert!(EventKind::new("agent.task.started").is_ok());
    }
}
