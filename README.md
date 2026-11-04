# NEXUS — Local-First Cognitive Operating System

NEXUS is a production-grade, local-first Cognitive OS for Linux designed to bridge the gap between LLM reasoning and secure system automation. It transforms a diagnostic agent into a cognitive system capable of perceiving, planning, and executing complex tasks with mandatory human authorization for high-risk operations.

**State: Production-Ready Core.** Features a reflective cognitive loop, hybrid semantic memory, a risk-based execution gate, and hardware-isolated sandboxing.

## 🧠 Cognitive Architecture

NEXUS operates on a closed-loop cognitive cycle:
**Intent $\rightarrow$ Plan $\rightarrow$ Execute $\rightarrow$ Observe $\rightarrow$ Critique $\rightarrow$ Decide**

### Core Components

| Component | Responsibility | Key Technology |
|---|---|---|
| **Cognitive Core** | Manages the reasoning loop, task suspension, and state synthesis. | Asyncio / Reflective Loop |
| **Automation Engine** | Executes tools through a secure gate based on Risk Levels. | Tool Registry / Policy Engine |
| **Policy Engine** | Deterministically evaluates if an action is `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`. | Risk-Based Rules |
| **Approval Manager** | Manages the HITL (Human-In-The-Loop) state machine with cryptographic binding. | SQLite / SHA-256 |
| **Sandbox Executor** | Isolates tool execution using namespace isolation and resource limits. | `bwrap` / Cgroups v2 |
| **Hybrid Memory** | Combines lexical search (FTS5) and semantic search (Vector) using Reciprocal Rank Fusion. | SQLite / LanceDB |
| **Task Store** | Persists suspended cognitive states with atomic writes and integrity checksums. | Hardened JSON / SHA-256 |
| **Sentinel** | Low-level system observation and metric collection. | Rust / Unix Sockets |

## 🛡️ Security Model

NEXUS follows a **Fail-Closed** design philosophy:

1.  **Risk Classification**: Every tool is assigned a `RiskLevel` (LOW, MEDIUM, HIGH, CRITICAL).
2.  **The Execution Gate**: No tool is executed without passing through the `PolicyEngine`.
3.  **Cryptographic Binding**: Approvals are bound to a hash of the tool ID, parameters, and task ID, preventing "parameter swapping" attacks.
4.  **HITL Enforcement**: High-risk operations trigger a `TaskSuspended` state. The agent cannot approve its own actions.
5.  **OS-Level Isolation**: Tools run in an unprivileged `bubblewrap` sandbox with restricted filesystem access, no network (unless specified), and strict CPU/RAM limits via `systemd-run`.

## 🚀 Quick Start

### Installation (Linux)
```bash
cd nexus
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# Install bubblewrap for sandboxing
sudo apt-get install bubblewrap 
```

### Basic Usage
```bash
# Start the API and Cognitive Core
python -m nexus_core serve

# Launch the TUI for system monitoring and approval management
python -m nexus_core tui

# Run a specific cognitive task
python -m nexus_core task run --prompt "Analyze system logs and suggest optimizations"
```

## 🌐 API & Interfaces

### Approval API
The `ApprovalAPI` allows humans to manage pending requests:
- `GET /api/v1/approvals/pending`: List all tasks awaiting authorization.
- `GET /api/v1/approvals/{id}`: Inspect tool parameters and risk justification.
- `POST /api/v1/approvals/{id}/approve`: Authorize execution.
- `POST /api/v1/approvals/{id}/deny`: Reject and abort task.
- `GET /api/v1/approvals/history`: Audit trail of all human decisions.

### Local Interface
- **TUI**: High-density terminal interface for real-time event monitoring and HITL decisions.
- **Web Dashboard**: Visual representation of the cognitive state, memory maps, and system health.

## 🛠️ Development & Testing

NEXUS maintains a rigorous testing suite covering the entire security chain:

```bash
# Run the full suite
pytest tests/

# Verify the E2E HITL flow (Request -> Suspension -> Approval -> Execution)
pytest tests/test_e2e_security.py
```

## 📉 Performance Baseline
- **Target Hardware**: Intel Low-Power CPU, 12GB RAM.
- **Memory**: Optimized for low-idle footprint; LanceDB provides efficient vector indexing without heavy external dependencies.
- **Latency**: Sub-millisecond policy evaluation; minimal overhead from `bwrap` namespaces.
