# NEXUS Engineering Roadmap

**Updated:** 2026-11-14

Legend: `[x]` done, `[ ]` open/partial. Notes describe the real state.

## P0 — Critical Infrastructure (Foundation & Unity)

- [ ] **Unified Branding:** remove remaining `zeus` logger/database names. The codebase is `nexus_core`, but the logger root and several logger names still use `zeus` (TD-001/TD-010).
- [x] **Deployment Validation:** `nexus-core.service` and `nexus-sentinel.service` are active and enabled.
- [x] **Environment Standardization:** `pyproject.toml` and `requirements.lock` exist for the target Linux Mint/Ubuntu environment.

## P1 — Core Cognitive Functionality (The "Brain")

- [x] **Task Planning:** discrete Planner Engine plus the adaptive cognitive loop.
- [x] **Reflective Loop:** Reflection Engine with success/retry/replan/approval/abort decisions.
- [x] **Sandbox Execution:** Bubblewrap-based isolated execution for safe tool usage.
- [x] **Agent Coordination:** Agent Registry, Runtime, Dispatcher, Orchestrator, Collaboration and Adversarial Review.
- [x] **Persistent Knowledge Graph:** SQLite graph in the governed database with nodes, edges, provenance, confidence, temporal validity, deterministic BFS traversal and no automatic simulation-to-fact promotion (T3).
- [x] **Control Plane IPC:** versioned `Hello`/`Welcome` handshake, local `SO_PEERCRED`/UID authentication, `0600` socket, timeouts, frame limits and `authorize`/approval/`health`/`event` commands in the Rust daemon (T4). Final security authority remains in Python until T6.
- [x] **Control Plane Parity:** Policy Engine and Approval Manager in Rust mirror Python decisions with shared fixtures, revocation, per-request TTL, `verify_action`, single-use claim and `CLAIMED → CONSUMED` (T5). Approvals remain in memory; durable persistence and final authority belong to T6.
- [ ] **Execution Lifecycle:** Execution Gate, Tool Registry, Rollback, SudoBroker and Execution Journal are implemented, but the Automation catalog remains seven read-only diagnostics. Safe write actions are not yet exposed.

## P2 — AI & Memory Infrastructure (Knowledge)

- [x] **Semantic Memory:** LanceDB vector store with Sentence-Transformers and hybrid RRF search.
- [x] **Memory Consolidation:** episodic-to-semantic consolidation proposals.
- [x] **Data Egress Policy:** explicit privacy levels and remote-provider authorization/HTTPS rules.

## P3 — Desktop & Web Interfaces (Experience)

- [ ] **Native Tray:** GTK4/libadwaita system tray not implemented.
- [ ] **Native Shell:** command-palette desktop shell not implemented.
- [x] **Enhanced Dashboard:** Web HUD includes Command Center, tasks, memory, events, models, diagnostics, agents, simulations, security, logs and configuration. Real-time memory visualization/model performance metrics remain limited.

## P4 — Advanced Autonomy (Agency)

- [ ] **Safe Shell:** the sandbox and Execution Gate exist, but arbitrary sandboxed shell execution is not exposed.
- [ ] **Scheduler:** `nexus_core/scheduler.py` implements periodic/cron-like automation and cognitive tasks, but it is not wired into the runtime/API and has no dedicated tests.
- [ ] **External Integrations:** DBus/API integrations for local apps not implemented.

## P5 — Hardening & Audit (2026-11-12)

- [x] Correct CLI global-option parsing for nested subcommands.
- [x] Fix approval-history route ordering and scenario paradigm validation.
- [x] Harden Event Fabric (canonical payloads, legacy envelopes, dead-letter migration, metadata).
- [x] Recalculate execution-journal integrity hashes on status updates.
- [x] Fix simulation Results Store timestamps/reconstruction/integrity.
- [x] Add `sqlite_utils.connect_db` with `busy_timeout` and guaranteed close.
- [x] Harden Task Store atomic writes, empty-key handling and UTF-8 reads.
- [x] Fix VectorStore LanceDB API compatibility, SQL escaping and idempotent upsert.
- [x] Fix distributed-network event timestamps/dedup and heartbeat clocks.
- [x] Add pending-approval controls to the Web HUD Security panel.
- [x] Align `config/nexus.toml` with the Ollama models actually installed locally.
- [x] Full validation: 305 tests + 12 subtests; ruff/compileall/node-check/diff-check clean.
