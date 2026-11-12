# NEXUS Architecture

**Version:** 1.1
**Updated:** 2026-11-12
**Pattern:** Modular, Event-Driven, Local-First
**Authoritative detail:** `docs/NEXUS_MASTER_ARCHITECTURE.md`

## 1. High-Level Topology

NEXUS operates as a middleware between the Linux OS and the user, using an asynchronous event bus for internal communication and SQLite for durable local state.

```
CLI / TUI / Web HUD
        │ HTTP/WS (127.0.0.1:8765, Bearer token)
        ▼
NEXUS API (FastAPI)
        │
        ▼
NEXUS COGNITIVE CORE (Python)
├── Cognitive Core (Intent → Plan → Execute → Reflect)
├── Cognitive Engines (Intent, Context, Reasoning, Decision, Planner, Knowledge)
├── Automation Engine + Tool Registry + Execution Gate
├── Policy Engine + Approval Manager (HITL, SHA-256)
├── LLM Gateway (Ollama + OpenAI-compatible)
├── Memory Store (SQLite FTS5 + LanceDB + RRF)
├── Event Fabric (EventBus + EventStore + transactional outbox/dead-letter)
├── Multi-Agent Plane (Registry, Runtime, Orchestrator, Review)
├── Simulation Plane (Worlds, Scenarios, Metrics, Analyzer)
├── Distributed Plane (Nodes, Scheduler, WebSocket network)
├── Sandbox (bwrap), Sentinel Receiver (UDS)
└── Task Store (suspended tasks, SHA-256 integrity, optional encryption)
        │ UDS (newline-JSON)
        ▼
NEXUS SENTINEL (Rust, core-rust) → /proc telemetry + filesystem watch
```

## 2. Planes

| Plane | Language | Responsibility | Current State |
|---|---|---|---|
| **Control Plane** | Rust + Python | Lifecycle, IPC, deterministic policy, execution gate | Partial (Rust daemon + Python runtime; systemd units active) |
| **Cognitive Plane** | Python | Interpretation, reasoning, planning, memory, model routing | Implemented (6 discrete engines + adaptive loop) |
| **Intelligence Plane** | Python | Agent registry, runtime, orchestration, collaboration, adversarial review | Implemented |
| **Simulation Plane** | Python | Worlds, entities, rules, clock, scenarios, experiments, metrics | Implemented |
| **Execution Plane** | Python | Tool registry, automation, sandbox, rollback, sudo broker, journal | Implemented; tool catalog still read-only diagnostics |
| **Distributed & Adaptive Plane** | Python | Nodes, resource-aware scheduling, experience consolidation, self-improvement, WebSocket network | Partial (single-node scheduler integration pending) |

## 3. Runtime Modules

### 3.1 Cognitive Core (`nexus_core/cognitive.py`)

Orchestrates the `Intent → Plan → Execute → Observe → Critique → Decide` loop.

- **Adaptive loop:** integrates the Reflection Engine to evaluate results and trigger replanning or retries.
- **Task lifecycle:** `start → [reflective cycle] → completed | failed | timed_out | cancelled`.
- **Cognitive audit:** records every reflection decision and evidence in the event log.
- **Optional engines:** Intent, Context, Reasoning, Decision, Planner and Knowledge engines enrich the loop without breaking the legacy path.

### 3.2 Memory (`nexus_core/memory/`)

- **Event Store:** append-only immutable log of system events (SQLite WAL, dedup).
- **Records:** short-term, long-term, episodic and semantic memory.
- **Search:** FTS5 lexical + LanceDB semantic vectors combined with Reciprocal Rank Fusion.
- **VectorStore:** lazy loading, LanceDB API compatibility (`list_tables`/`table_names`), safe SQL literal escaping and idempotent `upsert` (`delete` + `add`).
- **Consolidation:** episodic-to-semantic proposals.

### 3.3 LLM Gateway (`nexus_core/llm_router/`)

- Ollama and OpenAI-compatible providers.
- Ordered fallback chain, retries, circuit breakers, cooldown and context-budget truncation.
- Egress guard with privacy levels; remote providers require `authorized = true` and HTTPS.
- Local chain is aligned with the installed Ollama tags (`nexusorbe-llama3.1:latest`, `nexusorbe-llama3.2:latest`).

### 3.4 Event Fabric (`nexus_core/event_fabric.py`)

- **Transactional outbox:** events persist before delivery; retry with exponential backoff.
- **Dead-letter queue:** failed events are parked for inspection and requeue.
- **Canonical payloads:** deterministic JSON used for dedup and integrity.
- **Legacy envelope support:** reads old event envelopes and preserves severity/correlation/dedup metadata.
- **Remote propagation:** distributed WebSocket network preserves event timestamps and dedup keys.

### 3.5 Secure Agency (`nexus_core/execution_gate.py`, `policy.py`, `approval.py`, `sandbox.py`)

- **Execution Gate:** identity → capability → policy → risk → approval → resource → isolated execution → verification → commit → audit.
- **Approval Manager:** HITL state machine `PENDING → APPROVED → CLAIMED → CONSUMED`, SHA-256 binding, TTL expiration and revocation.
- **Sandbox:** bubblewrap namespace isolation, read-only root, isolated `/tmp` and `/workspace`, optional cgroup limits.
- **Supporting controls:** Tool Registry, Rollback Manager, SudoBroker and Execution Journal.
- **Current wiring:** `AutomationEngine` applies `PolicyEngine`/`ApprovalManager` directly and can optionally delegate to `ExecutionGate`; the bootstrap does not yet inject that gate by default (TD-015).

### 3.6 Multi-Agent Intelligence (`nexus_core/agent_*.py`, `task_dispatcher.py`, `collaboration_engine.py`, `adversarial_review.py`)

- Agent manifests, registry, runtime, dispatcher, orchestrator and lifecycle.
- Capability-based assignment, parallel dispatch with failure isolation.
- Agents never bypass the Execution Gate; disagreement is preserved rather than forced into consensus.

### 3.7 Simulation (`nexus_core/simulation_*.py`, `world_builder.py`, `scenario_manager.py`, `metrics_engine.py`)

- Ten simulation paradigms, isolated parallel worlds, clocks, state snapshots, deterministic rules and event scheduling.
- Scenario/experiment management with reproducibility and labeled results (`simulated`, `estimated`, `inferred`).
- Persistent Results Store with SHA-256 integrity verification.

### 3.8 Distributed & Adaptive Plane (`nexus_core/distributed_*.py`, `event_fabric.py`)

- Node manager, resource monitor, resource-aware scheduler with failover and retry.
- Experience consolidation and governed self-improvement (security-policy changes require human approval).
- WebSocket server/client for event propagation and heartbeats between nodes.
- Integration layer (`nexus_core/integration.py`) composes the full pipeline.

### 3.9 Interfaces (`nexus_core/cli.py`, `tui.py`, `api/`, `web/`)

- **CLI:** operational commands; global `--config`/`--json` accepted before or after nested subcommands.
- **TUI:** read-only curses dashboard.
- **API:** FastAPI on loopback with Bearer token and scope-based autonomy.
- **Web HUD:** Command Center, tasks, memory, events, models, diagnostics, agents, simulations, security, logs and configuration.

## 4. Persistence and Data Flow

- `Sentinel (Rust) → Unix Socket → Event Recorder (Python) → SQLite → Event Bus → Cognitive Core / UI`.
- **Persist before publish:** the recorder writes to the EventStore before publishing to the bus.
- **SQLite:** WAL mode, versioned migrations, `busy_timeout`, explicit commit/rollback and guaranteed close via `nexus_core/sqlite_utils.py`.
- **Private files:** `0700` directories and `0600` files; symlinks refused.
- **Task Store:** atomic writes with `O_NOFOLLOW`/fsync and integrity checksums; optional Fernet/XOR encryption when a key is supplied (disabled by default).

## 5. Security Model

- **API:** loopback-only bind (`127.0.0.1`), Bearer token, HMAC comparison.
- **Permissions:** scopes tied to `autonomy_level` (0: observation, 1: assistance, 2: execution).
- **Approvals:** cryptographically bound to tool, parameters, task, plan, mode, scope and policy version.
- **Replay protection:** `CLAIMED → CONSUMED` before subprocess launch; expired approvals are rejected at claim time.
- **Sandbox:** bwrap namespaces, no network by default, resource limits.
- **Egress:** privacy levels and remote-provider authorization/HTTPS rules.
- **Residual risks:** process-level boundary, plaintext API token, sandbox UID 0, blocking SQLite in async paths and optional task encryption.

## 6. Validation (2026-11-12)

- Full suite: **305 passed, 12 subtests passed** (~129 s).
- `ruff` fatal/undefined-name checks: clean.
- `compileall` and `node --check nexus_core/web/app.js`: clean.
- `git diff --check`: clean.
- Local LLM completion through the configured chain: successful (`nexusorbe-llama3.1:latest`).
