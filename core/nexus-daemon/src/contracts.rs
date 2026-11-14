//! Contratos JSON v1 compartilhados com o Python.
//!
//! Os schemas em `contracts/schemas` são a fonte da verdade. O módulo é usado
//! em produção pelo IPC de controle (T4) e pelos testes de contrato: os
//! mesmos fixtures validam Python e Rust.

#![allow(dead_code)]

use crate::events::types::is_ijson;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const CONTRACT_VERSION: &str = "1.0.0";

const MAX_IDENTIFIER: usize = 256;
const MAX_SCOPE: usize = 4096;
const MAX_JUSTIFICATION: usize = 8192;

#[derive(Debug, thiserror::Error)]
pub enum ContractError {
    #[error("campo obrigatório vazio: {0}")]
    EmptyField(&'static str),
    #[error("campo {field} excede o limite de {max} bytes")]
    FieldTooLong { field: &'static str, max: usize },
    #[error("valor inválido para {field}: {value}")]
    InvalidField { field: &'static str, value: String },
    #[error("params deve ser um objeto JSON")]
    ParamsNotObject,
    #[error("params fora do domínio I-JSON e não pode ser assinado")]
    NonIjsonParams,
    #[error("falha ao canonicalizar {field}: {reason}")]
    Canonicalization { field: &'static str, reason: String },
}

fn require_non_empty(field: &'static str, value: &str) -> Result<(), ContractError> {
    if value.trim().is_empty() {
        return Err(ContractError::EmptyField(field));
    }
    Ok(())
}

fn require_max(field: &'static str, value: &str, max: usize) -> Result<(), ContractError> {
    if value.len() > max {
        return Err(ContractError::FieldTooLong { field, max });
    }
    Ok(())
}

fn require_enum(field: &'static str, value: &str, allowed: &[&str]) -> Result<(), ContractError> {
    if !allowed.contains(&value) {
        return Err(ContractError::InvalidField {
            field,
            value: value.to_string(),
        });
    }
    Ok(())
}

fn require_identifier(field: &'static str, value: &str) -> Result<(), ContractError> {
    require_non_empty(field, value)?;
    require_max(field, value, MAX_IDENTIFIER)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContractEvent {
    pub event_id: String,
    pub kind: String,
    pub source: String,
    pub severity: String,
    pub payload: Value,
    pub correlation_id: Option<String>,
    pub created_at: String,
    pub dedup: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthorizationRequest {
    pub component_id: String,
    pub tool_id: String,
    pub params: Value,
    pub task_id: String,
    pub plan_id: String,
    pub risk_level: String,
    pub autonomy_level: u8,
    pub required_autonomy: u8,
    pub approval_id: Option<String>,
    pub execution_mode: String,
    pub access_level: String,
    pub policy_version: String,
    pub required_capability: Option<String>,
    pub fs_scope: String,
    pub net_scope: String,
    pub resource_limits: String,
    pub justification: String,
}

impl AuthorizationRequest {
    /// Valida o contrato antes de qualquer decisão de segurança.
    pub fn validate(&self) -> Result<(), ContractError> {
        require_identifier("component_id", &self.component_id)?;
        require_identifier("tool_id", &self.tool_id)?;
        require_identifier("task_id", &self.task_id)?;
        require_identifier("plan_id", &self.plan_id)?;
        require_identifier("policy_version", &self.policy_version)?;
        require_non_empty("fs_scope", &self.fs_scope)?;
        require_max("fs_scope", &self.fs_scope, MAX_SCOPE)?;
        require_non_empty("net_scope", &self.net_scope)?;
        require_max("net_scope", &self.net_scope, MAX_SCOPE)?;
        require_max("justification", &self.justification, MAX_JUSTIFICATION)?;
        if !self.params.is_object() {
            return Err(ContractError::ParamsNotObject);
        }
        require_enum(
            "risk_level",
            &self.risk_level,
            &["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        )?;
        require_enum("execution_mode", &self.execution_mode, &["HOST", "SANDBOX"])?;
        require_enum("access_level", &self.access_level, &["READ", "WRITE"])?;
        require_enum(
            "resource_limits",
            &self.resource_limits,
            &["default", "low", "medium", "high"],
        )?;
        if self.autonomy_level > 4 || self.required_autonomy > 4 {
            return Err(ContractError::InvalidField {
                field: "autonomy_level",
                value: format!("{}/{}", self.autonomy_level, self.required_autonomy),
            });
        }
        if let Some(approval_id) = &self.approval_id {
            require_identifier("approval_id", approval_id)?;
        }
        if let Some(capability) = &self.required_capability {
            require_identifier("required_capability", capability)?;
        }
        Ok(())
    }

    /// Projeção usada pelo Approval Manager para vincular a aprovação à
    /// operação exata (mesmo hash canônico do Python).
    pub fn approval_request(&self) -> ApprovalRequest {
        ApprovalRequest {
            tool_id: self.tool_id.clone(),
            params: self.params.clone(),
            task_id: self.task_id.clone(),
            plan_id: self.plan_id.clone(),
            execution_mode: self.execution_mode.clone(),
            access_level: self.access_level.clone(),
            fs_scope: self.fs_scope.clone(),
            net_scope: self.net_scope.clone(),
            resource_limits: self.resource_limits.clone(),
            policy_version: self.policy_version.clone(),
            justification: self.justification.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthorizationResult {
    pub decision: String,
    pub request_id: String,
    pub reason: String,
    pub approved: bool,
    pub execution_id: Option<String>,
    pub metadata: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalRequest {
    pub tool_id: String,
    pub params: Value,
    pub task_id: String,
    pub plan_id: String,
    pub execution_mode: String,
    pub access_level: String,
    pub fs_scope: String,
    pub net_scope: String,
    pub resource_limits: String,
    pub policy_version: String,
    pub justification: String,
}

impl ApprovalRequest {
    /// Valida o contrato; `params` precisa estar no domínio I-JSON porque o
    /// hash SHA-256 é um contrato Python ↔ Rust.
    pub fn validate(&self) -> Result<(), ContractError> {
        require_identifier("tool_id", &self.tool_id)?;
        require_identifier("task_id", &self.task_id)?;
        require_identifier("plan_id", &self.plan_id)?;
        require_identifier("policy_version", &self.policy_version)?;
        require_non_empty("fs_scope", &self.fs_scope)?;
        require_max("fs_scope", &self.fs_scope, MAX_SCOPE)?;
        require_non_empty("net_scope", &self.net_scope)?;
        require_max("net_scope", &self.net_scope, MAX_SCOPE)?;
        require_max("justification", &self.justification, MAX_JUSTIFICATION)?;
        if !self.params.is_object() {
            return Err(ContractError::ParamsNotObject);
        }
        if !is_ijson(&self.params) {
            return Err(ContractError::NonIjsonParams);
        }
        require_enum("execution_mode", &self.execution_mode, &["HOST", "SANDBOX"])?;
        require_enum("access_level", &self.access_level, &["READ", "WRITE"])?;
        require_enum(
            "resource_limits",
            &self.resource_limits,
            &["default", "low", "medium", "high"],
        )?;
        Ok(())
    }

    /// SHA-256 do approval_request em JSON canônico RFC 8785.
    ///
    /// O algoritmo é o mesmo de `approval_request_hash` no Python: UTF-8,
    /// chaves ordenadas, números I-JSON e `params` serializado como string.
    pub fn params_hash(&self) -> Result<String, ContractError> {
        self.validate()?;
        let params_json = serde_jcs::to_string(&self.params).map_err(|error| {
            ContractError::Canonicalization {
                field: "params",
                reason: error.to_string(),
            }
        })?;
        let payload = serde_json::json!({
            "tool_id": &self.tool_id,
            "params": params_json,
            "task_id": &self.task_id,
            "plan_id": &self.plan_id,
            "execution_mode": &self.execution_mode,
            "access_level": &self.access_level,
            "fs_scope": &self.fs_scope,
            "net_scope": &self.net_scope,
            "resource_limits": &self.resource_limits,
            "policy_version": &self.policy_version,
        });
        let payload_json =
            serde_jcs::to_string(&payload).map_err(|error| ContractError::Canonicalization {
                field: "approval_request",
                reason: error.to_string(),
            })?;
        Ok(hex::encode(Sha256::digest(payload_json.as_bytes())))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalRecord {
    pub approval_id: String,
    pub tool_id: String,
    pub params_hash: String,
    pub task_id: String,
    pub plan_id: String,
    pub execution_mode: String,
    pub access_level: String,
    pub fs_scope: Option<String>,
    pub net_scope: Option<String>,
    pub resource_limits: Option<String>,
    pub policy_version: String,
    pub justification: Option<String>,
    pub status: String,
    pub created_at: f64,
    pub expires_at: f64,
    pub decided_at: Option<f64>,
    pub decided_by: Option<String>,
    pub consumed_at: Option<f64>,
    pub risk_level: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalDecision {
    pub approval_id: String,
    pub decision: String,
    pub approver: String,
    pub timestamp: f64,
}

impl ApprovalDecision {
    pub fn validate(&self) -> Result<(), ContractError> {
        require_identifier("approval_id", &self.approval_id)?;
        require_identifier("approver", &self.approver)?;
        require_enum("decision", &self.decision, &["APPROVED", "DENIED"])?;
        if !self.timestamp.is_finite() || self.timestamp < 0.0 {
            return Err(ContractError::InvalidField {
                field: "timestamp",
                value: self.timestamp.to_string(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionResult {
    pub tool_id: String,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub duration_ms: u64,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolSpec {
    pub tool_id: String,
    pub version: String,
    pub description: String,
    pub risk: String,
    pub access: String,
    pub execution_mode: String,
    pub idempotent: bool,
    pub timeout_s: f64,
    pub input_schema: Value,
    pub output_schema: Value,
    pub required_capabilities: Vec<String>,
    pub resource_limits: Value,
    pub reversible: bool,
    pub metadata: Value,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::types::{is_ijson, Event};
    use std::path::{Path, PathBuf};

    fn contracts_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("contracts")
    }

    fn read_json(path: &Path) -> Value {
        let raw = std::fs::read_to_string(path)
            .unwrap_or_else(|error| panic!("falha ao ler {}: {error}", path.display()));
        serde_json::from_str(&raw)
            .unwrap_or_else(|error| panic!("JSON inválido em {}: {error}", path.display()))
    }

    fn manifest() -> Value {
        read_json(&contracts_root().join("fixtures").join("manifest.json"))
    }

    fn schema_path(name: &str) -> PathBuf {
        contracts_root()
            .join("schemas")
            .join(format!("{name}.schema.json"))
    }

    fn fixture_path(relative: &str) -> PathBuf {
        contracts_root().join("fixtures").join(relative)
    }

    #[test]
    fn manifest_and_schema_versions_are_consistent() {
        let manifest = manifest();
        assert_eq!(manifest["contract_version"], CONTRACT_VERSION);
        for key in ["valid", "invalid"] {
            for entry in manifest[key].as_array().expect("lista de fixtures") {
                let schema_name = entry["schema"].as_str().expect("schema");
                let schema = read_json(&schema_path(schema_name));
                assert_eq!(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema"
                );
                assert!(schema["$id"]
                    .as_str()
                    .expect("$id")
                    .starts_with("urn:nexus:contracts:v1:"));
                let fixture = entry["file"].as_str().expect("file");
                let _ = read_json(&fixture_path(fixture));
            }
        }
    }

    #[test]
    fn valid_fixtures_match_schemas() {
        let manifest = manifest();
        for entry in manifest["valid"].as_array().expect("fixtures válidas") {
            let schema_name = entry["schema"].as_str().expect("schema");
            let fixture = entry["file"].as_str().expect("file");
            let schema = read_json(&schema_path(schema_name));
            let instance = read_json(&fixture_path(fixture));
            let validator = jsonschema::options()
                .should_validate_formats(true)
                .build(&schema)
                .unwrap_or_else(|error| panic!("schema {schema_name} inválido: {error}"));
            assert!(
                validator.is_valid(&instance),
                "fixture deveria ser válida: {fixture}"
            );
        }
    }

    #[test]
    fn invalid_fixtures_are_rejected() {
        let manifest = manifest();
        for entry in manifest["invalid"].as_array().expect("fixtures inválidas") {
            let schema_name = entry["schema"].as_str().expect("schema");
            let fixture = entry["file"].as_str().expect("file");
            let schema = read_json(&schema_path(schema_name));
            let instance = read_json(&fixture_path(fixture));
            let validator = jsonschema::options()
                .should_validate_formats(true)
                .build(&schema)
                .unwrap_or_else(|error| panic!("schema {schema_name} inválido: {error}"));
            assert!(
                !validator.is_valid(&instance),
                "fixture deveria ser rejeitada: {fixture}"
            );
        }
    }

    #[test]
    fn valid_fixtures_deserialize_into_rust_types() {
        let event: ContractEvent =
            serde_json::from_value(read_json(&fixture_path("valid/event.json"))).expect("event");
        assert_eq!(event.kind, "agent.task.started");

        let production_event: Event =
            serde_json::from_value(read_json(&fixture_path("valid/event.json")))
                .expect("Event de produção deve aceitar o wire v1");
        assert_eq!(production_event.source, "cognitive_core");
        assert_eq!(production_event.dedup_key().map(|key| key.len()), Some(64));

        let _: AuthorizationRequest =
            serde_json::from_value(read_json(&fixture_path("valid/authorization_request.json")))
                .expect("authorization_request");
        let _: AuthorizationResult =
            serde_json::from_value(read_json(&fixture_path("valid/authorization_result.json")))
                .expect("authorization_result");
        let _: ApprovalRequest =
            serde_json::from_value(read_json(&fixture_path("valid/approval_request.json")))
                .expect("approval_request");
        let _: ApprovalRecord =
            serde_json::from_value(read_json(&fixture_path("valid/approval_record.json")))
                .expect("approval_record");
        let _: ApprovalDecision =
            serde_json::from_value(read_json(&fixture_path("valid/approval_decision.json")))
                .expect("approval_decision");
        let _: ExecutionResult =
            serde_json::from_value(read_json(&fixture_path("valid/execution_result.json")))
                .expect("execution_result");
        let _: ToolSpec = serde_json::from_value(read_json(&fixture_path("valid/tool_spec.json")))
            .expect("tool_spec");
    }

    #[test]
    fn event_dedup_vectors_match_rust() {
        let manifest = manifest();
        let vectors = manifest["vectors"].as_array().expect("vetores");
        let mut checked = 0;
        for entry in vectors {
            if entry["kind"] != "event_dedup" {
                continue;
            }
            let fixture = entry["file"].as_str().expect("file");
            let vector = read_json(&fixture_path(fixture));
            let event: Event =
                serde_json::from_value(vector["event"].clone()).expect("evento do vetor");
            assert_eq!(
                event.dedup_key().as_deref(),
                vector["expected_dedup_key"].as_str(),
                "vetor divergente: {fixture}"
            );
            checked += 1;
        }
        assert!(checked >= 2, "esperava vetores ASCII e numérico");
    }

    fn approval_hash(request: &ApprovalRequest) -> String {
        request.params_hash().expect("hash canônico")
    }

    #[test]
    fn approval_hash_vectors_match_rust() {
        let manifest = manifest();
        let vectors = manifest["vectors"].as_array().expect("vetores");
        let mut checked = 0;
        for entry in vectors {
            if entry["kind"] != "approval_hash" {
                continue;
            }
            let fixture = entry["file"].as_str().expect("file");
            let vector = read_json(&fixture_path(fixture));
            let request: ApprovalRequest =
                serde_json::from_value(vector["request"].clone()).expect("approval request");
            assert_eq!(
                approval_hash(&request),
                vector["expected_hash"].as_str().expect("hash esperado"),
                "vetor divergente: {fixture}"
            );
            checked += 1;
        }
        assert!(checked >= 3, "esperava vetores ASCII, não-ASCII e numérico");

        let vector = read_json(&fixture_path("vectors/approval_hash.json"));
        let request: ApprovalRequest =
            serde_json::from_value(vector["request"].clone()).expect("approval request");
        let record: ApprovalRecord =
            serde_json::from_value(read_json(&fixture_path("valid/approval_record.json")))
                .expect("approval record");
        assert_eq!(record.params_hash, approval_hash(&request));
    }

    #[test]
    fn non_ijson_numbers_are_outside_the_hash_domain() {
        let unsafe_integer = serde_json::json!({"n": 9_007_199_254_740_992u64});
        assert!(!is_ijson(&unsafe_integer));

        let event = Event::new("system.metrics", "nexus-daemon", unsafe_integer);
        assert!(
            event.dedup_key().is_none(),
            "payload fora de I-JSON não deve ser deduplicado"
        );
    }
}
