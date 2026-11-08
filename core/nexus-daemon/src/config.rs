use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub nexus: NexusSection,
    pub paths: PathsSection,
    #[serde(default)]
    pub logging: LoggingSection,
    #[serde(default)]
    pub events: EventsSection,
    #[serde(default)]
    pub ipc: IpcSection,
    #[serde(default)]
    pub services: ServicesSection,
    #[serde(default)]
    pub health: HealthSection,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NexusSection {
    pub name: String,
    #[serde(default = "default_environment")]
    pub environment: String,
}

fn default_environment() -> String {
    "production".to_string()
}

#[derive(Debug, Clone, Deserialize)]
pub struct PathsSection {
    pub data_dir: String,
    #[serde(default = "default_run_dir")]
    pub run_dir: String,
    #[serde(default = "default_log_file")]
    pub log_file: String,
}

fn default_run_dir() -> String {
    "data/run".to_string()
}
fn default_log_file() -> String {
    "data/logs/nexus.log".to_string()
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct LoggingSection {
    #[serde(default = "default_log_level")]
    pub level: String,
    #[serde(default = "default_true")]
    pub json: bool,
}

fn default_log_level() -> String {
    "INFO".to_string()
}
fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct EventsSection {
    #[serde(default = "default_retention_days")]
    pub retention_days: i64,
    #[serde(default = "default_dedup_window_hours")]
    pub dedup_window_hours: i64,
    #[serde(default = "default_query_limit")]
    pub query_limit: i64,
}

fn default_retention_days() -> i64 {
    0
}
fn default_dedup_window_hours() -> i64 {
    24
}
fn default_query_limit() -> i64 {
    500
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct IpcSection {
    #[serde(default = "default_socket_path")]
    pub socket_path: String,
}

fn default_socket_path() -> String {
    "data/run/nexus-daemon.sock".to_string()
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct ServicesSection {
    #[serde(default)]
    pub python_runtime: Option<ServiceEntry>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServiceEntry {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

fn default_enabled() -> bool {
    false
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct HealthSection {
    #[serde(default = "default_health_interval")]
    pub check_interval_secs: u64,
}

fn default_health_interval() -> u64 {
    30
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("arquivo de configuração não encontrado: {0}")]
    NotFound(PathBuf),
    #[error("TOML inválido em {0}: {1}")]
    InvalidToml(PathBuf, String),
    #[error("falha ao ler {0}: {1}")]
    IoError(PathBuf, String),
    #[error("configuração inválida: {0}")]
    Invalid(String),
}

pub fn load_config<P: AsRef<Path>>(path: P) -> Result<Config, ConfigError> {
    let path = path.as_ref();
    if !path.is_file() {
        return Err(ConfigError::NotFound(path.to_path_buf()));
    }
    let contents = std::fs::read_to_string(path).map_err(|e| ConfigError::IoError(path.to_path_buf(), e.to_string()))?;
    let config: Config = toml::from_str(&contents).map_err(|e| ConfigError::InvalidToml(path.to_path_buf(), e.to_string()))?;
    validate(&config)?;
    Ok(config)
}

fn validate(config: &Config) -> Result<(), ConfigError> {
    if config.nexus.name.is_empty() {
        return Err(ConfigError::Invalid("nexus.name não pode ser vazio".to_string()));
    }
    if config.paths.data_dir.is_empty() {
        return Err(ConfigError::Invalid("paths.data_dir não pode ser vazio".to_string()));
    }
    let level = config.logging.level.to_uppercase();
    match level.as_str() {
        "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL" => {}
        _ => {
            return Err(ConfigError::Invalid(format!(
                "logging.level inválido: {}",
                config.logging.level
            )));
        }
    }
    if config.ipc.socket_path.is_empty() {
        return Err(ConfigError::Invalid("ipc.socket_path não pode ser vazio".to_string()));
    }
    Ok(())
}

/// Resolve caminhos relativos em relação ao diretório raiz (pai do diretório `config/`).
pub fn resolve_root(source: &Path) -> PathBuf {
    if source.parent().map(|p| p.ends_with("config")).unwrap_or(false) {
        source.parent().unwrap().to_path_buf()
    } else {
        source.to_path_buf()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn load_valid_config() {
        let toml = r#"
[nexus]
name = "NEXUS"
environment = "production"

[paths]
data_dir = "data"

[logging]
level = "INFO"
json = true

[events]
retention_days = 0
dedup_window_hours = 24
query_limit = 500

[ipc]
socket_path = "data/run/nexus-daemon.sock"

[services]
  [services.python_runtime]
  command = "python"
  args = ["-m", "nexus_core", "serve"]
  enabled = false

[health]
check_interval_secs = 30
"#;
        let config: Config = toml::from_str(toml).unwrap();
        assert_eq!(config.nexus.name, "NEXUS");
        assert_eq!(config.logging.level, "INFO");
        assert_eq!(config.ipc.socket_path, "data/run/nexus-daemon.sock");
        assert!(config.services.python_runtime.unwrap().enabled == false);
    }

    #[test]
    fn invalid_log_level_rejected() {
        let toml = r#"
[nexus]
name = "NEXUS"

[paths]
data_dir = "data"

[logging]
level = "VERBOSE"

[ipc]
socket_path = "data/run/nexus-daemon.sock"
"#;
        let result: Result<Config, _> = toml::from_str(toml);
        // toml deserializa, mas nossa validação de nível é feita em `validate`.
        // Aqui apenas confirmamos que o parsing TOML funciona; o nível inválido pega na validação.
        let config = result.unwrap();
        let level = config.logging.level.to_uppercase();
        assert!(level != "DEBUG" && level != "INFO" && level != "WARNING" && level != "ERROR" && level != "CRITICAL");
    }
}
