//! Approval Manager mínimo do Control Plane (T4).
//!
//! O T4 expõe o ciclo de aprovação pelo IPC com as garantias que já são
//! possíveis sem mover a autoridade final: hash canônico vinculado à operação,
//! TTL, decisão única, claim atômico e uso único para autorização. A paridade
//! completa com o Python (revogação, CLAIMED → CONSUMED, persistência e
//! histórico) pertence ao T5.

use crate::contracts::{ApprovalDecision, ApprovalRecord, ApprovalRequest, ContractError};
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_APPROVAL_TTL_SECONDS: f64 = 3600.0;

#[derive(Debug, thiserror::Error)]
pub enum ApprovalError {
    #[error("approval inválida: {0}")]
    Invalid(String),
    #[error("approval não encontrada: {0}")]
    NotFound(String),
    #[error("approval {0} não está pendente")]
    NotPending(String),
    #[error("approval {0} expirou")]
    Expired(String),
    #[error("approval {0} não foi aprovada")]
    NotApproved(String),
    #[error("params divergem da aprovação vinculada")]
    HashMismatch,
    #[error("contrato: {0}")]
    Contract(#[from] ContractError),
}

/// Registro em memória. Reiniciar o daemon invalida approvals pendentes —
/// comportamento fail-closed até a persistência do T5.
pub struct ApprovalManager {
    approvals: Mutex<HashMap<String, ApprovalRecord>>,
    ttl_s: f64,
}

impl ApprovalManager {
    pub fn new() -> Self {
        Self::with_ttl_secs(DEFAULT_APPROVAL_TTL_SECONDS)
    }

    pub fn with_ttl_secs(ttl_s: f64) -> Self {
        assert!(
            ttl_s.is_finite() && ttl_s >= 0.0,
            "ttl de aprovação deve ser finito e não negativo"
        );
        Self {
            approvals: Mutex::new(HashMap::new()),
            ttl_s,
        }
    }

    pub fn ttl_secs(&self) -> f64 {
        self.ttl_s
    }

    /// Cria uma solicitação PENDING vinculada ao hash canônico da operação.
    pub fn request(
        &self,
        request: &ApprovalRequest,
        risk_level: Option<&str>,
    ) -> Result<ApprovalRecord, ApprovalError> {
        request.validate()?;
        if let Some(risk) = risk_level {
            if !matches!(risk, "LOW" | "MEDIUM" | "HIGH" | "CRITICAL") {
                return Err(ApprovalError::Invalid(format!(
                    "risk_level inválido: {risk}"
                )));
            }
        }
        let params_hash = request.params_hash()?;
        let now = unix_now();
        let approval_id = uuid::Uuid::new_v4().simple().to_string();
        let record = ApprovalRecord {
            approval_id: approval_id.clone(),
            tool_id: request.tool_id.clone(),
            params_hash,
            task_id: request.task_id.clone(),
            plan_id: request.plan_id.clone(),
            execution_mode: request.execution_mode.clone(),
            access_level: request.access_level.clone(),
            fs_scope: Some(request.fs_scope.clone()),
            net_scope: Some(request.net_scope.clone()),
            resource_limits: Some(request.resource_limits.clone()),
            policy_version: request.policy_version.clone(),
            justification: Some(request.justification.clone()),
            status: "PENDING".to_string(),
            created_at: now,
            expires_at: now + self.ttl_s,
            decided_at: None,
            decided_by: None,
            consumed_at: None,
            risk_level: risk_level.map(str::to_string),
        };
        let mut approvals = self.approvals.lock().expect("lock de approvals");
        if approvals.contains_key(&approval_id) {
            return Err(ApprovalError::Invalid("colisão de approval_id".to_string()));
        }
        approvals.insert(approval_id, record.clone());
        Ok(record)
    }

    /// Aplica uma decisão humana. Apenas PENDING pode ser decidida.
    pub fn decide(&self, decision: &ApprovalDecision) -> Result<ApprovalRecord, ApprovalError> {
        decision.validate()?;
        let now = unix_now();
        let mut approvals = self.approvals.lock().expect("lock de approvals");
        let record = approvals
            .get_mut(&decision.approval_id)
            .ok_or_else(|| ApprovalError::NotFound(decision.approval_id.clone()))?;
        refresh_expiry(record, now);
        match record.status.as_str() {
            "PENDING" => {}
            "APPROVED" | "DENIED" => {
                return Err(ApprovalError::NotPending(decision.approval_id.clone()))
            }
            "EXPIRED" => return Err(ApprovalError::Expired(decision.approval_id.clone())),
            _ => return Err(ApprovalError::NotPending(decision.approval_id.clone())),
        }
        record.status = decision.decision.clone();
        record.decided_at = Some(decision.timestamp);
        record.decided_by = Some(decision.approver.clone());
        Ok(record.clone())
    }

    /// Consulta o estado atual, expirando registros vencidos.
    pub fn status(&self, approval_id: &str) -> Result<ApprovalRecord, ApprovalError> {
        let now = unix_now();
        let mut approvals = self.approvals.lock().expect("lock de approvals");
        let record = approvals
            .get_mut(approval_id)
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        refresh_expiry(record, now);
        Ok(record.clone())
    }

    /// Verifica o vínculo e consome a aprovação para uma única autorização.
    ///
    /// O claim é atômico sob o mutex: duas chamadas concorrentes não podem
    /// consumir a mesma aprovação. A transição para CONSUMED acontece no
    /// ciclo de execução (T6), que ainda não existe no Control Plane.
    pub fn claim(
        &self,
        approval_id: &str,
        request: &ApprovalRequest,
    ) -> Result<ApprovalRecord, ApprovalError> {
        request.validate()?;
        let now = unix_now();
        let mut approvals = self.approvals.lock().expect("lock de approvals");
        let record = approvals
            .get_mut(approval_id)
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        refresh_expiry(record, now);
        match record.status.as_str() {
            "APPROVED" => {}
            "EXPIRED" => return Err(ApprovalError::Expired(approval_id.to_string())),
            "PENDING" | "DENIED" => {
                return Err(ApprovalError::NotApproved(approval_id.to_string()))
            }
            _ => return Err(ApprovalError::NotPending(approval_id.to_string())),
        }
        if record.params_hash != request.params_hash()? {
            return Err(ApprovalError::HashMismatch);
        }
        record.status = "CLAIMED".to_string();
        Ok(record.clone())
    }

    /// Expira solicitações pendentes/aprovadas vencidas e devolve a contagem.
    pub fn expire_stale(&self) -> usize {
        let now = unix_now();
        let mut approvals = self.approvals.lock().expect("lock de approvals");
        let mut expired = 0;
        for record in approvals.values_mut() {
            if matches!(record.status.as_str(), "PENDING" | "APPROVED") && record.expires_at <= now
            {
                record.status = "EXPIRED".to_string();
                expired += 1;
            }
        }
        expired
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.approvals.lock().expect("lock de approvals").len()
    }
}

impl Default for ApprovalManager {
    fn default() -> Self {
        Self::new()
    }
}

fn refresh_expiry(record: &mut ApprovalRecord, now: f64) {
    if matches!(record.status.as_str(), "PENDING" | "APPROVED") && record.expires_at <= now {
        record.status = "EXPIRED".to_string();
    }
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("relógio do sistema anterior à época Unix")
        .as_secs_f64()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request(params: serde_json::Value) -> ApprovalRequest {
        ApprovalRequest {
            tool_id: "sys.dangerous".to_string(),
            params,
            task_id: "task-1".to_string(),
            plan_id: "plan-1".to_string(),
            execution_mode: "HOST".to_string(),
            access_level: "WRITE".to_string(),
            fs_scope: "DEFAULT".to_string(),
            net_scope: "NONE".to_string(),
            resource_limits: "default".to_string(),
            policy_version: "1.0".to_string(),
            justification: "manual review".to_string(),
        }
    }

    fn decide(manager: &ApprovalManager, id: &str, decision: &str) -> ApprovalRecord {
        manager
            .decide(&ApprovalDecision {
                approval_id: id.to_string(),
                decision: decision.to_string(),
                approver: "operator".to_string(),
                timestamp: unix_now(),
            })
            .expect("decisão")
    }

    #[test]
    fn request_decide_and_claim_single_use() {
        let manager = ApprovalManager::new();
        let req = request(json!({"args": []}));
        let record = manager.request(&req, Some("HIGH")).expect("request");
        assert_eq!(record.status, "PENDING");
        assert_eq!(record.params_hash, req.params_hash().unwrap());
        assert_eq!(record.risk_level.as_deref(), Some("HIGH"));
        assert_eq!(
            manager.status(&record.approval_id).unwrap().status,
            "PENDING"
        );

        let approved = decide(&manager, &record.approval_id, "APPROVED");
        assert_eq!(approved.status, "APPROVED");
        assert_eq!(approved.decided_by.as_deref(), Some("operator"));

        let claimed = manager.claim(&record.approval_id, &req).expect("claim");
        assert_eq!(claimed.status, "CLAIMED");
        assert!(matches!(
            manager.claim(&record.approval_id, &req),
            Err(ApprovalError::NotPending(_))
        ));
    }

    #[test]
    fn hash_mismatch_does_not_consume_approval() {
        let manager = ApprovalManager::new();
        let req = request(json!({"args": ["--safe"]}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "APPROVED");

        let altered = request(json!({"args": ["--danger"]}));
        assert!(matches!(
            manager.claim(&record.approval_id, &altered),
            Err(ApprovalError::HashMismatch)
        ));
        assert_eq!(
            manager.status(&record.approval_id).unwrap().status,
            "APPROVED"
        );
        assert!(manager.claim(&record.approval_id, &req).is_ok());
    }

    #[test]
    fn denied_approval_cannot_be_claimed() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "DENIED");
        assert!(matches!(
            manager.claim(&record.approval_id, &req),
            Err(ApprovalError::NotApproved(_))
        ));
    }

    #[test]
    fn expired_approval_cannot_be_decided_or_claimed() {
        let manager = ApprovalManager::with_ttl_secs(0.0);
        let req = request(json!({}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        assert_eq!(
            manager.status(&record.approval_id).unwrap().status,
            "EXPIRED"
        );
        assert!(matches!(
            manager.decide(&ApprovalDecision {
                approval_id: record.approval_id.clone(),
                decision: "APPROVED".to_string(),
                approver: "operator".to_string(),
                timestamp: unix_now(),
            }),
            Err(ApprovalError::Expired(_))
        ));
        assert!(matches!(
            manager.claim(&record.approval_id, &req),
            Err(ApprovalError::Expired(_))
        ));
    }

    #[test]
    fn invalid_inputs_are_rejected() {
        let manager = ApprovalManager::new();
        let mut req = request(json!({}));
        req.execution_mode = "SHELL".to_string();
        assert!(matches!(
            manager.request(&req, None),
            Err(ApprovalError::Contract(ContractError::InvalidField { .. }))
        ));

        let req = request(json!({"n": 9_007_199_254_740_992u64}));
        assert!(matches!(
            manager.request(&req, None),
            Err(ApprovalError::Contract(ContractError::NonIjsonParams))
        ));

        let req = request(json!({}));
        let record = manager.request(&req, None).unwrap();
        assert!(manager
            .decide(&ApprovalDecision {
                approval_id: record.approval_id,
                decision: "MAYBE".to_string(),
                approver: "operator".to_string(),
                timestamp: unix_now(),
            })
            .is_err());
    }

    #[test]
    fn unknown_approval_is_rejected() {
        let manager = ApprovalManager::new();
        assert!(matches!(
            manager.status("missing"),
            Err(ApprovalError::NotFound(_))
        ));
        assert!(matches!(
            manager.claim("missing", &request(json!({}))),
            Err(ApprovalError::NotFound(_))
        ));
        assert_eq!(manager.expire_stale(), 0);
        assert_eq!(manager.len(), 0);
    }
}
