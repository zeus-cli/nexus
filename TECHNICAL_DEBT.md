# NEXUS Technical Debt Register

**Updated:** 2026-11-12
**Detailed register:** `docs/NEXUS_TECHNICAL_DEBT.md`

| ID | Priority | State | Description | Impact | Next Action |
|---|---|---|---|---|---|
| **TD-001** | P0 | Open | **Branding split** — logger root and several loggers still use `zeus` | Confusion in logs, DB names and imports | Rename logger root/names to `nexus`; audit `zeus` strings |
| **TD-002** | P0 | Resolved (2026-11-12) | **Deployment gap** — systemd units not active | Services now auto-start | Maintain `systemctl --user enable --now` validation |
| **TD-003** | P2 | Resolved (2026-11-12) | **Lexical-only memory** | Hybrid lexical + vector search now implemented | Keep RRF path covered by tests |
| **TD-004** | P3 | Open | **Browser-based desktop** — `nexus desktop` opens Chrome | No native tray/shell | Build GTK4/libadwaita interface |
| **TD-005** | P2 | Open | **Narrow automation** — only 7 read-only diagnostics | Secure machinery exists but no safe write actions | Expand Tool Registry and sandbox new tools |
| **TD-006** | P4 | Partial | **Hardware limit** — 12 GB host cannot run 12B/31B locally | Remote Ollama used for larger models | Benchmark on GPU/high-RAM host |
| **TD-007** | P1 | Accepted | **API token plaintext** at `0600` | Same-user processes can read it | Keep loopback-only; optional secret re-delivery design |
| **TD-008** | P1 | Open | **Synchronous SQLite in async paths** | Event loop can block under load | Add `asyncio.to_thread`/`spawn_blocking` or async driver |
| **TD-009** | P2 | Open | **Sandbox runs as UID 0 inside bwrap** | Namespace escape would yield host root | Use unprivileged UID/GID inside sandbox |
| **TD-010** | P0 | Open | **`zeus` prefix in Python modules** | Naming inconsistency | Rename systematically (same as TD-001) |
| **TD-011** | P1 | Resolved (Fase 3) | **SudoBroker/Rollback/Execution Journal missing** | Audit and rollback controls now exist | Keep integrity tests |
| **TD-012** | P1 | Partial | **Multi-agent, simulation, GTK4** | Agents and simulation implemented; GTK4 missing | Build GTK4 GUI |
| **TD-013** | P2 | Open | **Rust daemon signals not handled** | No graceful SIGTERM/SIGINT shutdown | Revisit when Tokio `unix` feature is available |
| **TD-014** | P1 | Partial | **No deterministic planner without LLM** | Provider chain fallback exists; no LLM-free planner | Add deterministic fallback planner |
| **TD-015** | P1 | Open | **Execution Gate not integrated into AutomationEngine** | Centralized gate not invoked by every tool path | Adapt `AutomationEngine.run()` to call the gate |
| **TD-016** | P1 | Open | **Execution Gate uses fixed `autonomy_level=1`** | Policy ignores real caller autonomy | Accept autonomy as a parameter |
| **TD-017** | P2 | Resolved (2026-11-12) | **Event Fabric/network incomplete** | Outbox, dead-letter and WebSocket network implemented | Monitor retries and dead-letter queue |
| **TD-018** | P1 | Resolved (2026-11-12) | **LLM config pointed to missing Ollama models** | Every completion returned HTTP 404 | Config aligned with installed `nexusorbe-*` tags |
| **TD-019** | P2 | Open | **Background `Scheduler` not wired into runtime** | Periodic/cron-like tasks unavailable from runtime/API | Integrate and add tests |
| **TD-020** | P2 | Open | **Task Store encryption disabled by default** | Suspended task state is plaintext unless a key is supplied | Wire a key source into bootstrap or document deployment key |

## Resolved in the 2026-11-12 audit

- CLI `--config`/`--json` in nested subcommands.
- API approval-history route ordering and scenario paradigm validation.
- Event Fabric canonical payloads, legacy envelopes, dead-letter migration and metadata.
- Execution Journal integrity-hash recalculation.
- Simulation Results Store timestamps/reconstruction/integrity.
- `sqlite_utils.connect_db` with `busy_timeout`.
- Task Store atomic writes, empty-key handling and UTF-8 reads.
- VectorStore LanceDB API compatibility, SQL escaping and idempotent upsert.
- Distributed-network event timestamps/dedup and heartbeat clocks.
- Web HUD pending-approval controls.
- Local LLM model configuration.
