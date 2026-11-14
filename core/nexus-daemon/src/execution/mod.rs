use serde::Serialize;
use serde_json::{json, Value};

use crate::approval::ApprovalManager;
use crate::contracts::{AuthorizationRequest, AuthorizationResult};
use crate::policy::{PolicyContext, PolicyDecision, PolicyEngine};

/// Execution Gate do Control Plane.
///
/// O T4 expõe a decisão de autorização pelo IPC, mas a autoridade final ainda
/// é o runtime Python até o T5/T6. O gate é determinístico, independente de
/// LLM e fail-closed para risco desconhecido, crítica e aprovação inválida.
pub struct ExecutionGate {
    policy: PolicyEngine,
}

impl ExecutionGate {
    pub fn new() -> Self {
        Self {
            policy: PolicyEngine::new(),
        }
    }

    /// Avalia uma operação simples (mantido para compatibilidade e testes).
    pub fn authorize(&self, component_id: &str, operation: &str, risk_level: &str) -> GateResult {
        let eval = self.policy.evaluate(&PolicyContext {
            component_id: component_id.to_string(),
            operation: operation.to_string(),
            risk_level: risk_level.to_string(),
        });
        GateResult {
            decision: eval.decision,
            reason: eval.reason,
            authorized: matches!(eval.decision, crate::policy::PolicyDecision::Allow),
        }
    }

    /// Executa o contrato `authorize` do IPC e devolve o resultado v1.0.0.
    ///
    /// A aprovação, quando apresentada, é validada contra o hash canônico da
    /// operação e consumida atomicamente para impedir replay. Aprovações
    /// pendentes são criadas apenas quando a política exige decisão humana.
    pub fn authorize_request(
        &self,
        request: &AuthorizationRequest,
        approvals: &ApprovalManager,
    ) -> AuthorizationResult {
        let request_id = uuid::Uuid::new_v4().simple().to_string();
        if let Err(error) = request.validate() {
            return denied(
                request_id,
                format!("invalid authorization request: {error}"),
            );
        }

        let evaluation = self.policy.evaluate_authorization(
            &request.risk_level,
            &request.access_level,
            &request.fs_scope,
            request.autonomy_level,
            request.required_autonomy,
        );

        if matches!(evaluation.decision, PolicyDecision::Deny) {
            return denied(request_id, evaluation.reason);
        }

        let approval_request = request.approval_request();
        if let Some(approval_id) = &request.approval_id {
            return match approvals.claim(approval_id, &approval_request) {
                Ok(record) => allowed(
                    request_id,
                    evaluation.reason,
                    json!({
                        "approval_id": record.approval_id,
                        "approval_status": record.status,
                        "risk_level": request.risk_level,
                    }),
                ),
                Err(error) => denied(request_id, format!("approval verification failed: {error}")),
            };
        }

        if matches!(evaluation.decision, PolicyDecision::RequireApproval) {
            return match approvals.request(&approval_request, Some(&request.risk_level)) {
                Ok(record) => AuthorizationResult {
                    decision: "REQUIRES_APPROVAL".to_string(),
                    request_id,
                    reason: evaluation.reason,
                    approved: false,
                    execution_id: None,
                    metadata: json!({
                        "approval_id": record.approval_id,
                        "requires_human": true,
                        "risk_level": record.risk_level,
                    }),
                },
                Err(error) => denied(request_id, format!("approval request failed: {error}")),
            };
        }

        allowed(request_id, evaluation.reason, json!({}))
    }
}

fn allowed(request_id: String, reason: String, metadata: Value) -> AuthorizationResult {
    AuthorizationResult {
        decision: "ALLOWED".to_string(),
        execution_id: Some(request_id.clone()),
        request_id,
        reason,
        approved: true,
        metadata,
    }
}

fn denied(request_id: String, reason: String) -> AuthorizationResult {
    AuthorizationResult {
        decision: "DENIED".to_string(),
        request_id,
        reason,
        approved: false,
        execution_id: None,
        metadata: json!({}),
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct GateResult {
    pub decision: crate::policy::PolicyDecision,
    pub reason: String,
    pub authorized: bool,
}

impl Default for ExecutionGate {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contracts::ApprovalDecision;
    use serde_json::json;

    fn request(risk_level: &str) -> AuthorizationRequest {
        AuthorizationRequest {
            component_id: "python-runtime".to_string(),
            tool_id: "sys.uptime".to_string(),
            params: json!({"args": []}),
            task_id: "task-1".to_string(),
            plan_id: "plan-1".to_string(),
            risk_level: risk_level.to_string(),
            autonomy_level: 1,
            required_autonomy: 1,
            approval_id: None,
            execution_mode: "HOST".to_string(),
            access_level: "READ".to_string(),
            policy_version: "1.0".to_string(),
            required_capability: None,
            fs_scope: "DEFAULT".to_string(),
            net_scope: "NONE".to_string(),
            resource_limits: "default".to_string(),
            justification: "test".to_string(),
        }
    }

    fn now() -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
    }

    #[test]
    fn low_risk_authorized_without_approval() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let result = gate.authorize_request(&request("LOW"), &approvals);
        assert_eq!(result.decision, "ALLOWED");
        assert!(result.approved);
        assert_eq!(
            result.execution_id.as_deref(),
            Some(result.request_id.as_str())
        );
        assert_eq!(approvals.len(), 0);
    }

    #[test]
    fn high_risk_requires_approval_and_consumes_it_once() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let mut request = request("HIGH");

        let pending = gate.authorize_request(&request, &approvals);
        assert_eq!(pending.decision, "REQUIRES_APPROVAL");
        assert!(!pending.approved);
        assert!(pending.execution_id.is_none());
        let approval_id = pending.metadata["approval_id"]
            .as_str()
            .expect("approval_id")
            .to_string();

        let decided = approvals
            .decide(&ApprovalDecision {
                approval_id: approval_id.clone(),
                decision: "APPROVED".to_string(),
                approver: "operator".to_string(),
                timestamp: now(),
            })
            .unwrap();
        assert_eq!(decided.status, "APPROVED");

        request.approval_id = Some(approval_id);
        let allowed = gate.authorize_request(&request, &approvals);
        assert_eq!(allowed.decision, "ALLOWED");
        assert_eq!(allowed.metadata["approval_status"], "CLAIMED");

        let replay = gate.authorize_request(&request, &approvals);
        assert_eq!(replay.decision, "DENIED");
        assert!(replay.reason.contains("approval verification failed"));
    }

    #[test]
    fn approval_is_bound_to_exact_params() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let request = request("HIGH");
        let pending = gate.authorize_request(&request, &approvals);
        let approval_id = pending.metadata["approval_id"]
            .as_str()
            .unwrap()
            .to_string();
        approvals
            .decide(&ApprovalDecision {
                approval_id: approval_id.clone(),
                decision: "APPROVED".to_string(),
                approver: "operator".to_string(),
                timestamp: now(),
            })
            .unwrap();

        let mut altered = request;
        altered.params = json!({"args": ["--different"]});
        altered.approval_id = Some(approval_id);
        let result = gate.authorize_request(&altered, &approvals);
        assert_eq!(result.decision, "DENIED");
        assert!(result.reason.contains("params divergem"));
        assert_eq!(
            approvals
                .status(altered.approval_id.as_deref().unwrap())
                .unwrap()
                .status,
            "APPROVED"
        );
    }

    #[test]
    fn denied_and_critical_never_create_or_consume_approval() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let critical = gate.authorize_request(&request("CRITICAL"), &approvals);
        assert_eq!(critical.decision, "DENIED");
        assert_eq!(approvals.len(), 0);

        let mut denied = request("HIGH");
        denied.approval_id = Some("missing".to_string());
        let result = gate.authorize_request(&denied, &approvals);
        assert_eq!(result.decision, "DENIED");
    }

    #[test]
    fn write_outside_safe_zone_and_low_autonomy_require_approval() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let mut write = request("LOW");
        write.access_level = "WRITE".to_string();
        write.fs_scope = "DEFAULT".to_string();
        assert_eq!(
            gate.authorize_request(&write, &approvals).decision,
            "REQUIRES_APPROVAL"
        );

        let mut safe = request("LOW");
        safe.access_level = "WRITE".to_string();
        safe.fs_scope = "SAFE_ZONE".to_string();
        assert_eq!(
            gate.authorize_request(&safe, &approvals).decision,
            "ALLOWED"
        );

        let mut autonomy = request("LOW");
        autonomy.autonomy_level = 0;
        autonomy.required_autonomy = 2;
        assert_eq!(
            gate.authorize_request(&autonomy, &approvals).decision,
            "REQUIRES_APPROVAL"
        );
    }

    #[test]
    fn malformed_contract_is_denied_before_policy() {
        let gate = ExecutionGate::new();
        let approvals = ApprovalManager::new();
        let mut invalid = request("LOW");
        invalid.execution_mode = "SHELL".to_string();
        let result = gate.authorize_request(&invalid, &approvals);
        assert_eq!(result.decision, "DENIED");
        assert!(result.reason.contains("invalid authorization request"));
        assert_eq!(approvals.len(), 0);
    }

    #[test]
    fn approval_request_projection_is_stable() {
        let request = request("HIGH");
        let projection = request.approval_request();
        assert_eq!(projection.tool_id, request.tool_id);
        assert_eq!(projection.params, request.params);
        assert_eq!(projection.task_id, request.task_id);
        assert!(projection.params_hash().is_ok());
        // O campo approval_id não entra no hash de vínculo.
        let mut with_id = request;
        with_id.approval_id = Some("abc".to_string());
        assert_eq!(
            projection.params_hash().unwrap(),
            with_id.approval_request().params_hash().unwrap()
        );
    }
}
