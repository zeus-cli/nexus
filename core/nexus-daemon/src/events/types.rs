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
        let valid = kind.split('.').all(|segment| {
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
#[serde(deny_unknown_fields)]
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

/// Limite do domínio seguro de inteiros IEEE 754 / I-JSON.
const IJSON_MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

/// Verifica se o valor está no domínio I-JSON usado pelos hashes canônicos.
///
/// O Python (`rfc8785`) rejeita inteiros fora de ±(2^53−1) e números não
/// finitos. O Rust precisa da mesma regra para não calcular um hash que o
/// Python recusaria.
pub(crate) fn is_ijson(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                value.unsigned_abs() <= IJSON_MAX_SAFE_INTEGER
            } else if let Some(value) = number.as_u64() {
                value <= IJSON_MAX_SAFE_INTEGER
            } else if let Some(value) = number.as_f64() {
                value.is_finite()
            } else {
                false
            }
        }
        Value::Array(items) => items.iter().all(is_ijson),
        Value::Object(map) => map.values().all(is_ijson),
        _ => true,
    }
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
    /// Mesma definição do Python (`dedup_key`): payload em JSON canônico
    /// RFC 8785 (JCS), campos unidos por U+001F e SHA-256 hexadecimal.
    /// Payloads fora do domínio I-JSON não recebem chave.
    pub fn dedup_key(&self) -> Option<String> {
        if !self.dedup || !is_ijson(&self.payload) {
            return None;
        }
        let canonical_payload = match serde_jcs::to_string(&self.payload) {
            Ok(value) => value,
            Err(_) => return None,
        };
        let material = format!(
            "{}\x1f{}\x1f{}\x1f{}",
            self.kind, self.source, self.severity, canonical_payload
        );
        let mut hasher = Sha256::new();
        hasher.update(material.as_bytes());
        Some(hex::encode(hasher.finalize()))
    }

    /// Tamanho do payload serializado em bytes (para aplicação de limite).
    pub fn payload_size(&self) -> usize {
        serde_json::to_vec(&self.payload)
            .map(|v| v.len())
            .unwrap_or(0)
    }

    /// Valida o subconjunto do contrato v1 que o IPC aceita antes de persistir.
    ///
    /// `created_at` é checado como ISO-8601 UTC/offset porque um evento
    /// inválido não deve entrar no store vindo de um cliente local.
    pub fn validate_contract(&self) -> Result<(), String> {
        if self.event_id.trim().is_empty() {
            return Err("event_id vazio".to_string());
        }
        if !valid_event_kind(&self.kind) {
            return Err(format!("kind inválido: {}", self.kind));
        }
        if self.source.trim().is_empty() || self.source.chars().any(char::is_whitespace) {
            return Err("source deve ser não vazio e sem espaços".to_string());
        }
        if !matches!(
            self.severity.as_str(),
            "debug" | "info" | "warning" | "error" | "critical"
        ) {
            return Err(format!("severity inválida: {}", self.severity));
        }
        if !self.payload.is_object() {
            return Err("payload deve ser um objeto JSON".to_string());
        }
        if let Some(correlation_id) = &self.correlation_id {
            if correlation_id.is_empty() {
                return Err("correlation_id não pode ser vazio quando presente".to_string());
            }
        }
        if !valid_iso8601(&self.created_at) {
            return Err(format!("created_at inválido: {}", self.created_at));
        }
        Ok(())
    }
}

fn valid_event_kind(kind: &str) -> bool {
    let mut segments = kind.split('.');
    let first = segments.next().unwrap_or("");
    let mut chars = first.chars();
    match chars.next() {
        Some(c) if c.is_ascii_lowercase() => {}
        _ => return false,
    }
    if !chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_') {
        return false;
    }
    segments.all(|segment| {
        !segment.is_empty()
            && segment
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
    })
}

/// Validação estrutural de `date-time` (ISO-8601/RFC 3339) sem dependência
/// de `chrono`: data, hora, fração opcional e offset `Z`/`±HH:MM`.
fn valid_iso8601(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 20 {
        return false;
    }
    let digits = |slice: &[u8]| slice.iter().all(u8::is_ascii_digit);
    let number = |slice: &[u8]| -> Option<u32> {
        if !digits(slice) {
            return None;
        }
        std::str::from_utf8(slice).ok()?.parse().ok()
    };
    if bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
    {
        return false;
    }
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = (
        number(&bytes[0..4]),
        number(&bytes[5..7]),
        number(&bytes[8..10]),
        number(&bytes[11..13]),
        number(&bytes[14..16]),
        number(&bytes[17..19]),
    ) else {
        return false;
    };
    if year < 1 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return false;
    }
    if hour > 23 || minute > 59 || second > 60 {
        return false;
    }

    let mut index = 19;
    if bytes[index] == b'.' {
        index += 1;
        let start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        if index == start || index - start > 9 {
            return false;
        }
    }
    if index >= bytes.len() {
        return false;
    }
    match bytes[index] {
        b'Z' => index + 1 == bytes.len(),
        b'+' | b'-' => {
            if index + 6 != bytes.len() || bytes[index + 3] != b':' {
                return false;
            }
            let Some(offset_hour) = number(&bytes[index + 1..index + 3]) else {
                return false;
            };
            let Some(offset_minute) = number(&bytes[index + 4..index + 6]) else {
                return false;
            };
            offset_hour <= 23 && offset_minute <= 59
        }
        _ => false,
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
        let ev = Event::new(
            "system.started",
            "nexus-daemon",
            serde_json::json!({"pid": 1}),
        )
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
        let ev = Event::new(
            "agent.task.started",
            "cognitive_core",
            serde_json::json!({"task_id": "x"}),
        );
        let k1 = ev.dedup_key().unwrap();
        let k2 = ev.dedup_key().unwrap();
        assert_eq!(k1, k2);
        assert_eq!(k1.len(), 64); // SHA-256 hex
    }

    #[test]
    fn dedup_opt_out_returns_none() {
        let ev =
            Event::new("system.heartbeat", "nexus-daemon", serde_json::json!({})).with_dedup(false);
        assert!(ev.dedup_key().is_none());
    }

    #[test]
    fn invalid_kind_rejected() {
        assert!(EventKind::new("Invalid Kind").is_err());
        assert!(EventKind::new("UPPER").is_err());
        assert!(EventKind::new("agent.task.started").is_ok());
    }

    #[test]
    fn wire_rejects_derived_dedup_key() {
        let json = serde_json::json!({
            "event_id": "0123456789abcdef0123456789abcdef",
            "kind": "system.started",
            "source": "test",
            "severity": "info",
            "payload": {},
            "correlation_id": null,
            "created_at": "2026-11-14T12:00:00Z",
            "dedup": true,
            "dedup_key": "nao-deve-trafegar"
        });
        let error = serde_json::from_value::<Event>(json).unwrap_err();
        assert!(
            error.to_string().contains("unknown field"),
            "dedup_key derivado não pode entrar no wire: {error}"
        );
    }

    #[test]
    fn contract_validation_accepts_fixture_shapes() {
        let event = Event::new("bridge.ping", "test", serde_json::json!({"n": 1}));
        assert!(event.validate_contract().is_ok());
        assert!(valid_iso8601("2026-11-14T12:00:00Z"));
        assert!(valid_iso8601("2026-11-14T12:00:00.123456Z"));
        assert!(valid_iso8601("2026-11-14T12:00:00+03:30"));
        assert!(!valid_iso8601("2026-11-14T12:00:00"));
        assert!(!valid_iso8601("not-a-date"));
        assert!(!valid_iso8601("2026-13-14T12:00:00Z"));
    }

    #[test]
    fn contract_validation_rejects_invalid_fields() {
        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.kind = "Bridge Ping".to_string();
        assert!(event.validate_contract().is_err());

        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.source = "two words".to_string();
        assert!(event.validate_contract().is_err());

        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.severity = "notice".to_string();
        assert!(event.validate_contract().is_err());

        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.payload = serde_json::json!(["not", "object"]);
        assert!(event.validate_contract().is_err());

        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.created_at = "2026-11-14 12:00:00".to_string();
        assert!(event.validate_contract().is_err());

        let mut event = Event::new("bridge.ping", "test", serde_json::json!({}));
        event.correlation_id = Some(String::new());
        assert!(event.validate_contract().is_err());
    }
}
