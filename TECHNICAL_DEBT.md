# NEXUS Technical Debt Register

| ID | Priority | State | Description | Impact | Next Action |
|---|---|---|---|---|---|
| **TD-001** | P0 | Open | **Branding Split** | Confusion in logs, DBs, and imports (`zeus` vs `nexus`). | Rename `nexus_core` to `nexus_core`. |
| **TD-002** | P0 | Open | **Deployment Gap** | Systemd units exist but are not active in environment. | Deploy and verify systemd units. |
| **TD-003** | P2 | Open | **Lexical-Only Memory** | Search is limited to keyword matching (FTS5). | Integrate local embeddings/vector store. |
| **TD-004** | P3 | Open | **Browser-based Desktop** | `nexus desktop` is just a Chrome window. | Build native GTK4 tray/shell. |
| **TD-005** | P4 | Open | **Narrow Automation** | Only 4 read-only commands allowed. | Design sandbox for broader actions. |
| **TD-006** | P2 | Open | **Hardware Limit** | Current host cannot run Gemma 4 12B/31B locally. | Benchmark on high-RAM/GPU host. |
