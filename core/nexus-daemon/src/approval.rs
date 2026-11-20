//! Approval Manager do Control Plane (T4/T5/T6).
//!
//! O T5 fechou a paridade de estado e ciclo de vida com o Python:
//! `PENDING → APPROVED|DENIED|EXPIRED`, `APPROVED → CLAIMED|REVOKED|EXPIRED`,
//! `CLAIMED → CONSUMED`, revogação, verificação de vínculo por hash, TTL por
//! solicitação e uso único.
//!
//! O T6 adiciona persistência durável em SQLite e o ciclo de execução:
//! `CLAIMED → CONSUMED` acontece no `ExecutionStart`, e `PREPARED → STARTING →
//! COMPLETED|FAILED|INTERRUPTED` fica registrado na tabela `executions`.
//! Reiniciar o daemon não invalida mais aprovações; a falha do daemon é tratada
//! pelo cliente Python como fail-closed.

use crate::approval_store::{ApprovalStore, ExecutionRecord};
use crate::contracts::{ApprovalDecision, ApprovalRecord, ApprovalRequest, ContractError};
use crate::control_schema::SchemaError;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_APPROVAL_TTL_SECONDS: f64 = 3600.0;
pub const DEFAULT_ORPHAN_TIMEOUT_SECONDS: f64 = 300.0;

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
    #[error("approval {0} não está em CLAIMED")]
    NotClaimed(String),
    #[error("approval {0} não pode ser revogada no estado atual")]
    NotRevocable(String),
    #[error("params divergem da aprovação vinculada")]
    HashMismatch,
    #[error("contrato: {0}")]
    Contract(#[from] ContractError),
    #[error("armazenamento de aprovação: {0}")]
    Storage(String),
}

impl From<rusqlite::Error> for ApprovalError {
    fn from(error: rusqlite::Error) -> Self {
        ApprovalError::Storage(error.to_string())
    }
}

impl From<SchemaError> for ApprovalError {
    fn from(error: SchemaError) -> Self {
        ApprovalError::Storage(error.to_string())
    }
}

/// Manager de aprovações persistente.
///
/// A conexão SQLite é serializada pelo `ApprovalStore`; `op_lock` garante que
/// leituras seguidas de transição (decidir, claim, consumo) não intercalem
/// entre threads do IPC. O lock de operações é sempre adquirido antes do lock
/// da conexão, evitando inversão de ordem.
pub struct ApprovalManager {
    store: Arc<ApprovalStore>,
    ttl_s: f64,
    op_lock: Mutex<()>,
}

impl ApprovalManager {
    pub fn new() -> Self {
        Self::with_ttl_secs(DEFAULT_APPROVAL_TTL_SECONDS)
    }

    pub fn with_ttl_secs(ttl_s: f64) -> Self {
        let store = ApprovalStore::open_in_memory().expect("abrir approval store em memória");
        Self::with_store(Arc::new(store), ttl_s)
    }

    pub fn open<P: AsRef<Path>>(path: P, ttl_s: f64) -> Result<Self, ApprovalError> {
        let store = ApprovalStore::open(path)?;
        Ok(Self::with_store(Arc::new(store), ttl_s))
    }

    pub fn with_store(store: Arc<ApprovalStore>, ttl_s: f64) -> Self {
        assert!(
            ttl_s.is_finite() && ttl_s >= 0.0,
            "ttl de aprovação deve ser finito e não negativo"
        );
        Self {
            store,
            ttl_s,
            op_lock: Mutex::new(()),
        }
    }

    fn lock_ops(&self) -> MutexGuard<'_, ()> {
        self.op_lock.lock().expect("lock de operações de approval")
    }

    /// Cria uma solicitação PENDING vinculada ao hash canônico da operação,
    /// usando o TTL padrão do manager.
    pub fn request(
        &self,
        request: &ApprovalRequest,
        risk_level: Option<&str>,
    ) -> Result<ApprovalRecord, ApprovalError> {
        self.request_with_ttl(request, risk_level, self.ttl_s)
    }

    /// Cria uma solicitação PENDING com TTL explícito por solicitação.
    ///
    /// Espelha `ApprovalManager.request_approval(req, ttl_s=...)` do Python.
    pub fn request_with_ttl(
        &self,
        request: &ApprovalRequest,
        risk_level: Option<&str>,
        ttl_s: f64,
    ) -> Result<ApprovalRecord, ApprovalError> {
        request.validate()?;
        if !ttl_s.is_finite() || ttl_s < 0.0 {
            return Err(ApprovalError::Invalid(format!("ttl inválido: {ttl_s}")));
        }
        if let Some(risk) = risk_level {
            if !matches!(risk, "LOW" | "MEDIUM" | "HIGH" | "CRITICAL") {
                return Err(ApprovalError::Invalid(format!(
                    "risk_level inválido: {risk}"
                )));
            }
        }
        let params_hash = request.params_hash()?;
        let now = unix_now();
        let record = ApprovalRecord {
            approval_id: uuid::Uuid::new_v4().simple().to_string(),
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
            expires_at: now + ttl_s,
            decided_at: None,
            decided_by: None,
            consumed_at: None,
            risk_level: risk_level.map(str::to_string),
        };
        let _guard = self.lock_ops();
        self.store.insert_approval(&record)?;
        Ok(record)
    }

    /// Aplica uma decisão humana. Apenas PENDING pode ser decidida.
    pub fn decide(&self, decision: &ApprovalDecision) -> Result<ApprovalRecord, ApprovalError> {
        decision.validate()?;
        let now = unix_now();
        let _guard = self.lock_ops();
        self.store.refresh_expiry(&decision.approval_id, now)?;
        let record = self
            .store
            .get_approval(&decision.approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(decision.approval_id.clone()))?;
        match record.status.as_str() {
            "PENDING" => {}
            "APPROVED" | "DENIED" => {
                return Err(ApprovalError::NotPending(decision.approval_id.clone()))
            }
            "EXPIRED" => return Err(ApprovalError::Expired(decision.approval_id.clone())),
            _ => return Err(ApprovalError::NotPending(decision.approval_id.clone())),
        }
        if !self.store.set_decision(
            &decision.approval_id,
            &decision.decision,
            decision.timestamp,
            &decision.approver,
        )? {
            return Err(ApprovalError::NotPending(decision.approval_id.clone()));
        }
        self.store
            .get_approval(&decision.approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(decision.approval_id.clone()))
    }

    /// Consulta o estado atual, expirando registros vencidos.
    pub fn status(&self, approval_id: &str) -> Result<ApprovalRecord, ApprovalError> {
        let now = unix_now();
        let _guard = self.lock_ops();
        self.store.refresh_expiry(approval_id, now)?;
        self.store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))
    }

    /// Verifica o vínculo e consome a aprovação para uma única autorização.
    ///
    /// O claim é atômico no SQLite: duas chamadas concorrentes não podem
    /// consumir a mesma aprovação. A transição para CONSUMED é feita por
    /// `start_execution` quando a execução começa.
    pub fn claim(
        &self,
        approval_id: &str,
        request: &ApprovalRequest,
    ) -> Result<ApprovalRecord, ApprovalError> {
        request.validate()?;
        let hash = request.params_hash()?;
        let now = unix_now();
        let _guard = self.lock_ops();
        self.store.refresh_expiry(approval_id, now)?;
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        match record.status.as_str() {
            "APPROVED" => {}
            "EXPIRED" => return Err(ApprovalError::Expired(approval_id.to_string())),
            "PENDING" | "DENIED" => {
                return Err(ApprovalError::NotApproved(approval_id.to_string()))
            }
            _ => return Err(ApprovalError::NotPending(approval_id.to_string())),
        }
        if record.params_hash != hash {
            return Err(ApprovalError::HashMismatch);
        }
        if !self.store.claim(approval_id, now)? {
            return Err(ApprovalError::NotPending(approval_id.to_string()));
        }
        self.store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))
    }

    /// Reivindica uma aprovação apenas pelo id, sem repetir o hash.
    ///
    /// Espelha `ApprovalManager.claim_approval` do Python. O chamador já fez a
    /// verificação de vínculo (`verify_action`) e a operação é local ao mesmo
    /// UID; o `Authorize` continua usando `claim` com hash.
    pub fn claim_by_id(&self, approval_id: &str) -> Result<ApprovalRecord, ApprovalError> {
        let now = unix_now();
        let _guard = self.lock_ops();
        self.store.refresh_expiry(approval_id, now)?;
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        match record.status.as_str() {
            "APPROVED" => {}
            "EXPIRED" => return Err(ApprovalError::Expired(approval_id.to_string())),
            "PENDING" | "DENIED" => {
                return Err(ApprovalError::NotApproved(approval_id.to_string()))
            }
            _ => return Err(ApprovalError::NotPending(approval_id.to_string())),
        }
        if !self.store.claim(approval_id, now)? {
            return Err(ApprovalError::NotPending(approval_id.to_string()));
        }
        self.store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))
    }

    /// Confere se o request ainda corresponde ao hash vinculado.
    ///
    /// Espelha `ApprovalManager.verify_action` do Python: não consome, não
    /// expira e não altera o estado; aprovação inexistente ou hash divergente
    /// devolvem `false`. Um request fora do contrato é erro (fail-closed).
    pub fn verify_action(
        &self,
        approval_id: &str,
        request: &ApprovalRequest,
    ) -> Result<bool, ApprovalError> {
        let current_hash = request.params_hash()?;
        let record = self.store.get_approval(approval_id)?;
        Ok(record.is_some_and(|stored| stored.params_hash == current_hash))
    }

    /// Transição `CLAIMED → CONSUMED` e início do ciclo de execução.
    ///
    /// Espelha `ApprovalManager.mark_starting` do Python: só uma aprovação
    /// previamente reivindicada pode ser consumida; a execução passa a
    /// `STARTING` na mesma transação SQLite.
    pub fn start_execution(
        &self,
        approval_id: &str,
    ) -> Result<(ApprovalRecord, ExecutionRecord), ApprovalError> {
        let now = unix_now();
        let _guard = self.lock_ops();
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        match record.status.as_str() {
            "CLAIMED" => {}
            "EXPIRED" => return Err(ApprovalError::Expired(approval_id.to_string())),
            _ => return Err(ApprovalError::NotClaimed(approval_id.to_string())),
        }
        if !self.store.start_execution(approval_id, now)? {
            return Err(ApprovalError::NotClaimed(approval_id.to_string()));
        }
        self.execution_pair(approval_id)
    }

    /// Mantido para paridade com o T5: consome e devolve o ApprovalRecord.
    pub fn mark_consumed(&self, approval_id: &str) -> Result<ApprovalRecord, ApprovalError> {
        self.start_execution(approval_id).map(|(record, _)| record)
    }

    /// Fecha a execução com `COMPLETED`, `FAILED` ou `INTERRUPTED`.
    ///
    /// Se a aprovação ainda estiver `CLAIMED` (chamador pulou o start), ela é
    /// consumida nesta operação para não deixar um claim órfão. O update é
    /// idempotente para repetição após perda de resposta do IPC.
    pub fn complete_execution(
        &self,
        approval_id: &str,
        status: &str,
        summary: Option<&str>,
    ) -> Result<(ApprovalRecord, ExecutionRecord), ApprovalError> {
        if !matches!(status, "COMPLETED" | "FAILED" | "INTERRUPTED") {
            return Err(ApprovalError::Invalid(format!(
                "status de execução inválido: {status}"
            )));
        }
        let now = unix_now();
        let _guard = self.lock_ops();
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        match record.status.as_str() {
            "CLAIMED" | "CONSUMED" => {}
            "EXPIRED" => return Err(ApprovalError::Expired(approval_id.to_string())),
            "PENDING" | "DENIED" => {
                return Err(ApprovalError::NotApproved(approval_id.to_string()))
            }
            _ => return Err(ApprovalError::NotClaimed(approval_id.to_string())),
        }
        self.store
            .complete_execution(approval_id, status, summary, now)?;
        self.execution_pair(approval_id)
    }

    /// Consulta aprovação + execução vinculada.
    pub fn execution_status(
        &self,
        approval_id: &str,
    ) -> Result<(ApprovalRecord, ExecutionRecord), ApprovalError> {
        let _guard = self.lock_ops();
        self.execution_pair(approval_id)
    }

    fn execution_pair(
        &self,
        approval_id: &str,
    ) -> Result<(ApprovalRecord, ExecutionRecord), ApprovalError> {
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        let execution = self
            .store
            .get_execution(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        Ok((record, execution))
    }

    /// Revoga uma aprovação `PENDING` ou `APPROVED`.
    ///
    /// Espelha `ApprovalManager.revoke` do Python: estados terminais
    /// (`CLAIMED`, `CONSUMED`, `DENIED`, `EXPIRED`, `REVOKED`) não são
    /// revogáveis. A checagem usa o estado armazenado, sem expirar antes,
    /// exatamente como no Python.
    pub fn revoke(&self, approval_id: &str) -> Result<ApprovalRecord, ApprovalError> {
        let _guard = self.lock_ops();
        let record = self
            .store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))?;
        if !matches!(record.status.as_str(), "PENDING" | "APPROVED") {
            return Err(ApprovalError::NotRevocable(approval_id.to_string()));
        }
        if !self.store.revoke(approval_id)? {
            return Err(ApprovalError::NotRevocable(approval_id.to_string()));
        }
        self.store
            .get_approval(approval_id)?
            .ok_or_else(|| ApprovalError::NotFound(approval_id.to_string()))
    }

    /// Expira solicitações pendentes/aprovadas vencidas e devolve a contagem.
    pub fn expire_stale(&self) -> usize {
        let now = unix_now();
        let _guard = self.lock_ops();
        self.store.expire_stale(now).unwrap_or(0)
    }

    /// Lista aprovações com filtro e paginação; expira PENDING antes.
    pub fn list(
        &self,
        status: Option<&str>,
        limit: u32,
        offset: u32,
    ) -> Result<Vec<ApprovalRecord>, ApprovalError> {
        let _guard = self.lock_ops();
        if status == Some("PENDING") {
            self.store.expire_stale(unix_now())?;
        }
        Ok(self.store.list_approvals(status, limit, offset)?)
    }

    /// Marca execuções antigas como `UNKNOWN` após reinício do daemon.
    pub fn recover_orphans(&self, timeout_s: f64) -> usize {
        let _guard = self.lock_ops();
        self.store
            .recover_orphans(unix_now(), timeout_s)
            .unwrap_or(0)
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.store.count().unwrap_or(0)
    }

    #[cfg(test)]
    pub fn force_expires_at(&self, approval_id: &str, expires_at: f64) {
        self.store
            .set_expires_at(approval_id, expires_at)
            .expect("forçar expiração");
    }
}

impl Default for ApprovalManager {
    fn default() -> Self {
        Self::new()
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
    fn claimed_approval_is_consumed_once() {
        let manager = ApprovalManager::new();
        let req = request(json!({"args": []}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "APPROVED");

        let claimed = manager.claim(&record.approval_id, &req).expect("claim");
        assert_eq!(claimed.status, "CLAIMED");
        assert!(claimed.consumed_at.is_none());

        let consumed = manager.mark_consumed(&record.approval_id).expect("consume");
        assert_eq!(consumed.status, "CONSUMED");
        assert!(consumed.consumed_at.is_some());

        assert!(matches!(
            manager.mark_consumed(&record.approval_id),
            Err(ApprovalError::NotClaimed(_))
        ));
        assert!(matches!(
            manager.claim(&record.approval_id, &req),
            Err(ApprovalError::NotPending(_))
        ));
        assert!(matches!(
            manager.revoke(&record.approval_id),
            Err(ApprovalError::NotRevocable(_))
        ));
    }

    #[test]
    fn revoke_accepts_pending_and_approved_only() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));

        let pending = manager.request(&req, Some("HIGH")).unwrap();
        let revoked = manager.revoke(&pending.approval_id).expect("revoke");
        assert_eq!(revoked.status, "REVOKED");
        assert!(matches!(
            manager.decide(&ApprovalDecision {
                approval_id: pending.approval_id.clone(),
                decision: "APPROVED".to_string(),
                approver: "operator".to_string(),
                timestamp: unix_now(),
            }),
            Err(ApprovalError::NotPending(_))
        ));
        assert!(matches!(
            manager.revoke(&pending.approval_id),
            Err(ApprovalError::NotRevocable(_))
        ));

        let approved = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &approved.approval_id, "APPROVED");
        assert_eq!(
            manager.revoke(&approved.approval_id).unwrap().status,
            "REVOKED"
        );
        assert!(matches!(
            manager.claim(&approved.approval_id, &req),
            Err(ApprovalError::NotPending(_))
        ));

        let denied = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &denied.approval_id, "DENIED");
        assert!(matches!(
            manager.revoke(&denied.approval_id),
            Err(ApprovalError::NotRevocable(_))
        ));
    }

    #[test]
    fn verify_action_checks_hash_without_mutating_state() {
        let manager = ApprovalManager::new();
        let req = request(json!({"args": ["--safe"]}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "APPROVED");

        assert!(manager
            .verify_action(&record.approval_id, &req)
            .expect("verify"));
        let altered = request(json!({"args": ["--danger"]}));
        assert!(!manager
            .verify_action(&record.approval_id, &altered)
            .expect("verify divergente"));
        assert!(!manager
            .verify_action("missing", &req)
            .expect("verify inexistente"));
        assert_eq!(
            manager.status(&record.approval_id).unwrap().status,
            "APPROVED"
        );
    }

    #[test]
    fn ttl_is_configurable_per_request() {
        let manager = ApprovalManager::with_ttl_secs(3600.0);
        let req = request(json!({}));
        let short = manager
            .request_with_ttl(&req, Some("HIGH"), 0.0)
            .expect("ttl curto");
        let long = manager.request(&req, Some("HIGH")).expect("ttl padrão");
        assert!(short.expires_at <= long.created_at + 0.001);
        assert!(long.expires_at > long.created_at);
        assert_eq!(
            manager.status(&short.approval_id).unwrap().status,
            "EXPIRED"
        );
        assert_eq!(manager.status(&long.approval_id).unwrap().status, "PENDING");

        assert!(matches!(
            manager.request_with_ttl(&req, None, -1.0),
            Err(ApprovalError::Invalid(_))
        ));
        assert!(matches!(
            manager.request_with_ttl(&req, None, f64::NAN),
            Err(ApprovalError::Invalid(_))
        ));
    }

    #[test]
    fn expire_stale_does_not_touch_terminal_states() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));
        let revoked = manager.request(&req, Some("HIGH")).unwrap();
        manager.revoke(&revoked.approval_id).unwrap();
        let denied = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &denied.approval_id, "DENIED");
        manager.force_expires_at(&revoked.approval_id, 0.0);
        manager.force_expires_at(&denied.approval_id, 0.0);

        assert_eq!(manager.expire_stale(), 0);
        assert_eq!(
            manager.status(&revoked.approval_id).unwrap().status,
            "REVOKED"
        );
        assert_eq!(
            manager.status(&denied.approval_id).unwrap().status,
            "DENIED"
        );
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

    #[test]
    fn persistent_store_survives_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("approvals.db");
        let approval_id;
        {
            let manager = ApprovalManager::open(&path, DEFAULT_APPROVAL_TTL_SECONDS).unwrap();
            let req = request(json!({"args": ["--safe"]}));
            let record = manager.request(&req, Some("HIGH")).unwrap();
            approval_id = record.approval_id.clone();
            decide(&manager, &approval_id, "APPROVED");
            manager.claim(&approval_id, &req).unwrap();
            manager.mark_consumed(&approval_id).unwrap();
            let (approval, execution) = manager.execution_status(&approval_id).unwrap();
            assert_eq!(approval.status, "CONSUMED");
            assert_eq!(execution.status, "STARTING");
        }

        let manager = ApprovalManager::open(&path, DEFAULT_APPROVAL_TTL_SECONDS).unwrap();
        let record = manager.status(&approval_id).unwrap();
        assert_eq!(record.status, "CONSUMED");
        assert!(record.consumed_at.is_some());
        let history = manager.list(Some("CONSUMED"), 10, 0).unwrap();
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].approval_id, approval_id);
    }

    #[test]
    fn execution_lifecycle_is_persisted_and_completion_is_idempotent() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "APPROVED");
        manager.claim(&record.approval_id, &req).unwrap();

        let (approval, execution) = manager.execution_status(&record.approval_id).unwrap();
        assert_eq!(approval.status, "CLAIMED");
        assert_eq!(execution.status, "PREPARED");
        assert!(execution.started_at.is_some());

        let (approval, execution) = manager.start_execution(&record.approval_id).unwrap();
        assert_eq!(approval.status, "CONSUMED");
        assert_eq!(execution.status, "STARTING");
        assert!(approval.consumed_at.is_some());

        let (approval, execution) = manager
            .complete_execution(&record.approval_id, "COMPLETED", Some("ok"))
            .unwrap();
        assert_eq!(approval.status, "CONSUMED");
        assert_eq!(execution.status, "COMPLETED");
        assert_eq!(execution.result_summary.as_deref(), Some("ok"));
        assert!(execution.completed_at.is_some());

        let (_, repeated) = manager
            .complete_execution(&record.approval_id, "COMPLETED", Some("ok"))
            .unwrap();
        assert_eq!(repeated.status, "COMPLETED");
    }

    #[test]
    fn recover_orphans_marks_old_executions_unknown() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        decide(&manager, &record.approval_id, "APPROVED");
        manager.claim(&record.approval_id, &req).unwrap();

        assert_eq!(manager.recover_orphans(0.0), 1);
        let (approval, execution) = manager.execution_status(&record.approval_id).unwrap();
        assert_eq!(approval.status, "CLAIMED");
        assert_eq!(execution.status, "UNKNOWN");
        assert_eq!(manager.recover_orphans(0.0), 0);
    }

    #[test]
    fn execution_completion_rejects_unclaimed_approval() {
        let manager = ApprovalManager::new();
        let req = request(json!({}));
        let record = manager.request(&req, Some("HIGH")).unwrap();
        assert!(matches!(
            manager.complete_execution(&record.approval_id, "COMPLETED", None),
            Err(ApprovalError::NotApproved(_))
        ));
        assert!(matches!(
            manager.complete_execution(&record.approval_id, "RUNNING", None),
            Err(ApprovalError::Invalid(_))
        ));
    }

    fn lifecycle_fixture() -> serde_json::Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("contracts/fixtures/parity/approval_lifecycle.json");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("falha ao ler {}: {error}", path.display()));
        serde_json::from_str(&raw).expect("approval lifecycle fixture")
    }

    #[test]
    fn shared_lifecycle_fixture_matches_python() {
        let fixture = lifecycle_fixture();
        assert_eq!(fixture["contract_version"], "1.0.0");
        let original: ApprovalRequest =
            serde_json::from_value(fixture["request"].clone()).expect("request");
        let variant: ApprovalRequest =
            serde_json::from_value(fixture["variant_request"].clone()).expect("variant");
        let expected_hash = fixture["expected_hash"].as_str().expect("hash");
        assert_eq!(original.params_hash().unwrap(), expected_hash);
        let default_ttl = fixture["default_ttl_seconds"]
            .as_f64()
            .expect("default_ttl_seconds");

        for scenario in fixture["scenarios"].as_array().expect("scenarios") {
            let name = scenario["name"].as_str().expect("nome");
            let manager = ApprovalManager::new();
            let mut approval_id = String::new();
            for step in scenario["steps"].as_array().expect("steps") {
                let op = step["op"].as_str().expect("op");
                let expect_status = step["expect_status"].as_str();
                let ok = match op {
                    "create" => {
                        let ttl = step["ttl_seconds"].as_f64().unwrap_or(default_ttl);
                        let record = manager
                            .request_with_ttl(&original, None, ttl)
                            .unwrap_or_else(|error| panic!("{name}: create falhou: {error}"));
                        assert_eq!(record.params_hash, expected_hash, "{name}");
                        assert_eq!(
                            record.status,
                            expect_status.expect("create expect_status"),
                            "{name}"
                        );
                        approval_id = record.approval_id;
                        continue;
                    }
                    "verify_action" => {
                        let request = if step["params"].as_str() == Some("variant") {
                            &variant
                        } else {
                            &original
                        };
                        let result = manager
                            .verify_action(&approval_id, request)
                            .expect("verify_action");
                        assert_eq!(
                            result,
                            step["expect"].as_bool().expect("expect"),
                            "{name}: step {step}"
                        );
                        continue;
                    }
                    "expire_stale" => {
                        assert_eq!(
                            manager.expire_stale(),
                            step["expect_expired"].as_u64().expect("expect_expired") as usize,
                            "{name}"
                        );
                        continue;
                    }
                    "status" => true,
                    "decide" => manager
                        .decide(&ApprovalDecision {
                            approval_id: approval_id.clone(),
                            decision: step["decision"].as_str().expect("decision").to_string(),
                            approver: "operator".to_string(),
                            timestamp: unix_now(),
                        })
                        .is_ok(),
                    "claim" => {
                        let request = if step["params"].as_str() == Some("variant") {
                            &variant
                        } else {
                            &original
                        };
                        manager.claim(&approval_id, request).is_ok()
                    }
                    "consume" => manager.mark_consumed(&approval_id).is_ok(),
                    "revoke" => manager.revoke(&approval_id).is_ok(),
                    other => panic!("{name}: op desconhecida: {other}"),
                };
                if let Some(expected) = step["expect_ok"].as_bool() {
                    assert_eq!(ok, expected, "{name}: step {step}");
                }
                if let Some(status) = expect_status {
                    assert_eq!(
                        manager.status(&approval_id).unwrap().status,
                        status,
                        "{name}: step {step}"
                    );
                }
            }
        }
    }
}
