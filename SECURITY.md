

## HITL Execution Gate
Toda execução de ferramenta no NEXUS deve atravessar o Execution Gate:
Tool Registry -> PolicyEngine -> ApprovalManager -> Execution Reservation -> Executor -> Result Validator -> Audit Log.

- Decisões da PolicyEngine: ALLOW, REQUIRE_APPROVAL, DENY.
- Vínculo Criptográfico: Aprovações são vinculadas ao SHA-256 de (tool_id, params, task_id, plan_id, mode, access, scope, policy_version).
- Protocolo de Consumo: At-most-once. Status: PENDING -> APPROVED -> CLAIMED -> CONSUMED.

## HITL Lifecycle and Recovery
The HITL system enforces a strict state transition to prevent double-execution:
APPROVED -> CLAIMED -> CONSUMED (STARTING) -> COMPLETED/FAILED.

- Consumption occurs BEFORE subprocess launch.
- Orphan Recovery: Approvals stuck in CLAIMED/STARTING for > 5 mins are marked UNKNOWN.
- Bypass Prevention: SandboxExecutor is used strictly via AutomationEngine.
- Human Authentication: All decisions must be signed by an authenticated APIUser.
