# ADR 0004 — Core Foundation: nexus-daemon (Fase 1)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `core/nexus-daemon/` — Control Plane em Rust (Tokio): ciclo de vida de serviços, IPC por Unix Domain Socket, Event Fabric (barramento + persistência), health check, logging estruturado e segurança mínima.
- **Depende de:** ADR 0001–0003 (fundação Python existente)

---

## Contexto

A Fase 0 (audit) confirmou que o repositório já contém um backend Python funcional (`nexus_core` v0.2.0) com event store SQLite, barramento de eventos, LLM gateway, policy engine, approval manager, sandbox bwrap e API FastAPI. O que falta — e é o maior gap arquitetural identificado — é o **Control Plane**: um serviço Rust persistente e independente de LLM que exponha autoridade operacional, gerencie o ciclo de vida de serviços e roteie eventos entre componentes.

A especificação mestre (seção 6) exige um `nexus-daemon` em Rust + Tokio que:

* permaneça operacional mesmo com o provedor LLM indisponível;
* não dependa de LLM para decisões de segurança;
* use Unix Domain Sockets para IPC local;
* expõe health checks, supervisão de processos, encerramento gracioso e recuperação de falhas.

---

## Decisão 1 — `nexus-daemon` como pacote Rust independente em `core/nexus-daemon/`

Criado um novo pacote Cargo em `core/nexus-daemon/`, separado do existente `core-rust/nexus-sentinel` (que permanece como coletor de telemetria). Motivos:

* Evita mexer no build existente do sentinel (3 testes verdes).
* Permite iterar o daemon sem tocar no sentinel.
* O sentinel continua sendo um produtor de eventos que se conecta ao daemon via UDS (mesmo protocolo JSON por linha).

O `core-rust/` poderá ser absorvido num workspace Cargo em uma fase posterior, quando houver múltiplos binários Rust compartilhando dependências.

---

## Decisão 2 — Tokio sem o feature `unix` (limitação do ambiente de build)

O índice de crates disponível neste ambiente não expõe o feature `unix` do `tokio` (necessário para `tokio::net::UnixListener`, `tokio::signal::unix` e `tokio::process`). Para contornar:

* **IPC/UDS:** implementado com `std::os::unix::net::{UnixListener, UnixStream}` (blocking std) executado em threads dedicadas (`std::thread::spawn`), com `tokio::sync::mpsc`/`std::sync::mpsc` para passagem de mensagens entre threads.
* **Sinais do SO (SIGTERM/SIGINT):** não tratados via `tokio::signal` (indisponível). O daemon roda até ser encerrado pelo processo; uma variável `NEXUS_DAEMON_ONCE=1` executa um ciclo de heartbeat e sai (útil para testes).
* **Supervisão de processos:** `std::process::Command` em `tokio::task::spawn_blocking`; o status do serviço é rastreado no supervisor, mas a atualização assíncrona de status a partir de `spawn_blocking` fica como placeholder para quando o tokio `unix`/`process` estiver disponível.

Consequência: o protocolo de IPC (JSON por linha) é idêntico ao do sentinel Rust existente, garantindo interoperabilidade com o Python e com futuros clientes Rust.

---

## Decisão 3 — Event Fabric: barramento em memória + persistência SQLite

O `EventBus` (memória) oferece pub/sub com padrões `kind` exato, `prefixo.*` e `*`, ordem preservada por assinante, isolamento de falhas de handler e nenhum bloqueio do publicador. O `EventStore` (SQLite WAL, uma conexão serializada por `Mutex`) espelha o schema do Python (`event_id, kind, source, severity, payload, dedup_key, correlation_id, created_at, recorded_at`) com dedup por janela temporal.

Regra arquitetural (mesmo do Python): **persistir antes de publicar**. Eventos publicados via IPC são gravados no store e, quando inseridos, repassados ao barramento.

Dedup: SHA-256(kind, source, severity, payload canônico) com janela configurável — idêntico à implementação Python (`events.Event.dedup_key`).

---

## Decisão 4 — Estrutura interna do daemon

```
core/nexus-daemon/src/
├── main.rs          # entry point Tokio, heartbeat, shutdown, IPC + supervision threads
├── bootstrap.rs     # sequência obrigatória: config → logging → store → bus → serviços
├── config.rs        # TOML com validação estrita (espelho do Python config.py)
├── logging.rs       # logging estruturado (tracing)
├── events/
│   ├── mod.rs       # re-exports: EventBus, EventStore, Event, EventKind, Severity, EventError
│   ├── types.rs     # contrato de evento (compatível com Python events.py)
│   ├── bus.rs       # barramento em memória (pub/sub com padrões)
│   └── store.rs     # persistência SQLite WAL com dedup
├── ipc/
│   ├── mod.rs       # re-export: IpcServer
│   ├── protocol.rs  # frames JSON (IpcFrame / OutgoingFrame)
│   └── server.rs    # servidor UDS blocking-std em threads dedicadas
├── supervisor.rs    # ciclo de vida de serviços filhos (registrar/start/stop/list)
├── security/
│   └── mod.rs       # Security Guard (identidade de componentes, confiança)
├── policy/
│   └── mod.rs       # Policy Engine (Allow / RequireApproval / Deny, determinístico)
├── execution/
│   └── mod.rs       # Execution Gate Fase 1 (delegado à Policy Engine)
├── resources/
│   └── monitor.rs   # monitor de RAM/CPU/swap via /proc (espelho do sentinel)
├── health/
│   └── check.rs     # health check (ok / degraded)
└── recovery/
    └── mod.rs       # recuperação de serviços com falha
```

---

## Decisão 5 — Segurança Fase 1 (expansa na Fase 3)

O `Security Guard`, `Policy Engine` e `Execution Gate` da Fase 1 são versões mínimas:

* `SecurityGuard`: registro de componentes confiáveis e identidade (`trusted` flag).
* `PolicyEngine`: decisão determinística por nível de risco (`low` → Allow, `medium/high` → RequireApproval, `critical` → Deny), sem dependência de LLM.
* `ExecutionGate`: aplica a decisão da `PolicyEngine`.

Na Fase 3 estes são substituídos pelo Execution Gate completo (identidade → capacidade → política → risco → aprovação → recurso → execução isolada → verificação → commit → auditoria).

---

## Decisão 6 — Configuração TOML, validação estrita

Espelho do `config.py` Python: seções `[nexus]`, `[paths]`, `[logging]`, `[events]`, `[ipc]`, `[services]`, `[health]`. Chaves desconhecidas são erro (não aviso). Níveis de log validados. Caminhos relativos resolvidos a partir do diretório raiz.

---

## Consequências

* **Positivo:** Control Plane Rust funcional, independente de LLM, com event fabric compatível com o Python, health check e ciclo de vida de serviços.
* **Negativo (fase 1):** sinal de SO não tratado (encerramento via kill), supervisão de processos sem atualização assíncrona de status, delivery de eventos publicado via IPC para assinantes locais ainda não integrado ao barramento assíncrono (store grava, entrega via bus fica para Fase 2).
* **Risco conhecido:** sem `tokio::unix`, o IPC usa threads std; desempenho adequado para uso local, mas não é o caminho idiomático Tokio. Será revisado quando o ambiente de build permitir o feature `unix`.

---

## Testes

* 27 testes unitários passando (event bus, event store, tipos de evento, config, bootstrap, health, resources, security, policy, execution, supervisor, recovery, logging, IPC protocol).
* Build sem erros; 50 avisos (majormente dead code de API superfície prevista para fases 2+).

---

## Próximos passos (Fase 1 → Fase 2)

* Integrar o runtime Python como serviço supervisionado do daemon.
* Estender o Event Fabric com transactional outbox e dead-letter queue.
* Substituir Policy/Execution Gate pelas versões completas (Fase 3).
