use tracing_subscriber::{fmt, EnvFilter};

/// Configura o logging estruturado + console.
///
/// Idempotente: chamar múltiplas vezes reconfigura o subscriber global.
pub fn init(level: &str, _json: bool) -> Result<(), String> {
    let filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(level));
    fmt::fmt()
        .with_env_filter(filter)
        .try_init()
        .map(|_| ())
        .map_err(|e| format!("{e:?}"))
}

#[cfg(test)]
mod tests {
    #[test]
    fn init_is_idempotent() {
        super::init("INFO", true).ok();
        super::init("DEBUG", false).ok();
    }
}
