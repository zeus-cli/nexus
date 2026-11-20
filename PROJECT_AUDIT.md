# NEXUS Project Audit

**Original audit date:** 2026-11-03
**Updated:** 2026-11-20
**Workspace:** `/home/nexus/Documentos/nexus`

> A auditoria abaixo registra o estado observado até 2026-11-12. O snapshot no
> início deste documento atualiza o progresso posterior. Para o estado
> operacional, consulte `ROADMAP.md` e `docs/NEXUS_MASTER_ARCHITECTURE.md`.

## 0. Current status — 2026-11-20

- **T6:** Execution Gate autoritativo implementado no daemon Rust. O modo segue
  opt-in e está desativado no `config/nexus.toml` do workspace.
- **T7:** em andamento. O Dell Inspiron 15 3511 é a estação de desenvolvimento;
  o Acer Nitro V15 é o servidor alvo. As units e o backup têm artefatos no
  repositório, mas a instalação, a rede, a GPU/Ollama e o restore ainda não
  foram validados no Acer.
- **T8:** T8.1–T8.4 têm catálogo, store Rust, IPC e bootstrap/CLI local do owner.
  Pairing de dispositivos, integração do gate com principals e clientes ainda
  não estão concluídos.
- **Limites atuais:** API com token local único e bind de loopback; sete
  ferramentas de automação somente leitura; sem namespaces de memória por
  pessoa/dispositivo.
- **Validação:** o último registro de suíte completa é o T6, em 2026-11-16
  (516 testes Python + 12 subtests; 86 testes Rust no daemon). A revisão atual
  do workspace não tem uma execução completa registrada.

Consulte o [plano T7](docs/NEXUS_T7_EXECUTION_PLAN.md), o
[design T8](docs/NEXUS_T8_IDENTITY_DESIGN.md) e o
[registro de dívida técnica](docs/NEXUS_TECHNICAL_DEBT.md).

## 1. Historical audit snapshot — 2026-11-12

NEXUS is no longer only a prototype. It contains a functioning local-first cognitive layer with:

- six discrete cognitive engines and an adaptive cognitive loop;
- hybrid lexical/semantic memory (SQLite FTS5 + LanceDB + RRF);
- a risk-based Execution Gate with HITL approvals, rollback, SudoBroker and an execution journal;
- multi-agent intelligence, universal simulation and distributed resource/network components;
- event fabric with transactional outbox, dead-letter queue and WebSocket propagation;
- CLI, TUI and a multi-panel Web HUD;
- a Rust telemetry sentinel and an activated user-level systemd service.

The remaining gaps are narrower: native GTK4, branding remnants (`zeus`), a read-only tool catalog, optional task encryption that is disabled by default, synchronous SQLite calls in async paths, and support for very large local models on 12 GB RAM.

## 2. Component Analysis

| Component | Status | Implementation Detail | Remaining Gap |
|---|---|---|---|
| **Cognitive Core** | Implemented | `cognitive.py` adaptive loop + six engines | Deeper planner/decision integration in the main loop |
| **LLM Gateway** | Implemented | Ollama + OpenAI-compatible, circuit breaker, fallback, context budget | Deterministic planner when no LLM is available |
| **Persistent Memory** | Implemented | SQLite WAL + FTS5 + LanceDB hybrid search with RRF | Procedural/reflective memory categories |
| **Rust Sentinel** | Implemented | `core-rust/` telemetry over Unix socket | Systemd units active; broader integration tests |
| **Automation** | Implemented but narrow | `AutomationEngine` applies `PolicyEngine`/HITL directly; `ExecutionGate`, Tool Registry, Rollback, SudoBroker and Journal are available | Gate not injected by bootstrap (TD-015); catalog still 7 read-only diagnostics |
| **Intelligence Plane** | Implemented | Registry, runtime, dispatcher, orchestrator, collaboration, adversarial review | LLM-backed specialist agents |
| **Simulation Plane** | Implemented | Worlds, clocks, rules, scenarios, experiments, metrics, analyzer | Agent execution inside simulated worlds |
| **Distributed Plane** | Partial | Node manager, resource scheduler, experience consolidation, self-improvement, WebSocket network | General scheduler not wired to runtime; persistence of distributed tasks |
| **Interfaces** | Implemented | CLI, TUI, Web HUD, API | Native GTK4 tray/shell |
| **Security** | Implemented | Loopback API, token scopes, SHA-256 approvals, bwrap, egress guard | No TLS (by design), token in plaintext, sandbox UID 0 |

## 3. Verification State (2026-11-12)

- **Python tests:** 305 passed, 12 subtests passed.
- **Static checks:** `ruff` fatal/undefined-name rules clean; `compileall` clean; `node --check` clean; `git diff --check` clean.
- **Rust:** not re-run in the 2026-11-12 audit; the sentinel previously had 3 unit tests passing and the daemon build is environment-dependent.
- **LLM:** local completion through `nexusorbe-llama3.1:latest` succeeded; OpenCode remote models validated separately.
- **Hardware:** host has 12 GB RAM. Local 3B/8B models are viable; Gemma 4 12B/31B is not practical locally without a larger host or GPU.

## 4. Findings from the Original Audit

| Original finding | Current status |
|---|---|
| **Branding schism (`zeus` vs `nexus`)** | **Open.** Logger root and several logger names still use `zeus`; tracked as TD-001/TD-010. |
| **Cognitive gap** | **Resolved.** Six cognitive engines, multi-agent plane and integration layer exist. |
| **Deployment gap** | **Resolved.** `nexus-core.service` and `nexus-sentinel.service` are active and enabled. |
| **Semantic memory missing** | **Resolved.** LanceDB vector store and hybrid RRF search are implemented and tested. |
| **No vector embeddings** | **Resolved.** `sentence-transformers` + LanceDB with graceful degradation. |
| **Broad automation missing** | **Partially resolved.** Execution Gate, Tool Registry, Rollback, SudoBroker and Journal exist; the catalog remains read-only. |
| **No native desktop GUI** | **Open.** Web HUD is the current desktop interface; GTK4 is deferred. |

## 5. 2026-11-12 Hardening Update

The audit fixed real defects across LLMs, connections, routes, database access, backend, frontend and syntax:

- CLI global options now work before/after nested subcommands.
- API approval-history route ordering and scenario paradigm validation corrected.
- Event fabric canonical payloads, legacy envelope reading, dead-letter migration and metadata handling corrected.
- Execution journal integrity hash recalculated on status updates.
- Simulation Results Store timestamps/reconstruction/integrity corrected.
- SQLite access centralized in `nexus_core/sqlite_utils.py` with `busy_timeout`.
- Task Store atomic writes, empty-key handling and UTF-8 reads corrected.
- VectorStore LanceDB API compatibility, SQL escaping and idempotent upsert corrected.
- Distributed network event timestamps/dedup and heartbeat clock corrected.
- Web HUD security panel now loads and decides pending approvals.
- `config/nexus.toml` aligned with the Ollama models actually installed locally.

See `docs/adr/0014-endurecimento-auditoria-2026-11-12.md` for the full decision record.

## 6. Critical Findings

1. **Branding remnants:** `zeus` remains in logger names and documentation; this is operational noise, not a functional blocker.
2. **Read-only automation catalog:** the secure machinery is implemented, but only seven diagnostic tools are registered.
3. **Optional encryption:** Task Store supports Fernet/XOR, but the runtime does not supply a key by default.
4. **Synchronous SQLite in async paths:** `busy_timeout` mitigates contention but does not remove event-loop blocking.
5. **Sandbox UID 0:** bwrap namespaces provide isolation, but an unprivileged in-namespace UID is still preferable.
6. **LLM dependency:** the gateway has provider fallback, but no deterministic planner exists if every LLM is unavailable.

## 7. Recommendation

**READY WITH DOCUMENTED LIMITATIONS.** The security-critical paths are implemented and tested. The remaining work is desktop experience, branding cleanup, broader tool coverage, asynchronous database access and optional-at-rest encryption.
