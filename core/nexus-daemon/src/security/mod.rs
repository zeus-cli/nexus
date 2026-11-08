use serde::Serialize;

/// Identidade de um componente local que se conecta ao daemon via IPC.
#[derive(Debug, Clone, Serialize)]
pub struct ComponentIdentity {
    pub component_id: String,
    pub kind: ComponentKind,
    pub trusted: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ComponentKind {
    Daemon,
    PythonRuntime,
    Sentinel,
    Cli,
    Tui,
    WebHud,
    Unknown,
}

/// Guard de segurança da Fase 1: autenticação de componentes e lista de confiáveis.
///
/// Fase 3 substitui isso pelo Execution Gate completo (identidade → capacidade →
/// política → risco → aprovação → recurso → execução isolada → verificação → commit → auditoria).
pub struct SecurityGuard {
    trusted_components: std::collections::HashSet<String>,
}

impl SecurityGuard {
    pub fn new() -> Self {
        Self {
            trusted_components: std::collections::HashSet::new(),
        }
    }

    pub fn register_trusted(&mut self, component_id: &str) {
        self.trusted_components.insert(component_id.to_string());
    }

    pub fn is_trusted(&self, component_id: &str) -> bool {
        self.trusted_components.contains(component_id)
    }

    pub fn identity(&self, component_id: &str, kind: ComponentKind) -> ComponentIdentity {
        ComponentIdentity {
            component_id: component_id.to_string(),
            kind,
            trusted: self.is_trusted(component_id),
        }
    }
}

impl Default for SecurityGuard {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trusted_component_recognized() {
        let mut guard = SecurityGuard::new();
        assert!(!guard.is_trusted("python-runtime"));
        guard.register_trusted("python-runtime");
        assert!(guard.is_trusted("python-runtime"));
        let ident = guard.identity("python-runtime", ComponentKind::PythonRuntime);
        assert!(ident.trusted);
    }
}
