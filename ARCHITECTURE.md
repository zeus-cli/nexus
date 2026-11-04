# NEXUS Architecture
**Version:** 1.0 (Authoritative)
**Pattern:** Modular, Event-Driven, Local-First

## 1. High-Level Topology
NEXUS operates as a middleware between the Linux OS and the User, utilizing an asynchronous event bus for all internal communication.

### Data Flow
`Sentinel (Rust)` -> `Unix Socket` -> `Event Recorder (Py)` -> `SQLite` -> `Event Bus` -> `Cognitive Core / UI`

## 2. Module Definitions

### A. Cognitive Core (`nexus_core/cognitive.py`)
Orchestrates the "Intent -> Plan -> Execute -> Observe -> Critique -> Decide" adaptive loop.
- **Adaptive Loop:** Integrates the Reflection Engine to evaluate results and trigger replanning or retries.
- **Task Lifecycle:** start -> [reflective cycle] -> (completed | failed | timed_out | cancelled).
- **Cognitive Audit:** Records every reflection decision and evidence used in the event log.

### B. Reflection Engine (`nexus_core/reflection.py`)
Independent auditor for task execution.
- **Responsibility:** Compares tool output against original goals to detect failures, contradictions, or incompleteness.
- **Decisions:** SUCCESS, RETRY, REPLAN, REQUEST_APPROVAL, ABORT, PARTIAL_SUCCESS.
- **Evidence-Based:** Uses plan, tool results, and memory context to justify decisions.

### C. Automation Engine (`nexus_core/automation.py`)
Controlled execution environment for system interactions.
- **Tool Registry:** Defines risk levels, access levels, and execution modes (Host vs Sandbox).
- **Security Gating:** Enforces autonomy levels and idempotency checks.
- **Integration:** Forwards execution results to the Reflection Engine for validation.

### D. Sandbox Executor (`nexus_core/sandbox.py`)
Hardened isolation layer using Bubblewrap (`bwrap`).
- **Isolation:** Unshares all namespaces (User, PID, Net, IPC, UTS, Cgroup).
- **Filesystem:** Root is read-only; limited explicit bind mounts; isolated `/tmp` and `/workspace`.
- **Governance:** Integrates with `systemd-run` for Cgroup v2 resource limits (CPU, Memory, Pids).
- **Network:** Disabled by default for untrusted execution.

### C. Persistent Memory (`nexus_core/memory/`)
Structured storage using SQLite.
- **Event Store:** Append-only immutable log of system events.
- **Records:** Categorized memory (Short, Long, Episodic, Semantic).
- **Search:** FTS5 Lexical search (Current). Vector/Semantic (Planned).

### D. Rust Sentinel (`core-rust/`)
Low-overhead system observer.
- **Responsibility:** `/proc` monitoring, hardware stats, directory watching.
- **IPC:** Newline-JSON over private Unix socket.

### E. Automation Engine (`nexus_core/automation.py`)
Controlled execution environment.
- **Policy:** Allowlist-only.
- **Constraints:** Read-only for current version; requires Autonomy Level 2.

## 3. Security Model
- **Isolation:** `0700` directories, `0600` files.
- **API:** Loopback-only bind (`127.0.0.1`), Bearer Token auth.
- **Permissions:** Escopes tied to `autonomy_level` (0: Obs, 1: Assist, 2: Exec).

### Approval Interface (TUI)
NEXUS implements a native Terminal User Interface (TUI) for authorizations.
Flow: ApprovalManager -> ApprovalAPI -> ApprovalUI.
This ensures that the LLM cannot interact with the decision logic directly.
