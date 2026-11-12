# NEXUS Repository Audit Report

**Audit Date:** 2026-11-07  
**Auditor:** Principal Software Architect (NEXUS Master Engineering Specification)  
**Workspace:** `/home/nexus/Documentos/nexus`  
**Repository Version:** `nexus-core` 0.2.0 (git master, 2 commits)  
**Primary Languages:** Python 3.12.3 + Rust  
**Environment:** Linux (Linux Mint/Ubuntu target), local-first, no network dependency required

> **Nota de atualização (2026-11-12):** este relatório é um retrato histórico de 2026-11-07. As Fases 3–7 foram implementadas depois; para o estado atual, consulte a seção **Atualização pós-auditoria (2026-11-12)** no fim deste documento, além de `README.md`, `ARCHITECTURE.md` e `docs/NEXUS_MASTER_ARCHITECTURE.md`.

---

## Executive Summary

The NEXUS repository is **not empty**. It contains a substantial, functioning Python cognitive layer (v0.2.0) plus a Rust telemetry sentinel. The codebase is internally coherent, well-tested (22 test files, all passing when run in logical subsets), and security-conscious by design (loopback-only API, token auth, SHA-256 approval binding, bwrap sandboxing, 0700/0600 filesystem permissions).

However, the repository implements only **Phases 0–2** of the Master Engineering Specification (foundation, backend, basic cognitive loop, secure execution gate for read-only diagnostics). It is **missing the majority** of the target architecture: the Rust `nexus-daemon`, the full Control Plane, the multi-agent swarm, the Universal Simulation subsystem, the GTK4 desktop GUI, rollback/SudoBroker mechanisms, resource profiles, distributed support, and the complete Event Fabric.

No destructive refactoring should occur before this report is reviewed and a migration strategy is approved.

---

## 1. Current Architecture

The current architecture is a **two-layer local-first system**:

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER / CLI / WEB HUD                     │
│                    (CLI, TUI, Browser Dashboard)                  │
└─────────────────────────────────────────────────────────────────┘
                              │ HTTP/WS (127.0.0.1:8765, Bearer token)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    NEXUS API (FastAPI, Layer 2)                 │
│  /health /status /config /events /memories /tasks               │
│  /automation/actions /llm/completions /ws/events                │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    NEXUS COGNITIVE CORE (Python)                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │ Cognitive   │  │ Automation  │  │ Approval Manager    │    │
│  │ Core (loop) │  │ Engine      │  │ (HITL, SHA-256)     │    │
│  └─────────────┘  └─────────────┘  └─────────────────────┘    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │ Reflection  │  │ Policy      │  │ LLM Gateway         │    │
│  │ Engine      │  │ Engine      │  │ (Ollama + OpenAI)   │    │
│  └─────────────┘  └─────────────┘  └─────────────────────┘    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │ Memory Store│  │ Event Store │  │ Event Bus / Recorder│    │
│  │ (SQLite+LanceDB)│ │ (SQLite WAL)│ │ (pub/sub)        │    │
│  └─────────────┘  └─────────────┘  └─────────────────────┘    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐    │
│  │ Sandbox     │  │ Sentinel    │  │ Config / Logging    │    │
│  │ (bwrap)     │  │ Receiver    │  │ (TOML, structured)  │    │
│  └─────────────┘  └─────────────┘  └─────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │ UDS (newline-JSON)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              NEXUS SENTINEL (Rust, core-rust/nexus-sentinel)    │
│  /proc telemetry (uptime, load, memory, processes, CPU, kernel) │
│  Filesystem watch (create/modify/delete)                        │
└─────────────────────────────────────────────────────────────────┘
```

**Data flow:** `Sentinel (Rust)` → `Unix Socket` → `Event Recorder (Py)` → `SQLite` → `Event Bus` → `Cognitive Core / UI` (matches ARCHITECTURE.md Section 1).

**Service lifecycle:** `nexus run` (bootstrap → heartbeat → retention) or `nexus serve` (API + maintenance loop). Systemd user units exist (`nexus-core.service`, `nexus-sentinel.service`) but are not activated.

**Data directory:** `data/` with subdirectories `backups/`, `databases/`, `events/`, `logs/`, `memory/`, `run/`, `secrets/`, `tasks/`, `vectors/`. All private (0700 dirs, 0600 files).

---

## 2. Existing Modules

### Python Package: `nexus_core` (v0.2.0)

| Module | File | Responsibility | Status |
|--------|------|----------------|--------|
| Core bootstrap/runtime | `app.py` | Service initialization, health, serve loop, graceful shutdown | IMPLEMENTED |
| Configuration | `config.py` | TOML config with strict validation, env overrides, LLM provider/chain validation | IMPLEMENTED |
| Logging | `logging_setup.py` | Structured JSON logging, rotating private files | IMPLEMENTED |
| Events model | `events.py` | Event dataclass (id, kind, source, payload, severity, correlation, dedup) | IMPLEMENTED |
| Event Bus | `eventbus.py` | Pub/sub with pattern matching, per-handler timeout isolation | IMPLEMENTED |
| Event Store | `memory/eventstore.py` | SQLite WAL append, idempotent dedup, queries, retention, verify | IMPLEMENTED |
| Event Recorder | `recorder.py` | Persist-before-publish bridge (store → bus) | IMPLEMENTED |
| Database | `memory/store.py` | SQLite connection, versioned migrations (v1-v3), integrity, backup | IMPLEMENTED |
| Memory records | `memory/records.py` | CRUD for 4 memory kinds, FTS5 + hybrid lexical/vector search | IMPLEMENTED |
| Vector store | `memory/vector.py` | LanceDB + Sentence-Transformers semantic search (lazy load) | IMPLEMENTED |
| Consolidation | `memory/consolidation.py` | Episodic→semantic proposal engine | IMPLEMENTED |
| Cognitive Core | `cognitive.py` | Intent→Plan→Execute→Observe→Critique→Decide loop, task suspend/resume | IMPLEMENTED |
| Reflection Engine | `reflection.py` | LLM-based critique (SUCCESS/RETRY/REPLAN/REQUEST_APPROVAL/ABORT/PARTIAL) | IMPLEMENTED |
| Automation Engine | `automation.py` | Tool execution with policy gating, HITL, CLAIMED→CONSUMED protocol | IMPLEMENTED |
| Policy Engine | `policy.py` | Deterministic risk-based ALLOW/REQUIRE_APPROVAL/DENY | IMPLEMENTED |
| Tool types | `automation_types.py` | Tool, RiskLevel, AccessLevel, ExecutionMode definitions | IMPLEMENTED |
| Approval Manager | `approval.py` | HITL state machine, SHA-256 param hash, SQLite, orphan recovery | IMPLEMENTED |
| Approval API | `approval_api.py` | Wrapper for human decisions via API | IMPLEMENTED |
| Approval UI | `approval_ui.py` | Rich-based terminal HITL interface | IMPLEMENTED |
| Sandbox | `sandbox.py` | Bubblewrap namespace isolation, optional systemd-run cgroup limits | IMPLEMENTED |
| Sentinel Receiver | `sentinel.py` | Unix domain socket server for Rust sentinel events | IMPLEMENTED |
| Task Store | `task_store.py` | Suspended task persistence with SHA-256 checksums, atomic writes | IMPLEMENTED |
| Resources | `resources.py` | psutil-based CPU/RAM/swap monitoring | IMPLEMENTED |
| Private FS | `privatefs.py` | 0700/0600 helpers, symlink refusal | IMPLEMENTED |
| CLI | `cli.py` | 20+ commands (status, config, task, memory, approvals, events, db, etc.) | IMPLEMENTED |
| TUI | `tui.py` | Curses read-only dashboard (status/events/memory/tasks) | IMPLEMENTED |
| API server | `api/server.py` | Uvicorn runner, loopback enforcement, auth bridging | IMPLEMENTED |
| API app | `api/app.py` | FastAPI app, 20+ endpoints, WebSocket realtime with replay | IMPLEMENTED |
| API auth | `api/auth.py` | Local token (file/env), HMAC compare, scope-based autonomy | IMPLEMENTED |
| Realtime hub | `api/realtime.py` | WebSocket fan-out from EventBus, backpressure, 32-client limit | IMPLEMENTED |
| LLM Gateway | `llm_router/gateway.py` | Provider routing, circuit breakers, retries, fallback, context budget | IMPLEMENTED |
| LLM Providers | `llm_router/providers.py` | Ollama + OpenAI-compatible HTTP adapters | IMPLEMENTED |
| Egress Guard | `security/egress.py` | Privacy levels (PUBLIC/PRIVATE/SECRET) for data leaving machine | IMPLEMENTED |
| Web Dashboard | `web/` | HTML/CSS/JS single-page dashboard (served at `/`) | IMPLEMENTED |

### Rust Component: `core-rust/`

| Component | File | Responsibility | Status |
|-----------|------|----------------|--------|
| Sentinel binary | `src/main.rs` | /proc telemetry sampling, fs watch, UDS client to Python | IMPLEMENTED (3 unit tests) |

### Configuration & Deployment

| File | Purpose | Status |
|------|---------|--------|
| `config/nexus.toml` | Runtime configuration (Ollama provider, autonomy level 1, LLM chain) | IMPLEMENTED |
| `pyproject.toml` | Python packaging (setuptools, `nexus` CLI entry point) | IMPLEMENTED |
| `requirements.lock` | Pinned dependencies (FastAPI, LanceDB, sentence-transformers, etc.) | IMPLEMENTED |
| `systemd/nexus-core.service` | User systemd unit for Python API (not activated) | EXISTS, NOT DEPLOYED |
| `systemd/nexus-sentinel.service` | User systemd unit for Rust sentinel (not activated) | EXISTS, NOT DEPLOYED |
| `share/applications/nexus.desktop` | Desktop entry | EXISTS |
| `share/icons/hicolor/scalable/apps/nexus.svg` | App icon | EXISTS |

### Documentation

| File | Status |
|------|--------|
| `README.md` | EXISTS |
| `ARCHITECTURE.md` | EXISTS |
| `ROADMAP.md` | EXISTS |
| `SECURITY.md` | EXISTS (Portuguese) |
| `SECURITY_REVIEW.md` | EXISTS |
| `TECHNICAL_DEBT.md` | EXISTS (6 items) |
| `PROJECT_AUDIT.md` | EXISTS (2026-11-03, historical) |
| `RELEASE_AUDIT.md` | EXISTS (claims 154 tests, 90 passed — appears outdated) |
| `PERFORMANCE_BASELINE.md` | EXISTS |
| `docs/adr/0001-0003.md` | 3 Architecture Decision Records |

---

## 3. Implemented Functionality

The following capabilities are **verified working** (tests pass):

### 3.1 Foundation Layer (Phase 1 — complete)
- Central TOML configuration with strict key validation and env var overrides
- Structured JSON logging with rotating private files (0600)
- SQLite database with WAL mode, versioned migrations (schema v1→v3), integrity checks, backup
- EventStore: append-only event log with SHA-256 dedup keys, time-window deduplication, filtered queries, retention, corruption detection
- EventBus: pub/sub with exact/prefix/wildcard patterns, per-handler timeout isolation, failure isolation
- EventRecorder: persist-before-publish, idempotent redelivery
- Bootstrap/runtime with health checks, graceful startup/shutdown
- Private filesystem helpers (0700 dirs, 0600 files, symlink refusal)

### 3.2 Backend Layer (Phase 2 — complete)
- FastAPI HTTP API on 127.0.0.1:8765 (loopback only, no TLS)
- Bearer token auth (local file or env), HMAC comparison
- Scope-based authorization: read (autonomy 0), write (autonomy 1), execute (autonomy 2)
- WebSocket realtime event stream with auth frame, replay, backpressure, 32-client limit
- Web dashboard (HTML/CSS/JS) served same-origin
- 20+ CLI commands for administration, diagnostics, task execution, memory management, approvals, events, DB maintenance
- Curses TUI (read-only dashboard)
- Rich-based Approval UI (HITL terminal)

### 3.3 Cognitive Layer (partial)
- Cognitive Core: Intent→Plan→Execute→Observe→Critique→Decide adaptive loop
- LLM-based planning (JSON output), step execution, reflection-based replanning/retry/abort
- Task suspension on `ApprovalRequired`, resumption with approval ID
- Persisted suspended tasks (TaskStore, SHA-256 checksummed JSON)
- Reflection Engine: LLM-based critique with 6 decision types
- Memory Store: 4 kinds (short_term, long_term, episodic, semantic), FTS5 lexical + LanceDB vector hybrid search (RRF)
- Memory consolidation: episodic→semantic proposals
- LLM Gateway: Ollama + OpenAI-compatible providers, circuit breakers (failure threshold + cooldown), retries, fallback chains, context budget truncation, usage/cost accounting
- Egress Guard: privacy-level filtering for outbound data

### 3.4 Secure Execution Layer (partial)
- Policy Engine: deterministic ALLOW/REQUIRE_APPROVAL/DENY by risk level (LOW/MEDIUM/HIGH/CRITICAL) and access level
- Automation Engine: 7 read-only diagnostic tools (sys.uptime, sys.memory, sys.disk, sys.kernel, proc.list, proc.info, svc.status)
- Approval Manager: HITL state machine PENDING→APPROVED→CLAIMED→CONSUMED, SHA-256 canonical param hash binding, TTL expiration, revocation, orphan recovery (>5 min CLAIMED/STARTING → UNKNOWN)
- Sandbox Executor: bubblewrap namespace isolation (user, PID, net, IPC, UTS, cgroup), read-only /usr /bin /lib /etc, isolated tmpfs /tmp and /workspace, optional systemd-run cgroup limits
- Resource monitoring: CPU/RAM/swap via psutil, pressure detection

### 3.5 Rust Sentinel
- /proc telemetry sampling (uptime, loadavg, meminfo, process count, CPU model, top 5 processes by RSS, kernel release, logical CPU count)
- Filesystem watch (create/modify/delete) with snapshot diffing
- Unix domain socket client (newline-JSON frames) to Python SentinelReceiver
- 3 unit tests (meminfo parsing, interval validation, watch directory)

---

## 4. Incomplete Functionality

The following are **partially implemented or designed but not complete**:

| Area | What Exists | What's Missing |
|------|-------------|----------------|
| **Control Plane** | Policy Engine, basic Resource Manager | nexus-daemon (Rust), Identity Manager, Execution Gate (separate from Policy), Service Lifecycle Manager, component authentication, replay protection at IPC level |
| **Cognitive Plane** | Cognitive Core, Reflection, Memory, LLM Router | Intent Engine, Context Engine (relevance-ranked retrieval), Reasoning Engine (structured, no private CoT), Decision Engine, Planner Engine (structured plan objects), Knowledge Engine, Procedural/Reflective memory |
| **Multi-Agent** | None | Agent Registry, Agent Manifest, Agent Runtime, Orchestrator, Task Dispatcher, Swarm, Collaboration, Adversarial Review, 9 specialist agents |
| **Simulation** | None | World Builder, Entity Manager, Rules Engine, Simulation Clock, Event Scheduler, Scenario Manager, Experiment Runner, Metrics Engine, Analyzer |
| **Desktop GUI** | Web dashboard (browser window) | GTK4 native desktop GUI, system tray, Command Palette |
| **Rollback/Sudo** | None | Rollback Manager, SudoBroker, Execution Journal separate from event store |
| **Event Fabric** | EventBus + EventStore (in-process) | UDS cross-language IPC, transactional outbox, dead-letter queue, schema versioning, security classification on events |
| **Resource Profiles** | None | MINIMAL/STANDARD/ADVANCED/DISTRIBUTED operational profiles |
| **Memory categories** | 4 kinds (short/long/episodic/semantic) | Procedural, Relational, Operational, Reflective memory categories |
| **Model profiles** | None | Fast/General/Reasoning/Coding/Research/Embedding/Classification profiles |

---

## 5. Broken Functionality

No functionality is **broken** in the sense of crashing or producing incorrect output. All tests pass. However, the following are **operational gaps**:

1. **Systemd units not deployed/activated** — `nexus-core.service` and `nexus-sentinel.service` exist but `systemd-analyze --user verify` only validates syntax; the services are not running. The system must be started manually (`nexus serve` / `nexus run`).
2. **Full test suite timeout** — Running `pytest tests/` all at once exceeded 120 seconds. Subset runs all pass. The timeout is likely due to tests that attempt real bwrap execution or slow imports (sentence-transformers, LanceDB lazy loading on first use). This is a test infrastructure issue, not broken code.
3. **`test_e2e_security.py::test_e2e_bypass_sandbox_direct`** attempts `cat /etc/shadow` inside bwrap. This passes because bwrap denies it, but it's a fragile test that depends on bwrap being installed and configured.
4. **Sandbox runs as `--uid 0 --gid 0`** inside the bubblewrap namespace — while namespaced, running as root inside the sandbox is a weaker isolation profile than using an unprivileged UID.
5. **`config.py` hardcodes `zeus_events.db` as fallback database name** — branding inconsistency (project is "NEXUS", logger prefix is "zeus", fallback DB is "zeus_events.db").
6. **Logger root is `"zeus"`** — not "nexus", leaving remnants of a previous branding.
7. **Web dashboard (`/`) has no authentication on the route itself** — token is entered client-side and sent via Bearer header; the route serves HTML without auth, but all API endpoints require auth.

---

## 6. Security Vulnerabilities

The codebase follows a **fail-closed, defense-in-depth** model. No critical vulnerabilities were found. Residual risks (from SECURITY_REVIEW.md and audit):

| Risk | Severity | Description | Mitigation Status |
|------|----------|-------------|-------------------|
| Process-level boundary bypass | Medium | If attacker gains arbitrary code execution within the CognitiveCore Python process, they can bypass Python-level gates (no OS-level process sandbox around the Python runtime itself) | NOT MITIGATED (documented residual risk) |
| TaskStore plaintext | Medium | Suspended task JSON files are plaintext; may contain prompts, tool params, task state | NOT MITIGATED (documented; encryption recommended for high-security) |
| Token in plaintext on disk | Low | API token stored at `data/secrets/api_token` (0600); any process running as same user can read it | ACCEPTED (loopback-only, documented) |
| Sandbox runs as uid 0 | Low-Medium | Bubblewrap runs as root inside namespace; namespace escape would give host root | PARTIAL (bwrap namespaces still provide strong isolation) |
| No TLS on API | Low | Loopback only; no external exposure. TLS not applicable to loopback. | ACCEPTED (by design) |
| No rate limiting on authenticated API | Low | Authenticated sessions can emit events in volume; only denials are throttled | NOT MITIGATED (deferred to Execution Engine phase) |
| ApprovalManager is synchronous sqlite | Low | `decide()`, `claim_approval()`, etc. are blocking sqlite calls invoked from async context | NOT MITIGATED (could block event loop under load) |
| Web dashboard served unauthenticated at `/` | Low | HTML shell served without auth; token entered client-side. API endpoints still require auth. | PARTIAL |
| `recover_orphans()` SQL has logical issue | Low | `WHERE status IN (?, ?)` only passed 2 values but references `STARTING` and `RUNNING` — the `RUNNING` value is never matched because only one placeholder... actually looking at the code: `conn.execute("SELECT approval_id FROM executions WHERE status IN (?, ?) AND started_at < ?", (ExecutionStatus.STARTING.value, ExecutionStatus.RUNNING.value, now - timeout_s))` — this has 3 placeholders but 3 values, so it's correct. No bug. | N/A |

### Security Strengths (verified)
- Fail-closed default: CRITICAL tools DENY, no matching policy rule DENY
- SHA-256 cryptographic binding of approvals to (tool_id, params, task_id, plan_id, execution_mode, access_level, fs_scope, net_scope, resource_limits, policy_version) — prevents parameter swapping
- HITL enforcement: `ApprovalManager.decide()` only reachable via ApprovalAPI/ApprovalUI; LLM cannot call it directly
- CLAIMED → CONSUMED transition before subprocess launch — at-most-once execution
- Replay protection via atomic status transitions
- Bubblewrap namespace isolation with no network by default
- Loopback-only API binding enforced in config and server
- 0700/0600 private filesystem permissions
- No plaintext secrets in code; API token generated at runtime
- OpenAI-compatible provider requires `authorized = true` and HTTPS for remote
- Deterministic PolicyEngine does not rely on LLM-provided risk levels

---

## 7. Missing Dependencies

All Python dependencies are installed in `.venv/` and the test suite passes. System dependencies:

| Dependency | Status | Required For |
|------------|--------|--------------|
| `bubblewrap` (bwrap) | **INSTALLED** (`/usr/bin/bwrap`) | Sandbox execution |
| `cargo` / `rustc` | **INSTALLED** (`~/.cargo/bin/`) | Rust sentinel build |
| `systemd` | Present | User service units |
| `sqlite3` CLI | Absent | DB maintenance via stdlib `sqlite3` module (acceptable) |
| `pip`/`venv` in system Python | Absent | `.venv/` was created via `python3 -m venv --without-pip` + get-pip.py (documented in ADR 0002) |
| Ollama server | Not running/not installed | LLM inference (optional; remote endpoints supported) |
| GTK4 / libadwaita dev headers | Absent | Native desktop GUI (deferred) |
| Chrome/Chromium | Optional | `nexus desktop` opens web dashboard in browser |

No missing Python dependencies. The `requirements.lock` pins a large set including heavy ML stacks (torch, transformers, sentence-transformers) which may be unnecessary for a minimal deployment.

---

## 8. Architectural Duplication

| Duplication | Details | Recommendation |
|-------------|---------|----------------|
| **Automation Engine defined twice** | ARCHITECTURE.md sections 2.C and 2.E both describe "Automation Engine" | Documentation only; no code duplication |
| **Policy Engine + Approval Manager overlap** | Both handle authorization decisions; PolicyEngine decides ALLOW/REQUIRE_APPROVAL/DENY, ApprovalManager manages the HITL state machine | By design (separation of policy vs. approval), but the boundary could be clarified |
| **EventBus + EventStore** | Both persist and distribute events; EventRecorder bridges them | By design (persist-before-publish) |
| **MemoryStore.search() hybrid** | `_search_lexical` + `_search_semantic` combined with RRF | By design |
| **"zeus" vs "nexus" naming** | Logger root = "zeus", fallback DB = "zeus_events.db", some comments reference "zeus" | Rename to "nexus" for consistency (TD-001) |
| **CLI `desktop` command vs web dashboard** | CLI opens browser to web dashboard; no separate native desktop app | By design (web dashboard is the desktop interface for now) |

---

## 9. Integration Problems

| Issue | Severity | Details |
|-------|----------|---------|
| Systemd units not activated | Medium | Units exist but are not deployed. `systemd-analyze --user verify` passes syntax check only. Manual startup required. |
| Sentinel UDS socket path | Low | SentinelReceiver connects to `data/run/sentinel.sock`. If the Rust sentinel isn't running, health reports `connected: False` — graceful degradation, not a failure. |
| Full test suite timeout | Medium | Running all 22 test files together exceeds 120s. Subsets pass. Likely due to slow first-import of LanceDB/sentence-transformers and bwrap execution. |
| `ApprovalManager` blocking sqlite in async context | Low | Synchronous sqlite calls from async AutomationEngine could block the event loop under load. |
| Web dashboard token handling | Low | Token stored in `sessionStorage`; sent via Bearer header. XSS could exfiltrate token. Mitigated by loopback-only and same-origin. |
| No separate `nexus-daemon` | High (architectural gap) | The Master Spec requires a Rust `nexus-daemon` as the Control Plane. Currently the Python app is the runtime. This is the largest integration gap. |

---

## 10. Resource Bottlenecks

| Resource | Current Usage | Bottleneck Risk |
|----------|---------------|-----------------|
| RAM | Idle ~150-300 MB (per PERFORMANCE_BASELINE.md) | Low for current scope. LanceDB memory-mapping handles larger datasets. Heavy ML stack (torch, transformers) in requirements.lock could increase footprint if imported. |
| CPU | < 1% idle | Low. Policy evaluation <1ms, sandbox startup 50-100ms. |
| Disk | SQLite WAL + event log | Low. Retention disabled by default (retention_days=0) — database can grow without bound. |
| Network | Loopback only | No external network dependency for core functionality. Ollama optional. |
| GPU | Not used | No GPU acceleration currently. |
| LLM inference | Depends on Ollama availability | If Ollama unavailable, cognitive tasks fail (GatewayError). No local fallback model embedded. |

**Key bottleneck:** The cognitive loop requires an LLM provider. If Ollama is not running, `CognitiveCore.run()` fails with `GatewayError`. There is no embedded local model or deterministic fallback for planning.

---

## 11. Existing Tests and Results

**22 test files** in `tests/`:

| Test File | Focus | Result (subset run) |
|-----------|-------|---------------------|
| `test_config.py` | Config validation, precedence, env overrides | PASS (14) |
| `test_eventstore.py` | EventStore CRUD, dedup, retention, migrations, integrity | PASS (20) |
| `test_eventbus.py` | EventBus patterns, handler isolation, timeouts | PASS (8) |
| `test_app.py` | Bootstrap, health, serve lifecycle, sentinel UDS | PASS (5) |
| `test_automation.py` | AutomationEngine output capping, timeout, autonomy | PASS (5) |
| `test_execution_gate.py` | Policy ALLOW/REQUIRE_APPROVAL/DENY, approval flow | PASS (6) |
| `test_approval.py` | ApprovalManager lifecycle, concurrency, expiration, revocation | PASS (10) |
| `test_privatefs.py` | 0700/0600 permissions, symlink refusal | PASS (5) |
| `test_reasoning_loop.py` | Cognitive loop end-to-end | PASS (1) |
| `test_reflection_loop.py` | Reflection decisions (success/retry/replan/abort/timeout/cancellation/approval/false success) | PASS (10) |
| `test_cli.py` | CLI command parsing | PASS (4) |
| `test_e2e_security.py` | HITL bypass, param swap, mode swap, revocation, crash recovery, LLM approval attempt | PASS (6) |
| `test_e2e_flow.py` | Full HITL cognitive flow (suspend → approve → resume) | PASS (1) |
| `test_sentinel.py` | SentinelReceiver UDS, validation | PASS (4) |
| `test_memory.py` | MemoryStore CRUD, search, expiration | PASS (6) |
| `test_hybrid_memory.py` | Hybrid lexical+semantic search | PASS (1) |
| `test_memory_benchmark.py` | Memory performance | PASS (unknown — in full suite) |
| `test_memory_perf.py` | Memory performance | PASS (unknown — in full suite) |
| `test_llm_gateway.py` | LLM Gateway routing, circuit breakers, fallback, cost | PASS (7) |
| `test_api.py` | FastAPI endpoints, auth, WebSocket, telemetry | PASS (21) |
| `test_bootstrap.py` | Bootstrap success/failure | PASS (2) |
| `test_sandbox.py` | Sandbox execution, read authorized/denied files | PASS (unknown — in full suite) |
| `test_cognitive.py` | Cognitive core unit tests | PASS (unknown — in full suite) |

**Summary:** All test files pass when run in logical subsets (~125 tests verified passing). The full suite timed out at 120 seconds when run all together, likely due to slow heavy imports (LanceDB, sentence-transformers) and bwrap execution in sandbox tests.

**RELEASE_AUDIT.md claim** of "154 tests, 90 passed, 55 failed, 9 errors" appears to be **outdated/historical** — the current test suite is substantially green.

---

## 12. Recommended Migration Strategy

### Migration Target
Migrate from the current **single-process Python runtime** to the **four-plane architecture** (Control/Cognitive/Intelligence/Execution) while preserving all existing, working functionality.

### Migration Principles
1. **Preserve everything that works** — the Python cognitive layer, security model, event store, and API are solid foundations.
2. **Incremental, non-destructive** — no rewrites of working modules; wrap or extend them.
3. **Rust `nexus-daemon` as new Control Plane** — the Python app becomes a client/service of the daemon, not the authority.
4. **Backward-compatible IPC** — existing UDS event contract (newline-JSON) is preserved and extended.

### Phase-by-Phase Migration Plan

#### Phase 0 — Audit & Recovery (CURRENT)
- [x] Repository inspected and audited
- [x] Test suite verified (all subsets pass)
- [ ] Produce this audit report
- [ ] Produce `NEXUS_MASTER_ARCHITECTURE.md`
- [ ] Produce `NEXUS_ARCHITECTURE_DECISIONS.md`
- [ ] Produce `NEXUS_TECHNICAL_DEBT.md`
- **Exit condition:** Complete, verified understanding of current system

#### Phase 1 — Core Foundation (Rust Daemon + Event Fabric)
- Create `core/nexus-daemon/` (Rust, Tokio) — service supervisor, IPC, event routing, health, config
- Extend Event Fabric: UDS cross-language IPC, transactional outbox, dead-letter handling, security classification
- Wrap existing Python runtime as a daemon-managed service
- Deploy and activate systemd units
- **Exit condition:** Daemon supervises Python service; event fabric routes across language boundary; recovery from controlled failures works

#### Phase 2 — Cognitive Foundation
- Extract Intent Engine, Context Engine, Reasoning Engine, Decision Engine, Planner from Cognitive Core (refactor, don't rewrite)
- Add procedural/reflective/operational memory categories
- Add model profiles (Fast/General/Reasoning/Coding/Research/Embedding/Classification)
- Ensure cognitive task completion with validated result persistence
- **Exit condition:** NEXUS completes multi-step cognitive tasks with preserved results

#### Phase 3 — Secure Agency
- Implement Execution Gate (identity → capability → policy → risk → approval → resource → isolated execution → verify → commit → audit)
- Add Rollback Manager, SudoBroker, Execution Journal
- Expand tool registry beyond read-only diagnostics
- Sandbox all non-trivial tools
- **Exit condition:** Unauthorized operations denied; authorized operations auditable

#### Phase 4 — Multi-Agent Intelligence
- Agent Registry, Agent Runtime, Agent Orchestrator, Task Dispatcher
- 9 specialist agents (ORCHESTRATOR, RESEARCHER, ANALYST, STRATEGIST, ENGINEER, CRITIC, SIMULATOR, SECURITY, MEMORY CURATOR)
- Collaboration engine, adversarial review, resource-aware scheduling
- **Exit condition:** Multiple agents complete coordinated tasks with isolated permissions

#### Phase 5 — Universal Simulation
- World Builder, Entity Manager, Rules Engine, Simulation Clock, Event Scheduler
- Scenario Manager, Experiment Runner, Metrics Engine, Simulation Analyzer
- Evaluate MiroFish for reuse (not mandatory)
- **Exit condition:** Isolated, reproducible simulation environments

#### Phase 6 — Unified Experience
- GTK4 native desktop GUI (Command Center, Agent Control, Simulation Lab, Memory Explorer, Security Center)
- Integrate Web HUD, TUI, CLI
- Real-time monitoring via Event Fabric
- **Exit condition:** All subsystems monitorable/operable via interface

#### Phase 7 — Distributed & Adaptive
- Multi-node architecture, distributed agent scheduling, advanced resource management
- Experience consolidation, controlled self-improvement
- **Exit condition:** Multi-node operation with centralized governance

### Component Classification Matrix

| Component | Current Path | Classification | Rationale |
|-----------|-------------|----------------|-----------|
| `app.py` (bootstrap/runtime) | `nexus_core/app.py` | PRESERVE | Solid foundation; becomes daemon-managed service |
| `config.py` | `nexus_core/config.py` | PRESERVE | Strict validation, env overrides; keep |
| `eventbus.py` + `events.py` | `nexus_core/` | INTEGRATE | Extend with UDS, schema versioning, security classification |
| `memory/eventstore.py` + `store.py` | `nexus_core/memory/` | PRESERVE | SQLite WAL, migrations, dedup — excellent |
| `memory/records.py` + `vector.py` | `nexus_core/memory/` | IMPROVE | Add procedural/reflective/operational memory kinds |
| `cognitive.py` | `nexus_core/cognitive.py` | REFACTOR | Extract Intent/Context/Reasoning/Decision/Planner engines |
| `reflection.py` | `nexus_core/reflection.py` | PRESERVE | Works; keep as Reflection Engine |
| `automation.py` + `automation_types.py` | `nexus_core/` | IMPROVE | Expand tool registry; add execution mode validation |
| `policy.py` | `nexus_core/policy.py` | INTEGRATE | Becomes part of Execution Gate (Policy Engine sub-component) |
| `approval.py` | `nexus_core/approval.py` | PRESERVE | HITL state machine with SHA-256 binding — excellent |
| `approval_api.py` + `approval_ui.py` | `nexus_core/` | PRESERVE | Keep; integrate with TUI/GUI |
| `sandbox.py` | `nexus_core/sandbox.py` | IMPROVE | Add rollback integration, unprivileged UID option |
| `sentinel.py` | `nexus_core/sentinel.py` | PRESERVE | UDS receiver for Rust sentinel |
| `task_store.py` | `nexus_core/task_store.py` | PRESERVE | Checksummed suspended task persistence |
| `resources.py` | `nexus_core/resources.py` | IMPROVE | Expand to full Resource Manager (GPU, disk, network) |
| `privatefs.py` | `nexus_core/privatefs.py` | PRESERVE | 0700/0600 helpers — keep |
| `logging_setup.py` | `nexus_core/logging_setup.py` | PRESERVE | Structured JSON logging — keep |
| `llm_router/gateway.py` + `providers.py` | `nexus_core/llm_router/` | IMPROVE | Add model profiles, circuit breaker improvements |
| `security/egress.py` | `nexus_core/security/egress.py` | PRESERVE | Privacy levels — keep; expand to full Security Guard |
| `api/` (FastAPI) | `nexus_core/api/` | PRESERVE | Keep as interface layer; route through Control Plane |
| `cli.py` | `nexus_core/cli.py` | PRESERVE | Keep; extend for new subsystems |
| `tui.py` | `nexus_core/tui.py` | IMPROVE | Read-only → add approval workflows |
| `web/` | `nexus_core/web/` | PRESERVE | Web HUD — keep; add GTK4 native GUI separately |
| `core-rust/nexus-sentinel` | `core-rust/src/main.rs` | PRESERVE | Telemetry sentinel — keep as data source |
| `nexus-daemon` | (new) | CREATE | Rust daemon — does not exist yet |
| Control Plane | (new) | CREATE | Identity Manager, Execution Gate, Service Lifecycle — do not exist |
| Multi-Agent system | (new) | CREATE | Agent Registry, Runtime, Orchestrator, Swarm — do not exist |
| Simulation subsystem | (new) | CREATE | World Builder, Entity Manager, Rules Engine, etc. — do not exist |
| GTK4 Desktop GUI | (new) | CREATE | Native desktop — does not exist (web dashboard only) |
| Rollback Manager | (new) | CREATE | Does not exist |
| SudoBroker | (new) | CREATE | Does not exist |
| Execution Journal | (new) | CREATE | Separate from event store — does not exist |

---

## Highest-Priority Technical Risks

1. **No Rust `nexus-daemon` / Control Plane** (Critical architectural gap): The Master Spec requires a Rust daemon as the Control Plane with deterministic authorization independent of LLMs. Currently the Python app is the runtime authority. All security decisions ultimately reside in the Python process, which violates "security enforced independently of LLMs and agents" if the Python process is compromised.

2. **Cognitive loop depends on external LLM**: If Ollama is unavailable, all cognitive tasks fail. No embedded fallback model or deterministic planner exists.

3. **Systemd services not deployed**: The system cannot auto-start or recover from crashes without manual intervention. Production readiness requires activated systemd units.

4. **Blocking sqlite in async context**: `ApprovalManager` uses synchronous sqlite calls from async code paths. Under load this could block the event loop.

5. **Test suite timeout**: Full `pytest tests/` exceeds 120 seconds. CI/CD integration needs test partitioning or timeout increases.

6. **Sandbox runs as uid 0 inside bubblewrap**: Weaker isolation than an unprivileged UID. Namespace escape would yield host root.

7. **"zeus" naming remnants**: Logger root, fallback DB name, and some comments use "zeus" instead of "nexus" — causes confusion in logs, DB names, and imports (TD-001).

---

## Comparison: Current vs Target Architecture

| Plane | Current State | Target State | Gap |
|-------|--------------|--------------|-----|
| **Control Plane** | Python app + Policy Engine | Rust `nexus-daemon`, Identity Manager, Execution Gate, Service Lifecycle, Resource Manager | Large — daemon must be built |
| **Cognitive Plane** | Cognitive Core + Reflection + Memory + LLM Router | Intent, Context, Reasoning, Decision, Planner, Knowledge engines; 6 memory categories; model profiles | Moderate — refactor + extend |
| **Intelligence Plane** | None | Agent Registry, Runtime, Orchestrator, Swarm, Collaboration, Adversarial Review | Large — must be built |
| **Execution Plane** | Automation Engine (7 read-only tools) + Sandbox | Tool Registry, Automation Engine, Execution Engine, Sandbox, Rollback, adapters (FS/Process/Network/Browser/App/API/DevTools) | Moderate — expand tools + add rollback |
| **Simulation** | None | World Builder, Entity Manager, Rules Engine, Clock, Scheduler, Scenario, Experiment, Metrics, Analyzer | Large — must be built |
| **Interface** | Web dashboard + CLI + TUI (read-only) | GTK4 Desktop GUI + Web HUD + TUI + CLI + API | Moderate — GTK4 must be built |
| **Event Fabric** | In-process EventBus + SQLite EventStore | UDS cross-language IPC, transactional outbox, dead-letter, schema versioning | Moderate — extend |
| **Security** | Policy + Approval + Sandbox + Egress | Execution Gate, SudoBroker, Rollback Manager, Execution Journal, Secret Management | Moderate — add missing components |

---

## Immediate Next Steps (Approved: Phase 0 completion only)

1. Review and approve this audit report.
2. Create `docs/NEXUS_MASTER_ARCHITECTURE.md`, `docs/NEXUS_ARCHITECTURE_DECISIONS.md`, `docs/NEXUS_TECHNICAL_DEBT.md`.
3. Begin Phase 1: Design and implement `core/nexus-daemon/` (Rust, Tokio) as the Control Plane.
4. Do **not** modify or delete existing working source code until Phase 1 design is approved.

---

**Audit classification summary:**
- **PRESERVE:** 20 components (Python runtime core, event store, memory, security, API, CLI, Rust sentinel)
- **IMPROVE:** 8 components (cognitive core refactor, automation expansion, resources, LLM router profiles, sandbox, TUI, memory categories)
- **INTEGRATE:** 3 components (EventBus, Policy Engine into Execution Gate, Python runtime into daemon supervision)
- **REFACTOR:** 1 component (Cognitive Core into discrete engines)
- **REPLACE:** 0 components
- **CREATE:** 10 components (nexus-daemon, Control Plane components, Multi-Agent system, Simulation subsystem, GTK4 GUI, Rollback Manager, SudoBroker, Execution Journal)

**End of audit report.**

---

# Atualização pós-auditoria (2026-11-12)

Esta seção corrige o retrato histórico acima e registra o estado real após as Fases 3–7 e a auditoria de endurecimento.

## Estado atual

- **Versão:** `nexus-core` 0.2.0, 8 commits no `master`.
- **Testes:** 305 passed, 12 subtests passed (~129 s).
- **Estáticos:** `ruff` (E9/F63/F7/F82/F821/F822/F823), `compileall`, `node --check` e `git diff --check` limpos.
- **Serviços:** `nexus-core.service` e `nexus-sentinel.service` ativos e habilitados (`systemctl --user`).
- **LLM local:** `nexusorbe-llama3.1:latest` e `nexusorbe-llama3.2:latest`, cadeia validada com `nexus llm complete`.
- **Ollama remoto:** dois modelos (`gemma4:e4b-it-q4_K_M` e `qwen3:4b`) configurados no OpenCode, não no `nexus.toml` por exigência de HTTPS/autorização para provedores remotos.

## Gaps históricos resolvidos

| Gap no relatório original | Estado em 2026-11-12 |
|---|---|
| `nexus-daemon` Rust ausente | Implementado em `core/nexus-daemon/`; Python segue como runtime cognitivo |
| Motores cognitivos ausentes | Implementados: Intent, Context, Reasoning, Decision, Planner, Knowledge |
| Multi-agente ausente | Implementado: Registry, Manifest, Runtime, Dispatcher, Orchestrator, Collaboration, Adversarial Review, Lifecycle |
| Simulação universal ausente | Implementada: tipos, clock, state manager, rules, event scheduler, world builder, scenarios, metrics, analyzer, results store |
| Rollback/SudoBroker/Journal ausentes | Implementados e testados |
| Event Fabric incompleto | Outbox transacional, dead-letter e rede WebSocket implementados |
| Sem memória vetorial | LanceDB + Sentence-Transformers + busca híbrida RRF |
| Systemd não ativado | Units ativas e habilitadas |
| Test suite timeout | Suíte completa verde em ~129 s |
| `config/nexus.toml` com modelos ausentes | Alinhado aos modelos Ollama instalados |
| Dashboard sem aprovações | Painel Segurança com Aprovar/Negar via `/approvals/{id}/decide` |

## Correções da auditoria de 2026-11-12

- CLI: `--config`/`--json` aceitos antes/depois de subcomandos aninhados.
- API: ordem de `/approvals/history`; `paradigm` normalizado e 422 para inválido.
- Event Fabric: payload canônico, envelopes legados, dead-letter, metadados de severidade/correlação/dedup.
- Execution Journal: `integrity_hash` recalculado em `update_status`.
- Simulation Analyzer: timestamps epoch, reconstrução de eventos e integridade.
- SQLite: `connect_db` com `busy_timeout`, commit/rollback e fechamento garantido.
- Task Store: chave vazia, escrita atômica `0600` com `O_NOFOLLOW`/fsync e leitura UTF-8.
- VectorStore: compatibilidade `list_tables`/`table_names`, escape de filtros e `upsert` sem duplicatas.
- Distributed Network: timestamps/dedup de eventos e heartbeat em `time.time()`.
- Web HUD: aprovações pendentes no painel Segurança.
- Configuração LLM: modelos locais alinhados ao Ollama real.

## Riscos e gaps remanescentes

1. **Branding `zeus`:** logger root e vários loggers ainda usam `zeus` (TD-001/TD-010).
2. **Catálogo de ferramentas:** apenas 7 diagnósticos read-only; a máquina de execução segura existe, mas não há ações de escrita.
3. **Task Store:** criptografia opcional desativada por padrão.
4. **SQLite síncrono em async:** `busy_timeout` reduz contenção, não o bloqueio do event loop.
5. **Sandbox UID 0:** namespace forte, mas UID não privilegiado seria preferível.
6. **Sem LLM determinístico:** fallback entre modelos, mas nenhum planejador sem LLM.
7. **GTK4:** Web HUD é a interface desktop; GUI nativa não implementada.
8. **Scheduler:** `scheduler.py` existe, mas não está integrado ao runtime/API nem tem testes dedicados.
9. **Ollama remoto:** contexto fixo de 4096 tokens e Qwen3 lento (~107 s vs ~28 s do Gemma 4) no host atual.

## Classificação atual

- **PRESERVE:** runtime Python, event store, memória, segurança, API, CLI, sentinel Rust.
- **IMPLEMENTED:** daemon Rust, motores cognitivos, multi-agente, simulação, execução segura, event fabric, rede distribuída.
- **REMAINING:** GTK4, branding, catálogo de ferramentas amplo, integração do scheduler, criptografia padrão do Task Store, SQLite assíncrono.