use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyDecision {
    Allow,
    RequireApproval,
    Deny,
}

#[derive(Debug, Clone, Serialize)]
pub struct PolicyContext {
    pub component_id: String,
    pub operation: String,
    pub risk_level: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PolicyEvaluation {
    pub decision: PolicyDecision,
    pub reason: String,
}

/// Policy Engine da Fase 1: decisão determinística, independente de LLM.
///
/// Aplica regras de risco estritas. Fase 3 expande para regras configuráveis.
pub struct PolicyEngine;

impl PolicyEngine {
    pub fn new() -> Self {
        Self
    }

    pub fn evaluate(&self, ctx: &PolicyContext) -> PolicyEvaluation {
        let (decision, reason) = match ctx.risk_level.as_str() {
            "critical" => (PolicyDecision::Deny, "critical operations denied by default policy".to_string()),
            "high" => (PolicyDecision::RequireApproval, "high risk operations require explicit human approval".to_string()),
            "medium" => (PolicyDecision::RequireApproval, "medium risk operations require human verification".to_string()),
            "low" => (PolicyDecision::Allow, "low risk operation permitted".to_string()),
            _ => (PolicyDecision::Deny, "no matching policy rule found; denying by default".to_string()),
        };
        PolicyEvaluation { decision, reason }
    }
}

impl Default for PolicyEngine {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn low_risk_allowed() {
        let pe = PolicyEngine;
        let ev = pe.evaluate(&PolicyContext {
            component_id: "test".into(),
            operation: "read".into(),
            risk_level: "low".into(),
        });
        assert_eq!(ev.decision, PolicyDecision::Allow);
    }

    #[test]
    fn critical_denied() {
        let pe = PolicyEngine;
        let ev = pe.evaluate(&PolicyContext {
            component_id: "test".into(),
            operation: "write".into(),
            risk_level: "critical".into(),
        });
        assert_eq!(ev.decision, PolicyDecision::Deny);
    }
}
