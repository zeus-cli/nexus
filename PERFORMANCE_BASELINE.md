# PERFORMANCE BASELINE 1.1 — NEXUS

**Updated:** 2026-11-12

## Target Hardware: Intel Low-Power, 12 GB RAM

### Baseline Metrics

| Metric | Reference | Notes |
|---|---|---|
| **Idle RAM** | ~150–300 MB | Python + SQLite + LanceDB; heavy ML imports only when used |
| **Idle CPU** | < 1% | No background inference |
| **Policy evaluation** | < 1 ms | Deterministic rules |
| **Approval check** | < 5 ms | SQLite WAL + `busy_timeout` |
| **Sandbox startup** | ~50–100 ms | Bubblewrap namespaces |
| **Full test suite** | ~129 s | 305 tests + 12 subtests (2026-11-12) |
| **Local Ollama completion** | ~12.2 s | `nexusorbe-llama3.1:latest`, 16 max output tokens, measured via `nexus llm complete` |
| **Remote OpenCode / Gemma 4** | ~28 s | Short agent run, remote Ollama at 4096 context |
| **Remote OpenCode / Qwen3** | ~107 s | Short agent run; host/model latency, not NEXUS |

### Persistence Cost

SQLite WAL mode keeps write latency low. The `connect_db` helper applies `busy_timeout`, explicit commit/rollback and guaranteed close to reduce `database is locked` failures under concurrent access.

### Resource Pressure Behavior

- **Memory:** LanceDB memory-mapping handles larger datasets without exhausting RAM. Sentence-Transformers is loaded lazily with a 5 s timeout; a cold import can exceed that on this host.
- **CPU:** the reflection loop adds minimal overhead; the primary cost is LLM inference.
- **Disk:** retention is disabled by default (`events.retention_days = 0`), so the database can grow without bound. Use explicit retention or backups.
- **Network:** core operation is loopback-only; Ollama is optional. Remote OpenCode models depend on network latency and host load.

### Caveats

- The test-suite timing varies with filesystem cache and whether `sentence_transformers`/LanceDB are already warm.
- Remote Ollama context is fixed at 4096 tokens by the host; `options.num_ctx` via the OpenAI-compatible endpoint is ignored.
- The Qwen3 model spends tokens on reasoning; short `max_tokens` limits can truncate its answer even though the request succeeds.
