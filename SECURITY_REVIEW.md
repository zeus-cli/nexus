# SECURITY REVIEW 1.1 — NEXUS

**Updated:** 2026-11-12
**Validation:** 305 tests + 12 subtests passing; ruff/compileall/node-check/diff-check clean.

## Threat Model & Mitigations

- **Threat:** LLM approves its own action.
  - **Mitigation:** `ApprovalManager.decide()` is only reachable via `ApprovalAPI` → `ApprovalUI`. The LLM has no direct handle to the decision path.
- **Threat:** Parameter swapping after approval.
  - **Mitigation:** SHA-256 hash of canonicalized parameters and scope is checked at execution time.
- **Threat:** Replay attacks.
  - **Mitigation:** Atomic `CLAIMED → CONSUMED` transition before subprocess launch; expired approvals are rejected at claim time.
- **Threat:** Sandbox escape.
  - **Mitigation:** `bwrap` unshares all namespaces; strictly limited mount points; no network by default.
- **Threat:** SQL/filter injection in vector-memory filters.
  - **Mitigation:** LanceDB filter literals are escaped (`'` → `''`); vector `upsert` replaces the previous vector via `delete` + `add`.
- **Threat:** Event loss or duplicate delivery.
  - **Mitigation:** Event Fabric persists before publishing, uses canonical payloads/dedup keys, transactional outbox with retry/backoff and a dead-letter queue.
- **Threat:** Remote LLM data egress.
  - **Mitigation:** Privacy levels in the egress guard; remote providers require `authorized = true` and HTTPS.
- **Threat:** Corrupted execution audit trail.
  - **Mitigation:** Execution Journal hashes every entry and recalculates the hash on status changes; `verify_all_integrity()` detects tampering.

## Residual Risks

- **Process-level boundary:** security controls live inside the Python process. Arbitrary code execution in that process can bypass the Python-level gates.
- **Task Store at rest:** encryption (Fernet/XOR) is available but disabled unless a key is supplied. Default deployments store suspended task state in plaintext.
- **API token:** stored in plaintext at `0600`; any same-user process can read it.
- **Sandbox UID:** `bwrap` runs as UID 0 inside the namespace; namespace escape would yield host root.
- **Synchronous SQLite in async paths:** `connect_db` applies `busy_timeout`, but calls can still block the event loop under load.
- **No TLS:** intentional for loopback-only API; remote access requires a separate reviewed design.
- **Branding remnants:** `zeus` logger names remain; operational confusion rather than a vulnerability (TD-001/TD-010).

## Verification Notes

- Approval expiry: `claim_approval()` is the enforcement point; `decide()` intentionally accepts late decisions so the audit trail can record them.
- The Web HUD security panel uses `/api/v1/approvals/pending` and posts to `/api/v1/approvals/{id}/decide`.
- The API route order places `/approvals/history` before `/approvals/{request_id}` so the literal path is not captured as an ID.
