
# PERFORMANCE BASELINE 1.0 — NEXUS

## Target Hardware: Intel Low-Power, 12GB RAM

### Baseline Metrics
- **Idle RAM**: ~150-300 MB (Python + SQLite + LanceDB).
- **Idle CPU**: < 1%
- **Execution Latency**: 
    - Policy Evaluation: < 1ms
    - Approval Check: < 5ms
    - Sandbox Overhead: ~50-100ms startup.
- **Persistence Cost**: SQLite WAL mode ensures minimal write latency.

### Resource Pressure Behavior
- **Memory**: LanceDB's memory-mapping allows handling larger datasets without exhausting RAM.
- **CPU**: Reflection loop adds minimal overhead; the primary cost is LLM inference.
