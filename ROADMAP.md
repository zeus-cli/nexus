# NEXUS Engineering Roadmap

## P0 — Critical Infrastructure (Foundation & Unity)
- [x] **Unified Branding:** Refactor `nexus_core` to `nexus_core` across all files, DBs, and services.
- [x] **Deployment Validation:** Install and enable systemd units; verify auto-start and Sentinel IPC.
- [x] **Environment Standardization:** Finalize `pyproject.toml` and `requirements.lock` for target Linux Mint/Ubuntu.

## P1 — Core Cognitive Functionality (The "Brain")
- [x] **Task Planning:** Implement a multi-step planner (Reasoning loop).
- [x] **Reflective Loop:** Implement the Reflection Engine for self-correction and goal validation.
- [x] **Sandbox Execution:** Implement Bubblewrap-based isolated execution for safe tool usage.
- [ ] **Agent Coordination:** Support for specialized sub-agents.
- [ ] **Execution Lifecycle:** Expand automation from "diagnostics" to "safe actions" with human-in-the-loop approval.

## P2 — AI & Memory Infrastructure (Knowledge)
- [ ] **Semantic Memory:** Integrate a local vector store (e.g., LanceDB or Chroma) and embeddings.
- [ ] **Memory Consolidation:** Implement logic to move short-term memories to long-term episodic records.
- [ ] **Data Egress Policy:** Implement explicit user consent for cloud-provider context sending.

## P3 — Desktop & Web Interfaces (Experience)
- [ ] **Native Tray:** Build a GTK4/libadwaita system tray application.
- [ ] **Native Shell:** Implement a "Command Palette" style interface for the desktop.
- [ ] **Enhanced Dashboard:** Add real-time memory visualization and model performance metrics.

## P4 — Advanced Autonomy (Agency)
- [ ] **Safe Shell:** Implementation of a sandboxed shell for arbitrary command execution with rollback.
- [ ] **Scheduler:** General-purpose task scheduler for background automation.
- [ ] **External Integrations:** Connect to local apps via DBus or API.