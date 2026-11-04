
"""Secure Automation Engine for the NEXUS Cognitive OS with HITL Integration."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Mapping
from pathlib import Path

from .events import Event, Severity
from .recorder import EventRecorder
from .sandbox import SandboxExecutor, SandboxResult
from .approval import ApprovalManager, ApprovalRequest, ApprovalStatus, ExecutionStatus
from .policy import PolicyEngine, PolicyContext, PolicyDecision
from .automation_types import Tool, RiskLevel, AccessLevel, ExecutionMode

# Security Constants
MAX_OUTPUT_BYTES = 16384

@dataclass(frozen=True, slots=True)
class ActionResult:
    tool_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }

class AutomationError(RuntimeError):
    """Base error for automation failures."""

class AutomationDenied(AutomationError):
    """Execution denied by security policy."""

class ApprovalRequired(AutomationError):
    """Execution requires human approval."""
    def __init__(self, request_id: str, justification: str):
        self.request_id = request_id
        self.justification = justification
        super().__init__(f"Approval required: {justification}")

# Tool definitions
CORE_TOOLS = {
    "sys.uptime": Tool("sys.uptime", "1.0", "Get system uptime", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/uptime",)),
    "sys.memory": Tool("sys.memory", "1.0", "Check memory usage", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/free", "-h",)),
    "sys.disk": Tool("sys.disk", "1.0", "Check disk usage", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/df", "-h",)),
    "sys.kernel": Tool("sys.kernel", "1.0", "Kernel version", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/uname", "-sr",)),
    "proc.list": Tool("proc.list", "1.0", "List top processes", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/ps", "aux",)),
    "proc.info": Tool("proc.info", "1.0", "Process info by PID", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/ps", "-p",), idempotent=True),
    "svc.status": Tool("svc.status", "1.0", "Check service status", RiskLevel.LOW, AccessLevel.READ, ExecutionMode.HOST, argv_template=("/usr/bin/systemctl", "status",), idempotent=True),
}

Runner = Callable[[tuple[str, ...], float], Awaitable[tuple[int, bytes, bytes]]]

class AutomationEngine:
    """Modular execution engine with risk-based gating and HITL integration."""

    def __init__(
        self,
        autonomy_level: int,
        recorder: EventRecorder,
        *,
        tools: Mapping[str, Tool] | None = None,
        default_timeout: float = 10.0,
        runner: Runner | None = None,
        approval_manager: ApprovalManager | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._autonomy_level = autonomy_level
        self._recorder = recorder
        self._tools = tools or CORE_TOOLS
        self._default_timeout = default_timeout
        self._runner = runner or _run_fixed_process
        self._sandbox = SandboxExecutor(recorder)
        self._approval_manager = approval_manager or ApprovalManager()
        self._policy_engine = policy_engine or PolicyEngine()

    def actions(self) -> list[dict[str, Any]]:
        """Public catalogue consumed by the CLI, TUI and HTTP API."""
        return self.list_tools()

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "id": tool.id,
                "version": tool.version,
                "description": tool.description,
                "risk": tool.risk.name,
                "access": tool.access.name,
                "mode": tool.execution_mode.name,
                "idempotent": tool.idempotent,
                "required_autonomy": tool.required_autonomy,
                "allowed": self._autonomy_level >= tool.required_autonomy,
            }
            for tool in self._tools.values()
        ]

    async def run(
        self, 
        tool_id: str, 
        args: tuple[str, ...] = (), 
        approval_id: str | None = None, 
        task_id: str = "unknown", 
        plan_id: str = "unknown",
        fs_scope: str = "DEFAULT",
        net_scope: str = "NONE",
        resource_limits: str = "default"
    ) -> ActionResult:
        tool = self._tools.get(tool_id)
        if tool is None:
            raise ValueError(f"Tool not found: {tool_id!r}")

        # 1. Policy Evaluation
        context = PolicyContext(
            autonomy_level=self._autonomy_level,
            fs_scope=fs_scope,
            net_scope=net_scope,
            resource_limits=resource_limits,
            task_context=task_id
        )
        
        decision, reason = self._policy_engine.evaluate(tool, context)
        
        if decision == PolicyDecision.DENY:
            await self._recorder.record(Event(kind="automation.denied", source="automation", payload={"tool_id": tool.id, "reason": reason}, severity=Severity.WARNING))
            raise AutomationDenied(f"Policy DENY: {reason}")

        # 2. Approval Handling
        effective_approval_id = approval_id
        
        if decision == PolicyDecision.REQUIRE_APPROVAL:
            if approval_id is None:
                req = ApprovalRequest(
                    tool_id=tool.id,
                    params={"args": args},
                    task_id=task_id,
                    plan_id=plan_id,
                    execution_mode=tool.execution_mode.value,
                    access_level=tool.access.value,
                    fs_scope=fs_scope,
                    net_scope=net_scope,
                    resource_limits=resource_limits,
                    policy_version=self._policy_engine.policy_version,
                    justification=reason
                )
                rid = self._approval_manager.request_approval(req)
                await self._recorder.record(Event(kind="automation.approval.requested", source="automation", payload={"request_id": rid, "tool_id": tool.id}))
                raise ApprovalRequired(rid, reason)
            
            req = ApprovalRequest(
                tool_id=tool.id,
                params={"args": args},
                task_id=task_id,
                plan_id=plan_id,
                execution_mode=tool.execution_mode.value,
                access_level=tool.access.value,
                fs_scope=fs_scope,
                net_scope=net_scope,
                resource_limits=resource_limits,
                policy_version=self._policy_engine.policy_version,
                justification=reason
            )
            if not self._approval_manager.verify_action(approval_id, req):
                raise AutomationDenied("Approval hash mismatch. Action parameters were modified.")
        
        # 3. Execution Protocol: CLAIMED -> CONSUMED (STARTING)
        if approval_id:
            if not self._approval_manager.claim_approval(approval_id):
                raise AutomationDenied("Approval not available, expired or already consumed.")
            
            # Mark as CONSUMED and STARTING before actual subprocess launch
            self._approval_manager.mark_starting(approval_id)
            effective_approval_id = approval_id
        elif decision == PolicyDecision.ALLOW:
            effective_approval_id = "auto-allowed"
        else:
            raise AutomationDenied("Execution attempted without required approval.")

        # 4. Actual Execution
        full_argv = tool.argv_template + args
        started = time.monotonic()
        
        await self._recorder.record(
            Event(
                kind="automation.action.started",
                source="automation",
                payload={"tool_id": tool.id, "args": args, "mode": tool.execution_mode.name, "approval_id": effective_approval_id},
                dedup=False,
            )
        )

        try:
            timeout = tool.timeout_s if hasattr(tool, "timeout_s") else self._default_timeout
            
            if tool.execution_mode == ExecutionMode.SANDBOX:
                res = await self._sandbox.execute(full_argv, timeout_s=timeout)
                code, stdout, stderr = res.exit_code, res.stdout, res.stderr
                truncated = res.truncated
            else:
                code, stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    self._runner(full_argv, timeout), timeout=timeout + 0.5
                )
                stdout, stderr, truncated = _decode_bounded(stdout_bytes, stderr_bytes)
            
            # Update execution status to reflect outcome
            status = ExecutionStatus.COMPLETED if code == 0 else ExecutionStatus.FAILED
            
        except asyncio.CancelledError:
            await self._record_terminal("automation.action.cancelled", tool, started, None)
            if effective_approval_id != "auto-allowed":
                self._approval_manager.update_execution_status(effective_approval_id, ExecutionStatus.INTERRUPTED)
            raise
        except Exception as exc:
            await self._record_terminal("automation.action.failed", tool, started, type(exc).__name__)
            if effective_approval_id != "auto-allowed":
                self._approval_manager.update_execution_status(effective_approval_id, ExecutionStatus.FAILED)
            if isinstance(exc, asyncio.TimeoutError):
                raise AutomationError(f"Tool {tool.id} timed out after {timeout}s") from exc
            raise AutomationError(f"Execution failed: {type(exc).__name__}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        
        result = ActionResult(
            tool_id=tool.id,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=truncated,
        )

        # 5. Finalize: Update final status
        if effective_approval_id != "auto-allowed":
            self._approval_manager.update_execution_status(
                effective_approval_id,
                status,
                summary=f"Exit: {code} | {stdout[:100].strip()}"
            )

        await self._recorder.record(
            Event(
                kind="automation.action.completed",
                source="automation",
                severity=Severity.INFO if code == 0 else Severity.WARNING,
                payload={
                    "tool_id": tool.id,
                    "exit_code": code,
                    "duration_ms": duration_ms,
                    "truncated": result.truncated,
                    "approval_id": effective_approval_id,
                },
                dedup=False,
            )
        )
        return result

    async def _record_terminal(
        self,
        kind: str,
        tool: Tool,
        started: float,
        error_type: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "tool_id": tool.id,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if error_type:
            payload["error_type"] = error_type
        await self._recorder.record(
            Event(
                kind=kind,
                source="automation",
                severity=Severity.WARNING,
                payload=payload,
                dedup=False,
            )
        )

async def _run_fixed_process(
    argv: tuple[str, ...], timeout_s: float
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/",
        env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    return int(process.returncode or 0), stdout, stderr

def _decode_bounded(stdout_bytes: bytes, stderr_bytes: bytes) -> tuple[str, str, bool]:
    stdout = stdout_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    truncated = len(stdout_bytes) > MAX_OUTPUT_BYTES or len(stderr_bytes) > MAX_OUTPUT_BYTES
    return stdout, stderr, truncated
