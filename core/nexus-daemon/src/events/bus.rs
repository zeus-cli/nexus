use super::types::{Event, EventError};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

/// Padrão de assinatura: ``kind`` exato, ``prefixo.*`` ou ``*`` global.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pattern {
    Exact(String),
    Prefix(String),
    Global,
}

impl Pattern {
    pub fn new(pattern: &str) -> Result<Self, EventError> {
        if pattern == "*" {
            return Ok(Pattern::Global);
        }
        if let Some(rest) = pattern.strip_suffix(".*") {
            if rest.is_empty() || rest.ends_with('.') {
                return Err(EventError::InvalidKind(pattern.to_string()));
            }
            return Ok(Pattern::Prefix(rest.to_string()));
        }
        // validação básica do kind exato
        if !pattern
            .split('.')
            .all(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'))
        {
            return Err(EventError::InvalidKind(pattern.to_string()));
        }
        Ok(Pattern::Exact(pattern.to_string()))
    }

    pub fn matches(&self, kind: &str) -> bool {
        match self {
            Pattern::Global => true,
            Pattern::Exact(p) => p == kind,
            Pattern::Prefix(prefix) => kind == *prefix || kind.starts_with(&format!("{prefix}.")),
        }
    }
}

/// Estatísticas do barramento de eventos.
#[derive(Debug, Default, Clone)]
pub struct BusStats {
    pub published: u64,
    pub delivered: u64,
    pub failures: u64,
    pub subscribers: usize,
}

/// Assinatura ativa: padrão + canal de entrega.
pub struct Subscription {
    pub id: String,
    pub pattern: Pattern,
    pub sender: tokio::sync::mpsc::UnboundedSender<Event>,
}

impl Subscription {
    fn matches(&self, kind: &str) -> bool {
        self.pattern.matches(kind)
    }
}

/// Barramento de eventos em memória.
///
/// Garantias (fase 1):
/// * ordem de entrega preservada por assinante (envio sequencial);
/// * falha de um assinante não interrompe os demais;
/// * nenhum assinante bloqueia o publicador (canais síncronos com `is_send`);
/// * dedup opcional aplicado pelo chamador (o barramento não deduplica).
#[derive(Default)]
pub struct EventBus {
    subscriptions: Arc<RwLock<HashMap<String, Subscription>>>,
    stats: Arc<RwLock<BusStats>>,
}

impl EventBus {
    pub fn new() -> Self {
        Self::default()
    }

    /// Inscreve um handler (canal) por padrão. Retorna o id da assinatura.
    pub async fn subscribe(&self, pattern: &str, sender: tokio::sync::mpsc::UnboundedSender<Event>) -> String {
        let pattern = Pattern::new(pattern).expect("padrão de assinatura inválido");
        let id = uuid::Uuid::new_v4().to_string();
        let sub = Subscription {
            id: id.clone(),
            pattern,
            sender,
        };
        self.subscriptions.write().await.insert(id.clone(), sub);
        {
            let mut stats = self.stats.write().await;
            stats.subscribers = self.subscriptions.read().await.len();
        }
        id
    }

    pub async fn unsubscribe(&self, id: &str) -> bool {
        let removed = self.subscriptions.write().await.remove(id).is_some();
        if removed {
            let mut stats = self.stats.write().await;
            stats.subscribers = self.subscriptions.read().await.len();
        }
        removed
    }

    /// Publica um evento para todos os assinantes compatíveis.
    /// Retorna quantas entregas foram enfileiradas com sucesso.
    pub async fn publish(&self, event: Event) -> u64 {
        {
            let mut stats = self.stats.write().await;
            stats.published += 1;
        }
        let subs = self.subscriptions.read().await;
        let mut delivered = 0u64;
        for sub in subs.values() {
            if sub.matches(&event.kind) {
                if sub.sender.send(event.clone()).is_ok() {
                    delivered += 1;
                } else {
                    let mut stats = self.stats.write().await;
                    stats.failures += 1;
                }
            }
        }
        {
            let mut stats = self.stats.write().await;
            stats.delivered += delivered;
        }
        delivered
    }

    pub async fn stats(&self) -> BusStats {
        self.stats.read().await.clone()
    }

    pub async fn subscriber_count(&self) -> usize {
        self.subscriptions.read().await.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::types::Severity;
    use tokio::sync::mpsc;

    #[tokio::test]
    async fn subscribe_and_deliver() {
        let bus = EventBus::new();
        let (tx, mut rx) = mpsc::unbounded_channel::<Event>();
        let id = bus.subscribe("system.*", tx).await;

        let ev = crate::events::types::Event::new("system.started", "test", serde_json::json!({}));
        bus.publish(ev.clone()).await;

        let received = rx.recv().await.unwrap();
        assert_eq!(received.event_id, ev.event_id);
        assert_eq!(bus.unsubscribe(&id).await, true);
    }

    #[tokio::test]
    async fn pattern_matching() {
        assert!(Pattern::new("system.*").unwrap().matches("system.started"));
        assert!(Pattern::new("system.*").unwrap().matches("system")); // prefixo casa com o próprio prefixo
        assert!(!Pattern::new("system.*").unwrap().matches("systemx"));
        assert!(Pattern::new("*").unwrap().matches("anything"));
        assert!(Pattern::new("agent.task.started").unwrap().matches("agent.task.started"));
    }

    #[tokio::test]
    async fn stats_track_delivery() {
        let bus = EventBus::new();
        let (tx1, mut rx1) = mpsc::unbounded_channel::<Event>();
        let (tx2, _rx2) = mpsc::unbounded_channel::<Event>();
        bus.subscribe("system.*", tx1).await;
        bus.subscribe("agent.*", tx2).await;

        bus.publish(crate::events::types::Event::new("system.started", "t", serde_json::json!({}))).await;

        // Só rx1 deve receber (system.started casa com system.*).
        let _ = rx1.recv().await;

        let stats = bus.stats().await;
        assert_eq!(stats.published, 1);
        assert_eq!(stats.delivered, 1);
    }
}
