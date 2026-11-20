//! Catálogo de escopos e papéis embutido no binário (T8).
//!
//! O Rust consome exatamente os mesmos arquivos versionados que o Python
//! (`contracts/identity/*.json`). O `include_str!` faz o cargo reconstruir o
//! binário quando o catálogo muda; não existe lista paralela em código.
//!
//! O IPC/CLI passam a consumir estes tipos no T8.3; até lá algumas APIs são
//! exercitadas apenas pelos testes.

#![allow(dead_code)]

use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

pub const SCOPES_JSON: &str = include_str!("../../../../contracts/identity/scopes.json");
pub const ROLES_JSON: &str = include_str!("../../../../contracts/identity/roles.json");

pub const OWNER_ROLE: &str = "owner";
pub const REQUIRED_ROLE_IDS: &[&str] = &["owner", "family", "guest", "service"];

/// Escopos que um papel built-in nunca pode receber. Regras terminadas em `.`
/// são prefixos; as demais são escopos exatos.
const FORBIDDEN_SCOPES: &[(&str, &[&str])] = &[
    (
        "family",
        &[
            "identity.principals.",
            "identity.devices.",
            "identity.pairing.",
            "security.approvals.read.all",
            "security.approvals.decide.others",
            "memory.read.all",
            "memory.delete.any",
            "agents.manage",
            "tools.execute",
            "llm.remote",
            "system.backup",
            "system.admin",
        ],
    ),
    (
        "guest",
        &[
            "identity.principals.",
            "identity.devices.",
            "identity.pairing.",
            "security.",
            "agents.",
            "tools.write",
            "tools.execute",
            "memory.read.shared",
            "memory.write.shared",
            "memory.read.all",
            "memory.delete.any",
            "llm.remote",
            "system.",
        ],
    ),
    (
        "service",
        &["identity.", "security.", "agents.", "llm.remote"],
    ),
];

#[derive(Debug, thiserror::Error)]
pub enum CatalogError {
    #[error("catálogo inválido: {0}")]
    Invalid(String),
    #[error("escopo desconhecido: {0}")]
    UnknownScope(String),
    #[error("papel desconhecido: {0}")]
    UnknownRole(String),
    #[error("escopos fora do papel {role}: {scopes:?}")]
    PrivilegeEscalation { role: String, scopes: Vec<String> },
    #[error("JSON do catálogo inválido: {0}")]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Clone)]
pub struct Scope {
    pub id: String,
    pub category: String,
    pub description: String,
}

#[derive(Debug, Clone)]
pub struct Role {
    pub id: String,
    pub name: String,
    pub description: String,
    pub builtin: bool,
    pub scopes: BTreeSet<String>,
}

#[derive(Debug)]
pub struct Catalog {
    pub catalog_version: String,
    pub contract_version: String,
    pub scopes: BTreeMap<String, Scope>,
    pub roles: BTreeMap<String, Role>,
}

#[derive(Deserialize)]
struct ScopesDocument {
    catalog_version: String,
    contract_version: String,
    scopes: Vec<ScopeEntry>,
}

#[derive(Deserialize)]
struct ScopeEntry {
    id: String,
    category: String,
    description: String,
}

#[derive(Deserialize)]
struct RolesDocument {
    catalog_version: String,
    contract_version: String,
    roles: Vec<RoleEntry>,
}

#[derive(Deserialize)]
struct RoleEntry {
    id: String,
    name: String,
    description: String,
    builtin: bool,
    scopes: Vec<String>,
}

impl Catalog {
    /// Valida os dois documentos JSON e devolve o catálogo.
    pub fn from_json(scopes_json: &str, roles_json: &str) -> Result<Self, CatalogError> {
        let scopes_doc: ScopesDocument = serde_json::from_str(scopes_json)?;
        let roles_doc: RolesDocument = serde_json::from_str(roles_json)?;

        if !valid_version(&scopes_doc.catalog_version)
            || !valid_version(&scopes_doc.contract_version)
        {
            return Err(CatalogError::Invalid(
                "versões do catálogo devem ser semver (ex.: 1.0.0)".to_string(),
            ));
        }
        if scopes_doc.catalog_version != roles_doc.catalog_version {
            return Err(CatalogError::Invalid(
                "scopes.json e roles.json têm catalog_version diferentes".to_string(),
            ));
        }
        if scopes_doc.contract_version != roles_doc.contract_version {
            return Err(CatalogError::Invalid(
                "scopes.json e roles.json têm contract_version diferentes".to_string(),
            ));
        }

        let mut scopes = BTreeMap::new();
        for entry in scopes_doc.scopes {
            if !valid_scope_id(&entry.id) {
                return Err(CatalogError::Invalid(format!(
                    "id de escopo inválido: {:?}",
                    entry.id
                )));
            }
            if entry.id.split('.').next() != Some(entry.category.as_str()) {
                return Err(CatalogError::Invalid(format!(
                    "categoria {:?} não corresponde ao prefixo de {:?}",
                    entry.category, entry.id
                )));
            }
            if entry.description.trim().is_empty() {
                return Err(CatalogError::Invalid(format!(
                    "escopo {:?} sem descrição",
                    entry.id
                )));
            }
            if scopes.contains_key(&entry.id) {
                return Err(CatalogError::Invalid(format!(
                    "escopo duplicado: {:?}",
                    entry.id
                )));
            }
            scopes.insert(
                entry.id.clone(),
                Scope {
                    id: entry.id,
                    category: entry.category,
                    description: entry.description,
                },
            );
        }
        if scopes.is_empty() {
            return Err(CatalogError::Invalid(
                "catálogo não pode ser vazio".to_string(),
            ));
        }

        let mut roles = BTreeMap::new();
        for entry in roles_doc.roles {
            if !valid_role_id(&entry.id) {
                return Err(CatalogError::Invalid(format!(
                    "id de papel inválido: {:?}",
                    entry.id
                )));
            }
            if roles.contains_key(&entry.id) {
                return Err(CatalogError::Invalid(format!(
                    "papel duplicado: {:?}",
                    entry.id
                )));
            }
            if entry.scopes.is_empty() {
                return Err(CatalogError::Invalid(format!(
                    "papel {:?} sem escopos",
                    entry.id
                )));
            }
            let mut granted = BTreeSet::new();
            for scope in entry.scopes {
                if !scopes.contains_key(&scope) {
                    return Err(CatalogError::UnknownScope(scope));
                }
                if !granted.insert(scope.clone()) {
                    return Err(CatalogError::Invalid(format!(
                        "escopo duplicado {:?} no papel {:?}",
                        scope, entry.id
                    )));
                }
            }
            roles.insert(
                entry.id.clone(),
                Role {
                    id: entry.id,
                    name: entry.name,
                    description: entry.description,
                    builtin: entry.builtin,
                    scopes: granted,
                },
            );
        }

        for role_id in REQUIRED_ROLE_IDS {
            match roles.get(*role_id) {
                Some(role) if role.builtin => {}
                Some(_) => {
                    return Err(CatalogError::Invalid(format!(
                        "papel obrigatório {role_id:?} deve ter builtin=true"
                    )))
                }
                None => {
                    return Err(CatalogError::Invalid(format!(
                        "papel built-in ausente: {role_id:?}"
                    )))
                }
            }
        }

        let owner = roles
            .get(OWNER_ROLE)
            .expect("owner validado como papel obrigatório");
        let missing: Vec<&str> = scopes
            .keys()
            .filter(|scope| !owner.scopes.contains(*scope))
            .map(String::as_str)
            .collect();
        if !missing.is_empty() {
            return Err(CatalogError::Invalid(format!(
                "owner não cobre todos os escopos; faltam: {missing:?}"
            )));
        }

        for (role_id, rules) in FORBIDDEN_SCOPES {
            let Some(role) = roles.get(*role_id) else {
                continue;
            };
            let hits: Vec<&str> = role
                .scopes
                .iter()
                .filter(|scope| matches_forbidden(scope, rules))
                .map(String::as_str)
                .collect();
            if !hits.is_empty() {
                return Err(CatalogError::Invalid(format!(
                    "papel {role_id:?} contém escopos proibidos: {hits:?}"
                )));
            }
        }

        Ok(Self {
            catalog_version: scopes_doc.catalog_version,
            contract_version: scopes_doc.contract_version,
            scopes,
            roles,
        })
    }

    pub fn scope(&self, scope_id: &str) -> Result<&Scope, CatalogError> {
        self.scopes
            .get(scope_id)
            .ok_or_else(|| CatalogError::UnknownScope(scope_id.to_string()))
    }

    pub fn role(&self, role_id: &str) -> Result<&Role, CatalogError> {
        self.roles
            .get(role_id)
            .ok_or_else(|| CatalogError::UnknownRole(role_id.to_string()))
    }

    pub fn require_scope(&self, scope_id: &str) -> Result<(), CatalogError> {
        self.scope(scope_id).map(|_| ())
    }

    pub fn scopes_for_role(&self, role_id: &str) -> Result<BTreeSet<String>, CatalogError> {
        Ok(self.role(role_id)?.scopes.clone())
    }

    /// Narrowing explícito: `None` mantém o papel, uma lista vazia reduz a
    /// nada, escopo desconhecido ou fora do papel falha. Nunca há união.
    pub fn narrow(
        &self,
        role_id: &str,
        requested: Option<&[&str]>,
    ) -> Result<BTreeSet<String>, CatalogError> {
        let role = self.role(role_id)?;
        let Some(requested) = requested else {
            return Ok(role.scopes.clone());
        };
        let mut result = BTreeSet::new();
        let mut escalated = Vec::new();
        for scope in requested {
            self.require_scope(scope)?;
            if role.scopes.contains(*scope) {
                result.insert((*scope).to_string());
            } else {
                escalated.push((*scope).to_string());
            }
        }
        if !escalated.is_empty() {
            return Err(CatalogError::PrivilegeEscalation {
                role: role_id.to_string(),
                scopes: escalated,
            });
        }
        Ok(result)
    }
}

/// Catálogo embutido, validado uma única vez por processo.
pub fn builtin_catalog() -> &'static Catalog {
    static CATALOG: OnceLock<Catalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        Catalog::from_json(SCOPES_JSON, ROLES_JSON)
            .expect("catálogo de identidade embutido inválido")
    })
}

fn valid_version(value: &str) -> bool {
    let parts: Vec<&str> = value.split('.').collect();
    parts.len() == 3
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()))
}

fn valid_role_id(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn valid_scope_id(value: &str) -> bool {
    let mut segments = value.split('.');
    let Some(first) = segments.next() else {
        return false;
    };
    if !valid_first_segment(first) {
        return false;
    }
    let mut count = 1;
    for segment in segments {
        if !valid_later_segment(segment) {
            return false;
        }
        count += 1;
    }
    count >= 2
}

fn valid_first_segment(segment: &str) -> bool {
    let mut chars = segment.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

fn valid_later_segment(segment: &str) -> bool {
    let mut chars = segment.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

fn matches_forbidden(scope: &str, rules: &[&str]) -> bool {
    rules
        .iter()
        .any(|rule| scope == *rule || (rule.ends_with('.') && scope.starts_with(rule)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    fn scopes_value() -> Value {
        serde_json::from_str(SCOPES_JSON).expect("scopes.json")
    }

    fn roles_value() -> Value {
        serde_json::from_str(ROLES_JSON).expect("roles.json")
    }

    fn build(scopes: &Value, roles: &Value) -> Result<Catalog, CatalogError> {
        Catalog::from_json(
            &serde_json::to_string(scopes).expect("scopes"),
            &serde_json::to_string(roles).expect("roles"),
        )
    }

    #[test]
    fn builtin_catalog_is_well_formed() {
        let catalog = builtin_catalog();
        assert_eq!(catalog.catalog_version, "1.0.0");
        assert_eq!(catalog.contract_version, "1.0.0");
        assert_eq!(catalog.scopes.len(), 28);
        assert_eq!(catalog.roles.len(), 4);
        for scope in catalog.scopes.values() {
            assert_eq!(scope.id.split('.').next(), Some(scope.category.as_str()));
            assert!(!scope.description.trim().is_empty());
        }
    }

    #[test]
    fn owner_covers_every_scope() {
        let catalog = builtin_catalog();
        let owner = catalog.role("owner").expect("owner");
        let all: BTreeSet<String> = catalog.scopes.keys().cloned().collect();
        assert_eq!(owner.scopes, all);
    }

    #[test]
    fn builtin_role_sizes_are_pinned() {
        let catalog = builtin_catalog();
        assert_eq!(catalog.scopes_for_role("family").expect("family").len(), 14);
        assert_eq!(catalog.scopes_for_role("guest").expect("guest").len(), 6);
        assert_eq!(
            catalog.scopes_for_role("service").expect("service").len(),
            6
        );
    }

    #[test]
    fn forbidden_roles_reject_sensitive_scopes() {
        let catalog = builtin_catalog();
        let family = catalog.scopes_for_role("family").expect("family");
        assert!(!family.contains("llm.remote"));
        assert!(!family.contains("system.admin"));
        assert!(!family.contains("security.approvals.decide.others"));
        let guest = catalog.scopes_for_role("guest").expect("guest");
        assert!(!guest.contains("tools.write"));
        assert!(!guest.contains("memory.read.shared"));
        let service = catalog.scopes_for_role("service").expect("service");
        assert!(!service.iter().any(|scope| scope.starts_with("identity.")));
        assert!(!service.iter().any(|scope| scope.starts_with("security.")));
    }

    #[test]
    fn narrow_none_keeps_all_and_empty_yields_none() {
        let catalog = builtin_catalog();
        assert_eq!(
            catalog.narrow("family", None).expect("none"),
            catalog.scopes_for_role("family").expect("family")
        );
        assert!(catalog
            .narrow("family", Some(&[]))
            .expect("empty")
            .is_empty());
    }

    #[test]
    fn narrow_keeps_subset_and_rejects_escalation() {
        let catalog = builtin_catalog();
        let narrowed = catalog
            .narrow("family", Some(&["tools.read", "memory.read.shared"]))
            .expect("subset");
        assert_eq!(narrowed.len(), 2);
        let error = catalog.narrow("guest", Some(&["tools.write"])).unwrap_err();
        assert!(matches!(
            error,
            CatalogError::PrivilegeEscalation { ref role, .. } if role == "guest"
        ));
    }

    #[test]
    fn narrow_rejects_unknown_scope() {
        let catalog = builtin_catalog();
        let error = catalog.narrow("family", Some(&["tools.nuke"])).unwrap_err();
        assert!(matches!(error, CatalogError::UnknownScope(scope) if scope == "tools.nuke"));
    }

    #[test]
    fn duplicate_scope_is_rejected() {
        let mut scopes = scopes_value();
        let first = scopes["scopes"][0].clone();
        scopes["scopes"].as_array_mut().unwrap().push(first);
        let error = build(&scopes, &roles_value()).unwrap_err();
        assert!(error.to_string().contains("duplicado"));
    }

    #[test]
    fn category_must_match_prefix() {
        let mut scopes = scopes_value();
        scopes["scopes"][0]["category"] = json!("memory");
        let error = build(&scopes, &roles_value()).unwrap_err();
        assert!(error.to_string().contains("categoria"));
    }

    #[test]
    fn owner_must_cover_every_scope() {
        let mut roles = roles_value();
        let owner = roles["roles"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|role| role["id"] == "owner")
            .expect("owner");
        let scopes = owner["scopes"].as_array_mut().unwrap();
        scopes.retain(|scope| scope != "system.admin");
        let error = build(&scopes_value(), &roles).unwrap_err();
        assert!(error.to_string().contains("system.admin"));
    }

    #[test]
    fn family_cannot_gain_remote_llm() {
        let mut roles = roles_value();
        let family = roles["roles"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|role| role["id"] == "family")
            .expect("family");
        family["scopes"]
            .as_array_mut()
            .unwrap()
            .push(json!("llm.remote"));
        let error = build(&scopes_value(), &roles).unwrap_err();
        assert!(error.to_string().contains("proibidos"));
    }

    #[test]
    fn version_mismatch_is_rejected() {
        let mut roles = roles_value();
        roles["catalog_version"] = json!("2.0.0");
        let error = build(&scopes_value(), &roles).unwrap_err();
        assert!(error.to_string().contains("catalog_version"));
    }

    #[test]
    fn invalid_json_is_rejected() {
        let error = Catalog::from_json("{", ROLES_JSON).unwrap_err();
        assert!(matches!(error, CatalogError::Json(_)));
    }
}
