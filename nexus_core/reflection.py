"""Reflection Engine for the NEXUS Cognitive Core."""

from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass
from typing import Any, Optional
from .llm_router.gateway import LLMGateway

class ReflectionDecision(Enum):
    SUCCESS = auto()            # Goal achieved
    RETRY = auto()               # Redo a safe operation
    REPLAN = auto()              # Change strategy
    REQUEST_APPROVAL = auto()    # Human intervention needed
    ABORT = auto()               # Irrecoverable failure
    PARTIAL_SUCCESS = auto()     # Goal partially achieved

@dataclass(frozen=True, slots=True)
class ReflectionResult:
    decision: ReflectionDecision
    justification: str
    suggested_action: str | None = None

class ReflectionEngine:
    """Evaluates task execution and determines the next cognitive step."""

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def critique(
        self,
        goal: str,
        plan: list[Any],
        execution_results: list[Any],
        system_state: str | None = None,
        memory_context: str | None = None,
    ) -> ReflectionResult:
        """Analyzes evidence to decide if the goal was met."""
        
        # Construct a critique prompt
        evidence = "\n".join([
            f"Step: {r.step.description if hasattr(r, 'step') else 'Unknown'}\nResult: {r.output}" 
            for r in execution_results
        ])
        
        critique_system = (
            "You are the NEXUS Auditor. Your job is to objectively evaluate if the provided "
            "evidence satisfies the original goal. Be skeptical. Do not assume success just because "
            "the tool returned exit code 0. Check for incomplete data or contradictions.\n\n"
            "DECISIONS:\n"
            "- SUCCESS: Goal fully achieved.\n"
            "- RETRY: A safe tool failed or gave partial data.\n"
            "- REPLAN: The current strategy is wrong or blocked.\n"
            "- REQUEST_APPROVAL: A sensitive operation is needed.\n"
            "- ABORT: The goal is impossible or dangerous.\n"
            "- PARTIAL_SUCCESS: Most of the goal is met, but some parts are missing.\n"
            "\nOutput MUST be a JSON object: {\"decision\": \"SUCCESS/RETRY/REPLAN/etc\", \"justification\": \"...\", \"suggested_action\": \"...\"}"
        )
        
        prompt = f"Goal: {goal}\n\nPlan:\n{plan}\n\nExecution Evidence:\n{evidence}\n\nSystem State: {system_state}\nMemory: {memory_context}"
        
        try:
            res = await self._gateway.complete(
                prompt,
                task="critique",
                system=critique_system,
                json_mode=True,
            )
            import json
            data = json.loads(res.text)
            
            decision_map = {
                "SUCCESS": ReflectionDecision.SUCCESS,
                "RETRY": ReflectionDecision.RETRY,
                "REPLAN": ReflectionDecision.REPLAN,
                "REQUEST_APPROVAL": ReflectionDecision.REQUEST_APPROVAL,
                "ABORT": ReflectionDecision.ABORT,
                "PARTIAL_SUCCESS": ReflectionDecision.PARTIAL_SUCCESS,
            }
            
            return ReflectionResult(
                decision=decision_map.get(data.get("decision"), ReflectionDecision.REPLAN),
                justification=data.get("justification", "No justification provided."),
                suggested_action=data.get("suggested_action"),
            )
        except Exception as e:
            # Fallback to REPLAN on failure to critique
            return ReflectionResult(
                decision=ReflectionDecision.REPLAN,
                justification=f"Critique failed: {str(e)}",
            )