
"""Deterministic Policy Engine for NEXUS Automation."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Optional
from dataclasses import dataclass
from .automation_types import Tool, RiskLevel, AccessLevel, ExecutionMode

class PolicyDecision(Enum):
    ALLOW = auto()
    REQUIRE_APPROVAL = auto()
    DENY = auto()

@dataclass(frozen=True, slots=True)
class PolicyContext:
    autonomy_level: int
    fs_scope: str
    net_scope: str
    resource_limits: str
    task_context: str | None = None

class PolicyEngine:
    """Evaluates if a tool execution is permitted based on risk and environment."""

    def __init__(self, policy_version: str = "1.0") -> None:
        self.policy_version = policy_version

    def evaluate(
        self, 
        tool: Tool, 
        context: PolicyContext
    ) -> tuple[PolicyDecision, str]:
        """Determines the decision for a tool execution."""
        
        if tool.risk == RiskLevel.CRITICAL:
            return PolicyDecision.DENY, "Critical tools are denied by default policy."

        if tool.risk == RiskLevel.HIGH:
            return PolicyDecision.REQUIRE_APPROVAL, "High risk operations require explicit human approval."

        if tool.risk == RiskLevel.MEDIUM:
            return PolicyDecision.REQUIRE_APPROVAL, "Medium risk operations require human verification."

        if tool.risk == RiskLevel.LOW:
            if tool.access == AccessLevel.READ:
                if context.autonomy_level >= tool.required_autonomy:
                    return PolicyDecision.ALLOW, "Low risk read operation permitted."
                return PolicyDecision.REQUIRE_APPROVAL, "Insufficient autonomy for low-risk read."
            
            if tool.access == AccessLevel.WRITE:
                if context.fs_scope == "SAFE_ZONE":
                    return PolicyDecision.ALLOW, "Low risk write in safe zone permitted."
                return PolicyDecision.REQUIRE_APPROVAL, "Low risk write outside safe zone requires approval."

        return PolicyDecision.DENY, "No matching policy rule found. Denying by default."
