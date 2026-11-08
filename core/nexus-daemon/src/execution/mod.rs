use serde::Serialize;

use crate::policy::{PolicyContext, PolicyEngine};

/// Execution Gate da Fase 1: valida se uma operação pode ser executada.
///
/// Fase 1 apenas aplica a decisão da PolicyEngine. Fase 3 adiciona o ciclo
/// completo: identidade → capacidade → política → risco → aprovação → recurso →
/// execução isolada → verificação → commit → auditoria.
pub struct ExecutionGate {
    policy: PolicyEngine,
}

impl ExecutionGate {
    pub fn new() -> Self {
        Self {
            policy: PolicyEngine::new(),
        }
    }

    /// Avalia se a operação é permitida. Retorna a decisão e justificativa.
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

    #[test]
    fn low_risk_authorized() {
        let gate = ExecutionGate::new();
        let res = gate.authorize("python-runtime", "read_memory", "low");
        assert!(res.authorized);
    }

    #[test]
    fn critical_not_authorized() {
        let gate = ExecutionGate::new();
        let res = gate.authorize("python-runtime", "rm_rf", "critical");
        assert!(!res.authorized);
    }
}
