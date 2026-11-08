use serde::Serialize;
use std::time::SystemTime;

#[derive(Debug, Clone, Serialize)]
pub struct HealthStatus {
    pub status: String, // "ok" | "degraded"
    pub version: String,
    pub uptime_ms: u64,
    pub checks: Vec<HealthCheck>,
    pub timestamp: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HealthCheck {
    pub name: String,
    pub ok: bool,
    pub detail: Option<String>,
}

/// Resultado de um health check.
#[derive(Debug, Clone)]
pub enum CheckResult {
    Ok,
    Degraded(String),
}

/// Registra e executa health checks do daemon.
pub struct HealthChecker {
    started_at: SystemTime,
    version: String,
}

impl HealthChecker {
    pub fn new(version: String) -> Self {
        Self {
            started_at: SystemTime::now(),
            version,
        }
    }

    pub fn check(&self, resource_stats: Option<&crate::resources::monitor::ResourceStats>) -> HealthStatus {
        let mut checks = Vec::new();
        let mut overall_ok = true;

        checks.push(HealthCheck {
            name: "configuracao".to_string(),
            ok: true,
            detail: None,
        });

        if let Some(res) = resource_stats {
            let ram_ok = res.ram.status == "healthy";
            let swap_ok = res.swap.status == "healthy";
            overall_ok &= ram_ok && swap_ok;
            checks.push(HealthCheck {
                name: "recursos".to_string(),
                ok: ram_ok && swap_ok,
                detail: Some(format!(
                    "RAM {}% ({}), swap {}% ({})",
                    res.ram.percent.round() as u64, res.ram.status,
                    res.swap.percent.round() as u64, res.swap.status
                )),
            });
        }

        let uptime_ms = self
            .started_at
            .elapsed()
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);

        HealthStatus {
            status: if overall_ok { "ok" } else { "degraded" }.to_string(),
            version: self.version.clone(),
            uptime_ms,
            checks,
            timestamp: now_iso(),
        }
    }
}

fn now_iso() -> String {
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .expect("relógio");
    crate::events::types::chrono_naive_utc(now.as_secs(), now.subsec_nanos())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_ok_when_no_resources() {
        let hc = HealthChecker::new("0.1.0".to_string());
        let status = hc.check(None);
        assert_eq!(status.status, "ok");
        assert_eq!(status.version, "0.1.0");
        assert!(status.checks.iter().any(|c| c.name == "configuracao" && c.ok));
    }
}
