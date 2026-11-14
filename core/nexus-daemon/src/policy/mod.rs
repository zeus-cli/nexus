//! Policy Engine determinístico do Control Plane.
//!
//! `evaluate_tool` espelha `PolicyEngine.evaluate`; `evaluate_authorization`
//! espelha `ExecutionGate._classify_risk`. As duas regras divergem de forma
//! normativa em escrita `LOW`/`SAFE_ZONE` sem autonomia e são cobertas pelo
//! fixture compartilhado em `contracts/fixtures/parity/policy_cases.json`.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyDecision {
    Allow,
    RequireApproval,
    Deny,
}

impl PolicyDecision {
    /// Nome usado pelo contrato de paridade (`ALLOW`, `REQUIRE_APPROVAL`,
    /// `DENY`), idêntico ao `PolicyDecision.name` do Python.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "ALLOW",
            Self::RequireApproval => "REQUIRE_APPROVAL",
            Self::Deny => "DENY",
        }
    }

    /// Nome usado pelo `GateDecision` do Python.
    pub fn as_gate_str(self) -> &'static str {
        match self {
            Self::Allow => "ALLOWED",
            Self::RequireApproval => "REQUIRES_APPROVAL",
            Self::Deny => "DENIED",
        }
    }
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
            "critical" => (
                PolicyDecision::Deny,
                "critical operations denied by default policy".to_string(),
            ),
            "high" => (
                PolicyDecision::RequireApproval,
                "high risk operations require explicit human approval".to_string(),
            ),
            "medium" => (
                PolicyDecision::RequireApproval,
                "medium risk operations require human verification".to_string(),
            ),
            "low" => (
                PolicyDecision::Allow,
                "low risk operation permitted".to_string(),
            ),
            _ => (
                PolicyDecision::Deny,
                "no matching policy rule found; denying by default".to_string(),
            ),
        };
        PolicyEvaluation { decision, reason }
    }

    /// Classificação determinística usada pelo Execution Gate do IPC.
    ///
    /// Espelha `ExecutionGate._classify_risk` do Python, inclusive a exigência
    /// de autonomia para escrita em `SAFE_ZONE` (o `PolicyEngine` puro do
    /// Python permite escrita em `SAFE_ZONE` sem exigir autonomia; são regras
    /// distintas e ambas têm teste de paridade). Risco desconhecido é negado.
    pub fn evaluate_authorization(
        &self,
        risk_level: &str,
        access_level: &str,
        fs_scope: &str,
        autonomy_level: u8,
        required_autonomy: u8,
    ) -> PolicyEvaluation {
        let (decision, reason) = match risk_level {
            "CRITICAL" => (
                PolicyDecision::Deny,
                "critical operations denied by default policy",
            ),
            "HIGH" => (
                PolicyDecision::RequireApproval,
                "high risk operations require explicit human approval",
            ),
            "MEDIUM" => (
                PolicyDecision::RequireApproval,
                "medium risk operations require human verification",
            ),
            "LOW" => {
                if access_level == "WRITE" && fs_scope != "SAFE_ZONE" {
                    (
                        PolicyDecision::RequireApproval,
                        "low risk write outside safe zone requires approval",
                    )
                } else if autonomy_level >= required_autonomy {
                    (
                        PolicyDecision::Allow,
                        "low risk operation permitted within approved scope",
                    )
                } else {
                    (
                        PolicyDecision::RequireApproval,
                        "insufficient autonomy for low-risk operation",
                    )
                }
            }
            _ => (
                PolicyDecision::Deny,
                "unknown risk level; denying by default",
            ),
        };
        PolicyEvaluation {
            decision,
            reason: reason.to_string(),
        }
    }

    /// Paridade de decisão com `PolicyEngine.evaluate` do Python.
    ///
    /// Diferencia-se de `evaluate_authorization` em um ponto normativo:
    /// escrita LOW em `SAFE_ZONE` é permitida sem exigir autonomia, exatamente
    /// como no Policy Engine Python. O Execution Gate Python usa uma regra
    /// adicional (autonomia) e por isso tem seu próprio método acima.
    pub fn evaluate_tool(
        &self,
        risk_level: &str,
        access_level: &str,
        fs_scope: &str,
        autonomy_level: u8,
        required_autonomy: u8,
    ) -> PolicyEvaluation {
        let (decision, reason) = match risk_level {
            "CRITICAL" => (
                PolicyDecision::Deny,
                "Critical tools are denied by default policy.",
            ),
            "HIGH" => (
                PolicyDecision::RequireApproval,
                "High risk operations require explicit human approval.",
            ),
            "MEDIUM" => (
                PolicyDecision::RequireApproval,
                "Medium risk operations require human verification.",
            ),
            "LOW" => match access_level {
                "READ" => {
                    if autonomy_level >= required_autonomy {
                        (PolicyDecision::Allow, "Low risk read operation permitted.")
                    } else {
                        (
                            PolicyDecision::RequireApproval,
                            "Insufficient autonomy for low-risk read.",
                        )
                    }
                }
                "WRITE" => {
                    if fs_scope == "SAFE_ZONE" {
                        (
                            PolicyDecision::Allow,
                            "Low risk write in safe zone permitted.",
                        )
                    } else {
                        (
                            PolicyDecision::RequireApproval,
                            "Low risk write outside safe zone requires approval.",
                        )
                    }
                }
                _ => (
                    PolicyDecision::Deny,
                    "No matching policy rule found. Denying by default.",
                ),
            },
            _ => (
                PolicyDecision::Deny,
                "No matching policy rule found. Denying by default.",
            ),
        };
        PolicyEvaluation {
            decision,
            reason: reason.to_string(),
        }
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

    fn parity_cases() -> serde_json::Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("contracts/fixtures/parity/policy_cases.json");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("falha ao ler {}: {error}", path.display()));
        serde_json::from_str(&raw).expect("parity fixture JSON")
    }

    #[test]
    fn shared_policy_cases_match_python_policy_engine() {
        let fixture = parity_cases();
        assert_eq!(fixture["contract_version"], "1.0.0");
        let engine = PolicyEngine::new();
        for case in fixture["cases"].as_array().expect("cases") {
            let decision = engine.evaluate_tool(
                case["risk_level"].as_str().expect("risk"),
                case["access_level"].as_str().expect("access"),
                case["fs_scope"].as_str().expect("scope"),
                case["autonomy_level"].as_u64().expect("autonomy") as u8,
                case["required_autonomy"].as_u64().expect("required") as u8,
            );
            assert_eq!(
                decision.decision.as_str(),
                case["policy_decision"].as_str().expect("policy_decision"),
                "caso {}",
                case["name"]
            );
        }
    }

    #[test]
    fn shared_gate_cases_match_python_execution_gate() {
        let fixture = parity_cases();
        let engine = PolicyEngine::new();
        for case in fixture["cases"].as_array().expect("cases") {
            let decision = engine.evaluate_authorization(
                case["risk_level"].as_str().expect("risk"),
                case["access_level"].as_str().expect("access"),
                case["fs_scope"].as_str().expect("scope"),
                case["autonomy_level"].as_u64().expect("autonomy") as u8,
                case["required_autonomy"].as_u64().expect("required") as u8,
            );
            assert_eq!(
                decision.decision.as_gate_str(),
                case["gate_decision"].as_str().expect("gate_decision"),
                "caso {}",
                case["name"]
            );
        }
    }

    #[test]
    fn policy_and_gate_differ_only_on_low_safe_zone_autonomy() {
        let engine = PolicyEngine::new();
        let policy = engine.evaluate_tool("LOW", "WRITE", "SAFE_ZONE", 0, 2);
        let gate = engine.evaluate_authorization("LOW", "WRITE", "SAFE_ZONE", 0, 2);
        assert_eq!(policy.decision, PolicyDecision::Allow);
        assert_eq!(gate.decision, PolicyDecision::RequireApproval);
    }
}
