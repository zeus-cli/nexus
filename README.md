# NEXUS — Local-First Cognitive Operating System

NEXUS is a production-grade, local-first Cognitive OS for Linux designed to bridge the gap between LLM reasoning and secure system automation. It transforms a diagnostic agent into a cognitive system capable of perceiving, planning, and executing complex tasks with mandatory human authorization for high-risk operations.

**State: Production-Ready Core, hardened by the 2026-11-12 audit.** The current suite has **305 tests passing (12 subtests)**, with hybrid semantic memory, a risk-based execution gate, event fabric with transactional outbox/dead-letter, distributed WebSocket networking, and hardware-isolated sandboxing.

## 🧠 Cognitive Architecture

NEXUS operates on a closed-loop cognitive cycle:
**Intent → Plan → Execute → Observe → Critique → Decide**

### Core Components

| Component | Responsibility | Key Technology |
|---|---|---|
| **Cognitive Core** | Manages the reasoning loop, task suspension, and state synthesis. | Asyncio / Reflective Loop |
| **Cognitive Engines** | Intent, Context, Reasoning, Decision, Planner and Knowledge engines. | Python / structured JSON |
| **Automation Engine** | Executes tools through a secure gate based on Risk Levels. | Tool Registry / Policy Engine |
| **Policy Engine** | Deterministically evaluates if an action is `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`. | Risk-Based Rules |
| **Approval Manager** | Manages the HITL state machine with cryptographic binding. | SQLite / SHA-256 |
| **Execution Gate** | Enforces identity → capability → policy → risk → approval → resource → execution → audit. | Python / Approval Manager |
| **Sandbox Executor** | Isolates tool execution using namespace isolation and resource limits. | `bwrap` / Cgroups v2 |
| **Hybrid Memory** | Combines lexical search (FTS5) and semantic search (LanceDB) using Reciprocal Rank Fusion. | SQLite / LanceDB |
| **LLM Gateway** | Routes tasks across local/remote providers with retries, circuit breakers and context budgets. | Ollama / OpenAI-compatible |
| **Event Fabric** | Persists events before publication and guarantees delivery with outbox and dead-letter queues. | SQLite WAL / WebSocket |
| **Task Store** | Persists suspended cognitive states with atomic writes and integrity checksums. | Hardened JSON / SHA-256 |
| **Sentinel** | Low-level system observation and metric collection. | Rust / Unix Sockets |
| **Web HUD** | Command Center, agents, simulations, memory, models and security panels. | HTML / CSS / JS |

## 🛡️ Security Model

NEXUS follows a **Fail-Closed** design philosophy:

1. **Risk Classification**: Every tool is assigned a `RiskLevel` (LOW, MEDIUM, HIGH, CRITICAL).
2. **The Execution Gate**: No tool is executed without passing through the `PolicyEngine`.
3. **Cryptographic Binding**: Approvals are bound to a hash of the tool ID, parameters, task ID and scope, preventing "parameter swapping" attacks.
4. **HITL Enforcement**: High-risk operations trigger a `TaskSuspended` state. The agent cannot approve its own actions. Expired approvals are rejected at claim time.
5. **OS-Level Isolation**: Tools run in an unprivileged `bubblewrap` sandbox with restricted filesystem access, no network (unless specified), and strict CPU/RAM limits via `systemd-run`.
6. **Data Egress Policy**: Privacy levels (`PUBLIC`, `PRIVATE`, `SECRET`) govern what may leave the machine. Remote LLM providers require explicit `authorized = true` and HTTPS.
7. **SQLite Contention Safety**: Shared connections use `busy_timeout`, explicit commit/rollback and guaranteed close via `nexus_core/sqlite_utils.py`.

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

# Check configured LLM providers and models
python -m nexus_core llm models

# Request a completion without executing the answer
python -m nexus_core llm complete --prompt "Responda apenas: ok" --max-tokens 16
```

## 🤖 LLM Configuration

The local `config/nexus.toml` chain uses the models actually present in the local Ollama instance:

```toml
[[llm.providers]]
name = "ollama-local"
kind = "ollama"
base_url = "http://127.0.0.1:11434"
models = ["nexusorbe-llama3.1:latest", "nexusorbe-llama3.2:latest"]
authorized = true

[[llm.chain]]
provider = "ollama-local"
model = "nexusorbe-llama3.1:latest"

[[llm.chain]]
provider = "ollama-local"
model = "nexusorbe-llama3.2:latest"
```

Remote providers belong in a separate deployment decision: NEXUS only accepts them with `authorized = true` and HTTPS. OpenCode is an external tool and is not part of the NEXUS security boundary; in the current environment it uses two remote Ollama models (`gemma4:e4b-it-q4_K_M` as default and `qwen3:4b` as a read-only subagent) with a 4096-token context limit.

## 🌐 API & Interfaces

### Approval API
The `ApprovalAPI` allows humans to manage pending requests:
- `GET /api/v1/approvals/pending`: List all tasks awaiting authorization.
- `GET /api/v1/approvals/{id}`: Inspect tool parameters and risk justification.
- `POST /api/v1/approvals/{id}/decide`: Approve or deny with `{"decision": "approve" | "deny" | "revoke"}`.
- `GET /api/v1/approvals/history`: Audit trail of all human decisions.

### Other Interfaces
- **CLI**: Operational administration, diagnostics, memory, tasks, approvals, events and database maintenance.
- **TUI**: High-density terminal interface for real-time event monitoring and HITL decisions.
- **Web Dashboard**: Command Center, tasks, memory, events, models, diagnostics, agents, simulations, security and logs.
- **API**: Loopback-only FastAPI server with Bearer token auth and scope-based autonomy.

## 🛠️ Development & Testing

NEXUS maintains a rigorous testing suite covering the entire security chain:

```bash
# Run the full suite (305 tests, 12 subtests)
pytest tests/

# Verify the E2E HITL flow (Request -> Suspension -> Approval -> Execution)
pytest tests/test_e2e_security.py

# Static validation
python -m compileall -q nexus_core tests
node --check nexus_core/web/app.js
git diff --check
```

The full suite takes about 2 minutes on the target host. Running `test_hybrid_memory.py` in isolation can hit the 5 s embedding-load timeout on a cold `sentence_transformers` import; the full suite warms that import in an earlier test.

## 📉 Performance Baseline
- **Target Hardware**: Intel Low-Power CPU, 12 GB RAM.
- **Memory**: Optimized for low-idle footprint; LanceDB provides efficient vector indexing without heavy external dependencies.
- **Latency**: Sub-millisecond policy evaluation; minimal overhead from `bwrap` namespaces.
- **Measured (2026-11-12)**: full suite ~129 s; local 8B Ollama completion ~12 s for 16 output tokens; remote OpenCode Gemma ~28 s and Qwen ~107 s per short agent run.

See `PERFORMANCE_BASELINE.md` for details and caveats.

## 📚 Documentation

| Document | Purpose |
|---|---|
| `ARCHITECTURE.md` | Concise architecture overview |
| `docs/NEXUS_MASTER_ARCHITECTURE.md` | Authoritative architecture and plane definitions |
| `docs/NEXUS_ARCHITECTURE_DECISIONS.md` | ADR index |
| `docs/NEXUS_TECHNICAL_DEBT.md` | Detailed technical-debt register |
| `docs/audit/NEXUS_REPOSITORY_AUDIT.md` | Historical repository audit + 2026-11-12 update |
| `SECURITY.md` | HITL and execution security rules |
| `ROADMAP.md` | Engineering roadmap and current status |
