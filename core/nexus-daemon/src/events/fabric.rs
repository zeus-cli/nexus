//! Event Fabric do daemon: única porta de entrada para store + bus.
//!
//! A ordem é sempre a mesma: persistir no `EventStore` e, apenas quando o
//! evento é novo, publicar no `EventBus`. O daemon não publica eventos
//! diretamente no barramento, espelhando o `EventRecorder`/`EventFabric` do
//! Python.
//!
//! O outbox durável e o dead-letter continuam no lado Python (T2). No Rust, o
//! store SQLite é a fonte de verdade e o IPC entrega eventos aos assinantes;
//! um outbox com offsets/acks por assinante pertence ao T4, quando o Control
//! Plane passar a ser autoridade.

use super::types::Event;
use super::{EventBus, EventStore};
use std::sync::Arc;

/// Fachada de emissão usada pelo bootstrap, pelo loop principal e pelo IPC.
pub struct EventFabric {
    store: Arc<EventStore>,
    bus: Arc<EventBus>,
}

impl EventFabric {
    pub fn new(store: Arc<EventStore>, bus: Arc<EventBus>) -> Self {
        Self { store, bus }
    }

    pub fn bus(&self) -> &Arc<EventBus> {
        &self.bus
    }

    /// Persiste o evento e só publica se ele foi realmente inserido.
    ///
    /// Retorna `Ok(true)` quando o evento é novo e `Ok(false)` quando a
    /// deduplicação encontrou um equivalente na janela configurada.
    pub async fn emit(
        &self,
        event: Event,
        dedup_window_seconds: i64,
    ) -> Result<bool, rusqlite::Error> {
        let (stored, inserted) = self.store.append(&event, dedup_window_seconds)?;
        if inserted {
            self.bus.publish(stored).await;
        }
        Ok(inserted)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::types::Severity;
    use tokio::sync::mpsc;

    fn fabric() -> (Arc<EventStore>, Arc<EventBus>, EventFabric) {
        let store = Arc::new(EventStore::open(":memory:").unwrap());
        let bus = Arc::new(EventBus::new());
        let fabric = EventFabric::new(Arc::clone(&store), Arc::clone(&bus));
        (store, bus, fabric)
    }

    #[tokio::test]
    async fn emit_persists_then_publishes() {
        let (store, bus, fabric) = fabric();
        let (tx, mut rx) = mpsc::unbounded_channel::<Event>();
        bus.subscribe("system.*", tx).await;

        let event = Event::new("system.started", "test", serde_json::json!({"pid": 1}))
            .with_severity(Severity::Info);

        assert!(fabric.emit(event.clone(), 86_400).await.unwrap());
        let delivered = rx.recv().await.expect("evento entregue");
        assert_eq!(delivered.event_id, event.event_id);
        assert_eq!(store.count().unwrap(), 1);
    }

    #[tokio::test]
    async fn emit_deduplicates_and_does_not_republish() {
        let (store, bus, fabric) = fabric();
        let (tx, mut rx) = mpsc::unbounded_channel::<Event>();
        bus.subscribe("system.*", tx).await;

        let event = Event::new("system.started", "test", serde_json::json!({"pid": 1}));

        assert!(fabric.emit(event.clone(), 86_400).await.unwrap());
        assert_eq!(rx.recv().await.unwrap().event_id, event.event_id);
        assert!(!fabric.emit(event, 86_400).await.unwrap());
        assert!(rx.try_recv().is_err(), "duplicata não deve ser republicada");
        assert_eq!(store.count().unwrap(), 1);
    }
}
