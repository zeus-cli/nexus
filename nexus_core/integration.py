"""NEXUS Integration Layer — integração dos componentes entre si.

Responsabilidade: conectar os módulos criados nas Fases 2-7 para que
funcionem como um sistema unificado, não como módulos isolados.

Integrações:
* Cognitive Core + Planner Engine + Agent Runtime (planos são executados por agentes)
* Experience Consolidation + Self-Improvement (experiências validadas geram propostas)
* Execution Gate + Automation Engine + Agent Runtime (todas as execuções passam pelo gate)
"""

from __future__ import annotations

from typing import Any

from .agent_manifest import AgentManifest, AgentRole
from .agent_orchestrator import AgentOrchestrator
from .agent_registry import AgentRegistry
from .agent_runtime import AgentRuntime
from .cognitive import CognitiveCore
from .decision_engine import DecisionEngine, DecisionOption, ApprovalRequirement
from .experience_consolidation import ExperienceConsolidationEngine, ExperienceType
from .execution_gate import ExecutionGate, GateDecision
from .intent_engine import IntentEngine
from .knowledge_engine import KnowledgeEngine, KnowledgeKind, KnowledgeSource
from .planner_engine import PlannerEngine, PlanStep
from .reflection import ReflectionEngine


class NEXUSIntegration:
    """Camada de integração que conecta os componentes do NEXUS."""

    def __init__(
        self,
        cognitive: CognitiveCore,
        intent_engine: IntentEngine,
        planner: PlannerEngine,
        decision_engine: DecisionEngine,
        agent_registry: AgentRegistry,
        agent_runtime: AgentRuntime,
        agent_orchestrator: AgentOrchestrator,
        execution_gate: ExecutionGate,
        knowledge_engine: KnowledgeEngine,
        experience_engine: ExperienceConsolidationEngine,
        reflection: ReflectionEngine,
    ) -> None:
        self.cognitive = cognitive
        self.intent_engine = intent_engine
        self.planner = planner
        self.decision_engine = decision_engine
        self.agent_registry = agent_registry
        self.agent_runtime = agent_runtime
        self.agent_orchestrator = agent_orchestrator
        self.execution_gate = execution_gate
        self.knowledge_engine = knowledge_engine
        self.experience_engine = experience_engine
        self.reflection = reflection

    # ------------------------------------------------------------------ #
    # Fluxo cognitivo completo: Intent → Context → Plan → Decide → Execute
    # ------------------------------------------------------------------ #

    async def process_intent(
        self,
        prompt: str,
        *,
        task_id: str,
        system: str | None = None,
    ) -> dict[str, Any]:
        """Processa uma solicitação através de todo o pipeline cognitivo.

        Etapas:
        1. Intent Engine interpreta o prompt.
        2. Conhecimento é registrado (proveniência do usuário).
        3. Planner Engine gera um plano.
        4. Decision Engine avalia se o plano requer aprovação.
        5. Se aprovado, o plano é criado e retornado para execução.
        """
        # 1. Interpretação de intenção.
        intent = await self.intent_engine.interpret(prompt)

        # 2. Registra a intenção no conhecimento (proveniência: usuário).
        self.knowledge_engine.add(
            kind=KnowledgeKind.ASSUMPTION,
            content=f"User intent: {intent.objective} ({intent.intent_type.name})",
            source=KnowledgeSource.USER,
            source_ref=task_id,
            confidence=intent.confidence,
            metadata={"task_id": task_id, "intent_type": intent.intent_type.name},
        )

        # 3. Geração de plano.
        plan = self.planner.generate_plan(
            objective=intent.objective,
            intent_capabilities=intent.required_capabilities,
            risk_classification="low",
        )
        self.planner.start_plan(plan.plan_id)

        # 4. Decisão sobre o plano (avalia se requer aprovação humana).
        options = [
            DecisionOption(
                option_id="execute_plan",
                description=f"Execute plan: {intent.objective}",
                expected_outcome="Goal achieved via planned steps",
                evidence_quality=0.7,
                operational_risk="low",
                resource_consumption="low",
                reversible=True,
                confidence=0.8,
            ),
            DecisionOption(
                option_id="abort",
                description="Abort plan execution",
                expected_outcome="Plan not executed",
                evidence_quality=0.9,
                operational_risk="low",
                resource_consumption="low",
                reversible=True,
                confidence=1.0,
            ),
        ]
        decision = self.decision_engine.decide(options)

        # 5. Registra decisão no conhecimento.
        self.knowledge_engine.add(
            kind=KnowledgeKind.ASSUMPTION,
            content=f"Decision: {decision.recommended_option.description}",
            source=KnowledgeSource.MODEL_INFERENCE,
            source_ref=task_id,
            confidence=decision.score,
            metadata={
                "task_id": task_id,
                "approval_required": decision.approval_requirement.name,
            },
        )

        return {
            "task_id": task_id,
            "intent": intent.as_dict(),
            "plan": plan.as_dict(),
            "decision": decision.as_dict(),
            "plan_id": plan.plan_id,
            "requires_human_approval": decision.approval_requirement
            == ApprovalRequirement.HUMAN,
        }

    # ------------------------------------------------------------------ #
    # Execução de plano via agentes (Fase 4)
    # ------------------------------------------------------------------ #

    async def execute_plan_via_agents(
        self,
        plan_id: str,
        *,
        task_descriptions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Executa um plano despachando tarefas para agentes especializados.

        Cada tarefa é despachada para o agente mais adequado com base em
        capacidades. O Execution Gate é consultado antes de cada execução.
        """
        plan = self.planner.get_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")

        results = []
        for spec in task_descriptions:
            # Despacha para o agente adequado.
            agent_id, task_id = await self.agent_orchestrator._dispatcher.dispatch(
                description=spec.get("description", "task"),
                input=dict(spec.get("input", {})),
                required_capabilities=tuple(spec.get("required_capabilities", ())),
                required_tools=tuple(spec.get("required_tools", ())),
                preferred_role=None,
            )

            # Verifica autorização via Execution Gate antes de executar.
            manifest = self.agent_registry.get_manifest(agent_id)
            gate = await self.execution_gate.authorize(
                component_id=f"agent-{agent_id}",
                tool_id="agent.execute",
                params=spec.get("input", {}),
                task_id=task_id,
                justification=f"agent task: {spec.get('description', 'task')}",
            )
            if gate.decision == GateDecision.DENIED:
                results.append({
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": "denied",
                    "reason": gate.reason,
                })
                continue

            # Executa a tarefa.
            result = await self.agent_runtime.execute_task(task_id)
            results.append({
                "task_id": task_id,
                "agent_id": agent_id,
                "status": result.status.name,
                "output": result.output,
                "error": result.error,
            })

            # Registra experiência no Experience Consolidation Engine.
            self.experience_engine.add_experience(
                experience_type=ExperienceType.TASK_SUCCESS
                if result.status.name == "COMPLETED"
                else ExperienceType.TASK_FAILURE,
                title=f"Task: {spec.get('description', 'task')}",
                description=f"Executed by agent {agent_id}",
                source_node_id="local",
                source_task_id=task_id,
                context={"plan_id": plan_id, "agent_id": agent_id},
                outcome={"status": result.status.name, "error": result.error},
                confidence=0.8 if result.status.name == "COMPLETED" else 0.3,
                tags=("task", "agent-execution"),
            )

        return {
            "plan_id": plan_id,
            "task_results": results,
        }

    # ------------------------------------------------------------------ #
    # Autoaperfeiçoamento baseado em experiência (Fase 7)
    # ------------------------------------------------------------------ #

    def propose_improvements_from_experience(
        self,
        *,
        min_reuse_count: int = 1,
    ) -> list[str]:
        """Gera propostas de autoaperfeiçoamento a partir de experiências validadas.

        Experiências com alto reuso são candidatas a se tornarem melhorias
        no sistema (ex.: otimização de fluxo, ajuste de recursos).
        """
        proposal_ids = []
        reusable = self.experience_engine.list_records(reusable_only=True)

        for record in reusable:
            if record.reuse_count < min_reuse_count:
                continue

            # Só propõe se a experiência for de sucesso (não falha).
            if record.experience_type != ExperienceType.TASK_SUCCESS:
                continue

            proposal_id = self._self_improvement_engine.propose_improvement(
                improvement_type=__import__("nexus_core.self_improvement", fromlist=["ImprovementType"]).ImprovementType.WORKFLOW_OPTIMIZATION,
                title=f"Optimize: {record.title}",
                description=record.description,
                rationale=f"Validated experience with {record.reuse_count} reuses",
                proposed_changes={
                    "source_record_id": record.record_id,
                    "optimization": record.title,
                },
                affected_components=("orchestrator", "runtime"),
                risk_level="low",
                source_experience_ids=(record.record_id,),
            )
            proposal_ids.append(proposal_id)

        return proposal_ids

    @property
    def _self_improvement_engine(self):
        from .self_improvement import SelfImprovementEngine
        if not hasattr(self, "_sie"):
            self._sie = SelfImprovementEngine()
        return self._sie

    # ------------------------------------------------------------------ #
    # Estatísticas do sistema integrado
    # ------------------------------------------------------------------ #

    def system_status(self) -> dict[str, Any]:
        """Retorna o status integrado de todos os componentes."""
        return {
            "agents": self.agent_registry.list_dict(),
            "plans": [p.as_dict() for p in self.planner.list_plans()],
            "experience_stats": self.experience_engine.stats(),
            "improvement_stats": self._self_improvement_engine.stats(),
        }


__all__ = ["NEXUSIntegration"]
