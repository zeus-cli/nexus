# ADR 0017 — Event Fabric único: outbox transacional, consumidor idempotente e bridge Python ↔ Rust (T2)

- **Status:** aceito
- **Data:** 2026-11-14
- **Escopo:** T2 do roadmap — fechar o Event Fabric como única porta de emissão
  nos dois lados, com outbox na mesma transação do EventStore, retry/backoff,
  dead-letter, consumidor idempotente e bridge UDS Python ↔ Rust
- **Depende de:** ADR 0001 (EventStore/EventBus), ADR 0004 (nexus-daemon),
  ADR 0012 (outbox/dead-letter originais), ADR 0016 (contratos v1.0.0)

---

## Contexto

O ADR 0012 introduziu outbox e dead-letter, mas deixou um risco explícito: o
outbox vivia em um SQLite separado do EventStore. `EventStore.append` e
`TransactionalOutbox.enqueue` eram duas transações independentes; um crash entre
elas podia persistir um evento sem entrada no outbox (evento nunca entregue) ou
enfileirar uma entrega sem o fato correspondente no store (entrega sem
auditoria).

Além disso, a entrega era um worker manual, sem fachada única de emissão, sem
consumidor idempotente durável e sem protocolo operacional entre o runtime
Python e o daemon Rust. O daemon publicava eventos diretamente no `EventBus`
sem uma fachada equivalente, e o IPC não tinha subscribe/publish reais.

Restrições:

- preservar o backend Python e os componentes Rust existentes;
- não expor a API remotamente antes do gate Rust (T4–T6);
- segurança determinística e fail-closed, independente de LLM;
- nenhuma durabilidade fictícia: o barramento Rust continua em memória;
- manter eventos já persistidos legíveis e não reescrever identidade de fatos.

---

## Decisão

1. **Outbox, dead-letter e consumidores no mesmo SQLite do EventStore.**
   A migração 4 (`event_fabric`) cria `outbox`, `dead_letter` e
   `consumer_processed` no banco governado pelo `Database`. O DDL é
   compartilhado por `nexus_core/event_fabric_schema.py` entre a migração e o
   `TransactionalOutbox`. Assim, `EventStore.append` e `enqueue` participam da
   mesma transação `BEGIN IMMEDIATE`/`COMMIT`; qualquer falha no enqueue faz
   rollback do evento. O `TransactionalOutbox` ainda aceita um caminho
   independente para testes e ferramentas, mas o runtime usa o banco
   compartilhado.

2. **`EventFabric` é a única porta de emissão no Python.**
   `EventFabric.emit` faz persistência + enqueue atômicos, tenta a entrega
   imediata ao sink (o `EventBus`) e marca `DELIVERED`. Falha de entrega não
   levanta exceção: o evento já é durável, `mark_failed` agenda retry
   exponencial e, após `max_attempts`, a entrada vai para dead-letter. O pump
   em background (`start`/`stop`, iniciado por `serve`) reprocessa pendências
   no próximo ciclo ou após reinício. `EventRecorder(..., fabric=...)` roteia
   `record()` pelo fabric; sem fabric, o caminho legado é preservado para
   bibliotecas e testes isolados.

3. **Retry e dead-letter com identidade preservada.**
   `requeue_from_dead_letter` reenfileira o mesmo `event_id` e mantém o
   `entry_id` original do outbox, mesmo quando a linha do outbox precisa ser
   recriada. Consumidores idempotentes reconhecem a reentrega como o mesmo
   fato; não há novo `event_id` para contornar constraint.

4. **`IdempotentConsumer` com efeito at-most-once por consumidor.**
   A tabela `consumer_processed` tem chave `(consumer_id, event_id)` e também
   consulta `dedup_key` dentro de uma janela. O marcador só é gravado depois do
   handler bem-sucedido; falha permite nova tentativa. A garantia é
   at-least-once na entrega e at-most-once no efeito por consumidor **no
   processo único atual**. Claim transacional entre processos concorrentes
   pertence ao Control Plane (T4).

5. **Rust: `EventFabric` como fachada `store.append → bus.publish`.**
   `events::fabric::EventFabric` compartilha `EventStore` e `EventBus` com o
   bootstrap e o IPC. `emit` só publica quando o store inseriu o evento novo;
   duplicata não é republicada. O `Event` do wire ganhou
   `#[serde(deny_unknown_fields)]`, rejeitando campos derivados como
   `dedup_key`. O daemon passa a emitir `system.started`, heartbeat e
   `system.stopped` pelo fabric, não por `store.append` + `bus.publish`
   separados.

6. **IPC Rust com subscribe/publish reais e cleanup.**
   `IpcServer::spawn` faz o bind antes de retornar; cada cliente tem thread de
   leitura, canal de saída e thread de forwarding. `Subscribe` valida o padrão
   com `Pattern::new` antes de chamar o barramento (sem panic), `Unsubscribe` e
   desconexão removem as assinaturas. `Event` usa `fabric.emit`; `Ack` reflete
   a aceitação durável, e duplicatas não geram segundo `Delivered`.

7. **Bridge Python síncrona sobre newline-JSON.**
   `nexus_core/event_bridge.py` fala o mesmo protocolo do daemon
   (`{"type": ..., "body": {...}}`), valida o contrato v1.0.0 antes de publicar
   e reconstrói `Event` a partir de `Delivered`. Timeouts, frames inválidos e
   `Ack` divergente falham de forma explícita. A bridge é uma fronteira
   operacional/teste; o runtime Python continua usando o barramento em
   processo. Ela não abre porta de rede nem adiciona autenticação — isso
   continua bloqueado até o Control Plane.

---

## Consequências

- O risco de dois bancos do ADR 0012 está fechado para o runtime: evento e
  entrada do outbox são atômicos no mesmo SQLite. Bancos de outbox antigos e
  separados não são migrados automaticamente; eles eram estado efêmero de
  entrega, não fatos auditáveis.
- `schema_version` sobe para 4. Bancos existentes recebem as tabelas via
  migração idempotente; o outbox também migra colunas antigas
  (`metadata`, `dedup`, severidade/correlação na dead-letter).
- `EventRecorder` mantém a regra “persistir antes de publicar”, agora com retry
  durável. A entrega ao `EventBus` é at-least-once; consumidores com efeitos
  externos precisam de `IdempotentConsumer` (ou de claim no Control Plane).
- O daemon Rust ganha a mesma fachada de emissão, mas **não** ganha outbox
  durável nem offsets/acks por assinante. O `EventBus` continua em memória e
  um cliente desconectado perde eventos. Isso é explicitamente adiado para o
  T4, quando o Control Plane passar a ser autoridade.
- A bridge é local (UDS) e sem autenticação própria; qualquer processo do mesmo
  usuário pode conectar. Não é uma API remota e não deve ser exposta como tal.
- O modo `NEXUS_DAEMON_ONCE` aceita `NEXUS_DAEMON_HOLD_MS` para permitir testes
  de integração com clientes IPC antes do encerramento gracioso.

---

## Validação

- `tests/test_event_fabric_runtime.py`: 15 testes cobrindo emissão atômica,
  rollback do store quando o enqueue falha, dedup sem republicação, retry com
  backoff, dead-letter, requeue preservando identidade (inclusive recriando a
  linha do outbox), migração de outbox legado sem coluna `dedup`, recuperação
  após reinício, consumidor idempotente (evento/chave, falha sem marcar,
  assinatura no bus), recorder e pump.
- `tests/test_event_fabric.py`: 16 testes de compatibilidade do outbox
  standalone (enqueue, status, retry, dead-letter, stats, requeue).
- `tests/test_event_bridge.py`: 9 testes — 8 com `FakeDaemon` (publish, health,
  subscribe/delivered, frame fora de ordem preservado, Ack divergente, frame de
  erro, timeout, socket ausente, uso sem conexão) e 1 com o daemon Rust real em
  modo once, validando publish, subscribe, dedup e health pela bridge, além da
  persistência no SQLite do daemon.
- `core/nexus-daemon`: 39 testes `cargo test` passando, incluindo fabric,
  roundtrip IPC publish/subscribe/dedup, padrão inválido sem panic e rejeição
  de `dedup_key` no wire.
- Suíte Python completa e contagens finais registradas em `README.md` e
  `docs/NEXUS_MASTER_ARCHITECTURE.md`.

---

## Próximo passo

T3 — Knowledge Graph. O T4 deve substituir o outbox em memória do Rust por
outbox durável com offsets/acks por assinante, promover os contratos de
autorização a produção no IPC e, só então, mover o gate para o Control Plane
(T5–T6).
