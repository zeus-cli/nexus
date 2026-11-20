# NEXUS Architecture Decisions

**Versão:** 1.7
**Atualizado:** 2026-11-20
**Projeto:** NEXUS — Cognitive Operating System  
**Escopo:** Registro de decisões arquiteturais (ADRs) — contexto, problema, alternativas, decisão, consequências, implicações de segurança e migração.

> **Como usar este índice:** cada título abre o ADR completo em `docs/adr/`.
> Os ADRs 0001–0013 preservam o contexto e as contagens da data original.
> Consulte `ROADMAP.md` e a arquitetura mestre para o estado atual.

Os registros recentes acompanham as entregas por etapa:

- **T0–T1 (ADR 0015–0016):** Execution Gate no runtime e contratos Python ↔ Rust.
- **T2–T5 (ADR 0017–0020):** Event Fabric, Knowledge Graph, IPC e paridade de
  policy/aprovação no Rust.
- **T6 (ADR 0021):** autoridade final de execução no daemon, persistência e
  fail-closed, opt-in.
- **T7 (ADR 0022):** decisões baseline de host, rede e backup; validação no Acer
  Nitro V15 pendente.
- **T8 (ADR 0023):** catálogo de identidade e escopos, com integração gradual.

---

## ADR 0001 — Fundação local-first (Fase 1) — Python

**Status:** aceito | **Data:** 2026-11-03  
**Escopo:** configuração, logging, SQLite, event store, event bus, serviço

### Contexto
Antes de qualquer LLM ou agente, é necessário um substrato confiável: configuração explícita, observabilidade, persistência durável e canal de eventos desacoplado.

### Decisões
1. **Fase 1 sem dependências externas** — apenas stdlib (`sqlite3`, `tomllib`, `asyncio`, `logging`, `argparse`). FastAPI/Pydantic entram na Fase 2 quando `python3-venv` estiver disponível.
2. **Uma conexão SQLite serializada por processo** — WAL, `synchronous=NORMAL`, `busy_timeout=5000`, protegida por `RLock`.
3. **Migrações versionadas em tabela** — `schema_migrations(version, name, applied_at)`, cada migração em `BEGIN IMMEDIATE`/`COMMIT` com `ROLLBACK`.
4. **Identidade e idempotência de evento** — `event_id` UUID4; `dedup_key = sha256(kind, source, severity, payload)` com janela configurável.
5. **Persistir antes de publicar** — `EventRecorder` grava no store e só então publica no bus.
6. **Estrutura mínima, sem módulos vazios** — apenas diretórios com conteúdo real.
7. **Serviço residente mínimo** — `nexus run` (heartbeat, retenção, `system.started`/`system.stopped`).

### Implicações de segurança
Sem autenticação na Fase 1 (serviço não abre porta). Token e permissões entram na Fase 2.

### Critérios de conclusão (atendidos)
Testes verdes, `nexus status` reporta `ok`, dedup comprovado, backup consistente.

---

## ADR 0002 — Backend local (Fase 2) — Python

**Status:** aceito | **Data:** 2026-11-03  
**Depende de:** ADR 0001

### Contexto
Expor o runtime para HUD/CLI sem abrir autoridade: a API apenas lê e registra.

### Decisões
1. **FastAPI + uvicorn, um processo, loopback** — `127.0.0.1:8765`, `server_header=False`, `/docs`/`redoc`/`openapi` desabilitados.
2. **Token local com permissões derivadas da autonomia** — token em `data/secrets/api_token` (0600), `hmac.compare_digest`, escopos read/write/execute vinculados ao nível de autonomia.
3. **Autenticação WebSocket no primeiro frame** — `{"type":"auth","token":"..."}` em até 5 s, senão fecha com `4401`.
4. **Backpressure explícito** — fila limitada (256) por cliente, descarte de eventos antigos com contagem explícita, 32 clientes máximos.
5. **Manutenção dentro do processo da API** — loop de heartbeat/retenção hospedado na API.
6. **Auditoria de acesso negado com throttle** — `security.api.denied`, throttle de 2 s.

### Implicações de segurança
Token em texto plano no disco (aceitável em loopback); sem TLS (loopback apenas); sem rate limit geral (apenas negações).

---

## ADR 0003 — Dados locais, bind da API e retenção — Python

**Status:** aceito | **Data:** 2026-11-03  
**Substitui parcialmente:** defaults operacionais do ADR 0001

### Decisões
1. **Arquivos privados por padrão** — `0700` dirs, `0600` files; symlinks recusados.
2. **API somente em loopback** — `api.host` deve ser IP de loopback; sem TLS.
3. **Retenção desativada inicialmente** — `retention_days = 0`; poda explícita via `nexus db purge`.

---

## ADR 0004 — Core Foundation: nexus-daemon (Fase 1) — Rust

**Status:** aceito | **Data:** 2026-11-07  
**Escopo:** `core/nexus-daemon/` — Control Plane em Rust (Tokio)

### Decisões
1. **`nexus-daemon` como pacote Rust independente** em `core/nexus-daemon/`, separado de `core-rust/nexus-sentinel`.
2. **Tokio sem feature `unix`** — IPC/UDS via `std::os::unix::net` blocking em threads dedicadas; sinais de SO não tratados via `tokio::signal`; `NEXUS_DAEMON_ONCE=1` para testes.
3. **Event Fabric:** barramento em memória (`EventBus`) + persistência SQLite WAL (`EventStore`), compatível com o contrato Python.
4. **Persistir antes de publicar** — mesma regra do Python.
5. **Segurança Fase 1 mínima** — Security Guard, Policy Engine (Allow/RequireApproval/Deny), Execution Gate delegado; completo na Fase 3.
6. **Configuração TOML com validação estrita** — espelho do `config.py`.

### Implicações de segurança
Decisões de segurança determinísticas, independentes de LLM. Sem encriptação de dados em repouso na Fase 1 (mesmo que o Python).

### Consequências
Control Plane funcional, independente de LLM. Sinais de SO não tratados (encerramento via kill); supervisão de processos sem atualização assíncrona de status (placeholder).

---

## Índice de ADRs

| ADR | Título | Status |
|-----|--------|--------|
| [0001](adr/0001-fase-1-fundacao.md) | Fundação local-first (Fase 1) — Python | aceito |
| [0002](adr/0002-fase-2-backend.md) | Backend local (Fase 2) — Python | aceito |
| [0003](adr/0003-seguranca-e-retencao-local.md) | Dados locais, bind da API e retenção — Python | aceito |
| [0004](adr/0004-fase-1-core-foundation-daemon.md) | Core Foundation: nexus-daemon (Fase 1) — Rust | aceito |
| [0005](adr/0005-fase-2-cognitive-foundation-engines.md) | Cognitive Foundation: motores cognitivos discretos (Fase 2) — Python | aceito |
| [0006](adr/0006-fase-3-secure-agency.md) | Secure Agency: Execution Gate, Rollback, SudoBroker, Execution Journal (Fase 3) — Python | aceito |
| [0007](adr/0007-fase-4-multi-agent-intelligence.md) | Multi-Agent Intelligence (Fase 4) — Python | aceito |
| [0008](adr/0008-fase-5-universal-simulation.md) | Universal Simulation (Fase 5) — Python | aceito |
| [0009](adr/0009-fase-6-unified-experience-web-dashboard.md) | Unified Experience: Web Dashboard (Fase 6) — Python | aceito |
| [0010](adr/0010-fase-7-distributed-adaptive-ecosystem.md) | Distributed & Adaptive Ecosystem (Fase 7) — Python | aceito |
| [0011](adr/0011-fase-7-integration-layer.md) | NEXUS Integration Layer (Fase 7) — Python | aceito |
| [0012](adr/0012-fase-7-event-fabric-outbox-deadletter.md) | Event Fabric: Outbox e Dead-Letter Queue (Fase 7+) — Python | aceito |
| [0013](adr/0013-fase-7-distributed-network-websocket.md) | Distributed Network: WebSocket entre nós (Fase 7+) — Python | aceito |
| [0014](adr/0014-endurecimento-auditoria-2026-11-12.md) | Endurecimento pós-auditoria: SQLite, Event Fabric, VectorStore e CLI | aceito |
| [0015](adr/0015-execution-gate-runtime-obrigatorio.md) | Execution Gate no runtime e ciclo authorize → executar → commit | aceito |
| [0016](adr/0016-contratos-python-rust-v1.md) | Contratos Python ↔ Rust v1.0.0 | aceito |
| [0017](adr/0017-event-fabric-unico-t2.md) | Event Fabric único: outbox e bridge Python ↔ Rust (T2) | aceito |
| [0018](adr/0018-knowledge-graph-persistente-t3.md) | Knowledge Graph persistente (T3) | aceito |
| [0019](adr/0019-ipc-controle-t4.md) | IPC de controle no daemon Rust (T4) | aceito |
| [0020](adr/0020-paridade-policy-approval-t5.md) | Paridade de Policy Engine e Approval Manager (T5) | aceito |
| [0021](adr/0021-execution-gate-autoritativo-t6.md) | Execution Gate autoritativo no Rust (T6) | aceito |
| [0022](adr/0022-host-principal-rede-backup-t7.md) | Host principal, rede e backup baseline (T7) | aceito; validação no host pendente |
| [0023](adr/0023-identity-permission-manager-t8.md) | Identity & Permission Manager (T8) | aceito |
