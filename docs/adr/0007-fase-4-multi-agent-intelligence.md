# ADR 0007 — Multi-Agent Intelligence: Agent Registry, Runtime, Orchestrator, Collaboration, Adversarial Review (Fase 4)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/agent_manifest.py`, `agent_registry.py`, `agent_runtime.py`, `task_dispatcher.py`, `agent_orchestrator.py`, `collaboration_engine.py`, `adversarial_review.py`, `agent_lifecycle.py`
- **Depende de:** ADR 0001–0006 (Execution Gate, Tool Registry existentes)

---

## Contexto

A especificação mestre (seção 10) exige um ecossistema de agentes especializados com:

* Agent Registry, Agent Manifest, Agent Runtime, Agent Orchestrator
* Task Dispatcher, Shared Task Context, Communication Protocol
* Collaboration Engine, Adversarial Review, Agent Lifecycle Manager
* 9 agentes iniciais: ORCHESTRATOR, RESEARCHER, ANALYST, STRATEGIST, ENGINEER, CRITIC, SIMULATOR, SECURITY, MEMORY CURATOR

Princípios: agentes declaram capacidades explicitamente; operam dentro de limites de permissão; nunca bypassam o Execution Gate; execução paralela com isolamento de falhas; agentes podem discordar; consenso não equivale a correção.

---

## Decisão 1 — Agent Manifest (`agent_manifest.py`)

Cada agente declara identidade, papel (ORCHESTRATOR/RESEARCHER/ANALYST/STRATEGIST/ENGINEER/CRITIC/SIMULATOR/SECURITY/MEMORY_CURATOR), capacidades, perfil de modelo, escopo de memória, acesso a ferramentas, limites de permissão, limites de recurso, timeout e esquema de saída. Inclui `has_tool_access()` e `has_capability()` para validação.

---

## Decisão 2 — Agent Registry (`agent_registry.py`)

Registro central com validação de manifesto, prevenção de IDs duplicados, estado de ciclo de vida (REGISTERED/ACTIVE/IDLE/BUSY/SUSPENDED/TERMINATED/ERROR) e consultas por papel.

---

## Decisão 3 — Agent Runtime (`agent_runtime.py`)

Executa agentes isoladamente. Valida que o agente só usa ferramentas para as quais tem acesso declarado e capacidades requeridas. Integra opcionalmente com o Execution Gate (Fase 3) — cada execução de agente passa pelo gate de autorização. Suporta handlers síncronos/assíncronos com timeout, isolamento de falhas (uma falha não derruba outros agentes), retry e cancelamento.

---

## Decisão 4 — Task Dispatcher (`task_dispatcher.py`)

Atribui tarefas a agentes com base em capacidades, ferramentas necessárias, papel preferido e carga atual (exclui agentes BUSY). Suporta despacho paralelo com `asyncio.gather` e isolamento de falhas (`return_exceptions=True`).

---

## Decisão 5 — Agent Orchestrator (`agent_orchestrator.py`)

Decompoe objetivos complexos em tarefas, despacha para agentes especializados (paralelo ou sequencial), consolida resultados e reporta sucesso/falha por tarefa. Não impõe consenso artificial.

---

## Decisão 6 — Collaboration Engine (`collaboration_engine.py`)

Agentes compartilham contribuições para uma tarefa compartilhada. A consolidação preserva contribuições individuais com confiança, detecta conflitos (mesma chave com valores diferentes entre agentes) e gera consenso simples (valor de maior confiança). Não impõe consenso quando há conflito.

---

## Decisão 7 — Adversarial Review (`adversarial_review.py`)

Revisão crítica de decisões importantes. Cada revisor analisa independentemente e pode discordar do tomador. Vereditos: APPROVED/REJECTED/NEEDS_REVISION/INCONCLUSIVE. Consenso só quando todos aprovam ou todos rejeitam. Auto-revisão não permitida (INCONCLUSIVE).

---

## Decisão 8 — Agent Lifecycle Manager (`agent_lifecycle.py`)

Gerencia spawn, activate, suspend, resume, terminate e registro de resultados de tarefa por agente (contagem de sucesso/falha).

---

## Consequências

* **Positivo:** ecossistema multi-agente completo, testável, integrado ao Execution Gate; isolamento de falhas; agentes podem discordar.
* **Negativo:** o `AdversarialReview` usa revisão automática heurística na Fase 4; revisores LLM reais são integrados quando os agentes especializados forem implementados com modelos próprios.
* **Risco conhecido:** o Agent Runtime ainda não integra dinamicamente com o Tool Registry para validação de schemas de entrada/saída; fica para consumo pelo Planner (Fase 2) quando planos acionam agentes.

---

## Testes

* `tests/test_multi_agent.py`: 18 testes cobrindo Manifest, Registry, Runtime, Dispatcher, Orchestrator, Collaboration, Adversarial Review e Lifecycle.
* Regressão completa: 209 testes passando.
