
# SECURITY REVIEW 1.0 — NEXUS

## Threat Model & Mitigations
- **Threat**: LLM approves its own action.
  - **Mitigation**: `ApprovalManager.decide()` is only reachable via `ApprovalAPI` $\rightarrow$ `ApprovalUI`.
- **Threat**: Parameter swapping after approval.
  - **Mitigation**: SHA-256 hash check of canonicalized parameters at the moment of execution.
- **Threat**: Sandbox escape.
  - **Mitigation**: `bwrap` unshares all namespaces; strictly limited mount points.
- **Threat**: Replay attacks.
  - **Mitigation**: Atomic `CLAIMED` $\rightarrow$ `CONSUMED` transition.

## Residual Risks
- **Process-Level Boundary**: Security controls exist within the Python process. If an attacker gains arbitrary code execution within the `CognitiveCore` process, they can bypass the Python-level gates.
- **TaskStore**: State is stored in plaintext JSON. For high-security environments, this should be encrypted.
