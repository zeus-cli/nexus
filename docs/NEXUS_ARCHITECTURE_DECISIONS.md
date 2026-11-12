# NEXUS Architecture Decisions

**Versão:** 1.1
**Atualizado:** 2026-11-12
**Projeto:** NEXUS — Cognitive Operating System  
**Escopo:** Registro de decisões arquiteturais (ADRs) — contexto, problema, alternativas, decisão, consequências, implicações de segurança e migração.

> **Nota:** os ADRs 0001–0013 são registros históricos e preservam as contagens de testes da data original. O ADR 0014 consolida o endurecimento da auditoria de 2026-11-12 e o estado atual de validação.

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
| 0001 | Fundação local-first (Fase 1) — Python | aceito |
| 0002 | Backend local (Fase 2) — Python | aceito |
| 0003 | Dados locais, bind da API e retenção — Python | aceito |
| 0004 | Core Foundation: nexus-daemon (Fase 1) — Rust | aceito |
| 0005 | Cognitive Foundation: motores cognitivos discretos (Fase 2) — Python | aceito |
| 0006 | Secure Agency: Execution Gate, Rollback, SudoBroker, Execution Journal (Fase 3) — Python | aceito |
| 0007 | Multi-Agent Intelligence: Agent Registry, Runtime, Orchestrator, Collaboration, Adversarial Review (Fase 4) — Python | aceito |
| 0008 | Universal Simulation: Simulation Subsystem (Fase 5) — Python | aceito |
| 0009 | Unified Experience: Web Dashboard aprimorado (Fase 6) — Python | aceito |
| 0010 | Distributed & Adaptive Ecosystem (Fase 7) — Python | aceito |
| 0011 | NEXUS Integration Layer (Fase 7 - Integração Final) — Python | aceito |
| 0012 | Event Fabric: Transactional Outbox e Dead-Letter Queue (Fase 7+) — Python | aceito |
| 0013 | Distributed Network: WebSocket entre nós (Fase 7+) — Python | aceito |
| 0014 | Endurecimento pós-auditoria: SQLite, Event Fabric, VectorStore e CLI — Python | aceito |
