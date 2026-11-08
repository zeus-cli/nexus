# ADR 0012 — Event Fabric: Transactional Outbox e Dead-Letter Queue (Fase 7+)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/event_fabric.py` — Transactional Outbox + Dead-Letter Queue
- **Depende de:** ADR 0001–0011 (EventStore, EventBus existentes)

---

## Contexto

O Event Fabric existente (EventBus + EventStore) opera apenas em processo. Para suporte a operação distribuída (Fase 7), é necessário garantir:

* **Transactional Outbox:** eventos críticos devem ser persistidos atomicamente junto com transações de negócio, evitando perda de mensagens quando a entrega ao EventBus falha.
* **Dead-Letter Queue:** eventos que falham repetidamente na entrega devem ser direcionados para uma fila de mortos, evitando bloqueio do sistema e permitindo inspeção/reenvio manual.
* **Retry com backoff exponencial:** eventos falhados são retryados com atraso crescente, evitando saturação do sistema.

Sem essas garantias, eventos críticos podem ser perdidos silenciosamente, comprometendo auditoria e consistência do sistema distribuído.

---

## Decisão 1 — Transactional Outbox

Criado `TransactionalOutbox` que persiste eventos em SQLite (WAL) antes de entregá-los ao EventBus. O fluxo é:

1. `enqueue(event)` — persiste o evento como PENDING no outbox.
2. `get_pending()` — retorna eventos pendentes ou com retry agendado (next_retry_at <= agora).
3. `mark_delivered(entry_id)` — marca como DELIVERED após entrega bem-sucedida.
4. `mark_failed(entry_id, error, retry_delay_s)` — marca como falhada, agendando retry com backoff exponencial (2^attempts segundos) ou movendo para dead-letter após max_attempts.

O outbox é consultado por um worker dedicado que entrega eventos ao EventBus e atualiza o status.

---

## Decisão 2 — Dead-Letter Queue

Criado `DeadLetterProcessor` que gerencia a fila de mensagens mortas:

* Eventos que excedem max_attempts são movidos automaticamente para dead_letter com o motivo da falha.
* `list_dead_letter()` retorna todos os eventos mortos.
* `requeue_from_dead_letter(entry_id)` reenfileira um evento morto de volta ao outbox como PENDING (com novo event_id para evitar UNIQUE constraint).
* `retry_all_dead_letter()` reenfileira todos os eventos mortos de uma vez.

Estatísticas de motivos de falha são expostas para diagnóstico operacional.

---

## Decisão 3 — Integração com o EventBus existente

O Outbox não substitui o EventBus existente; ele o complementa. Eventos críticos são primeiro enfileirados no outbox e, após entrega bem-sucedida, publicados no EventBus. Isso garante consistência entre estado de negócio e eventos emitidos.

---

## Consequências

* **Positivo:** Nenhum evento crítico é perdido devido a falha de entrega; sistema mais resiliente para operação distribuída; auditoria completa de tentativas de entrega.
* **Negativo:** Sobrecarga de gravação no SQLite para cada evento crítico; requer worker dedicado para entrega do outbox.
* **Risco conhecido:** O outbox usa um banco SQLite separado do EventStore; consistência entre os dois bancos não é garantida por transação única (seria necessário two-phase commit, descartado por complexidade).

---

## Testes

* `tests/test_event_fabric.py`: 9 testes cobrindo enqueue, get_pending, mark_delivered, mark_failed com retry, max_attempts → dead-letter, stats, dead-letter list/requeue, processor stats e retry_all.
* Regressão completa: 280 testes passando.
