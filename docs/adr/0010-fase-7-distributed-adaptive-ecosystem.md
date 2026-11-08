# ADR 0010 — Distributed & Adaptive Ecosystem (Fase 7)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/distributed_node.py`, `distributed_node_manager.py`, `distributed_resources.py`, `distributed_scheduler.py`, `experience_consolidation.py`, `self_improvement.py`
- **Depende de:** ADR 0001–0009 (Node Manager depende de EventStore/EventBus existentes)

---

## Contexto

A especificação mestre (seção 17 e Fase 7) exige:

* Quatro perfis de recurso: MINIMAL, STANDARD, ADVANCED, DISTRIBUTED.
* Arquitetura distribuída: múltiplos nós, provedores remotos, agentes distribuídos, agendamento de carga.
* Gerenciamento avançado de recursos: CPU, RAM, GPU, disco, rede.
* Consolidação de experiência entre nós.
* Autoaperfeiçoamento controlado com governança humana.
* Testes de confiabilidade a longo prazo.

O backend Python já possuía monitor de recursos local (`resources.py`), mas faltava a visão distribuída, o agendamento entre nós e o autoaperfeiçoamento governado.

---

## Decisão 1 — Distributed Node e Node Manager

Criado `DistributedNode` (identidade, estado, recursos, capacidades) e `NodeManager` (registro, heartbeats, detecção de nós inalcançáveis, inventário centralizado).

* Heartbeat com timeout configurável para detecção de falha.
* Nós reportam recursos locais (auto-relato).
* O controller mantém o inventário e expõe `cluster_summary()`.
* Reentrada automática de nós que se reconectam (mesmo endereço).

---

## Decisão 2 — Distributed Resource Monitor

Criado `DistributedResourceMonitor` que agrega recursos locais e remotos em `ClusterResourceView` (CPU, RAM, GPU, disco, rede).

* `find_best_node_for_task()` seleciona o nó com mais recursos livres proporcionais.
* Suporte a requisito de GPU e papel preferido.
* Integra com o `ResourceMonitor` local existente (psutil).

---

## Decisão 3 — Distributed Task Scheduler

Criado `DistributedTaskScheduler` que agenda tarefas para nós com recursos disponíveis.

* Fila de prioridade (heap) ordenada por prioridade e ordem de inserção.
* Failover: se o nó atribuído ficar inalcançável, as tarefas são reenfileiradas (`handle_node_unreachable`).
* Retry com limite de tentativas (`max_attempts`).
* Isolamento de falhas: falha de um nó não afeta outras tarefas.

---

## Decisão 4 — Experience Consolidation Engine

Criado `ExperienceConsolidationEngine` que consolida experiências validadas de múltiplos nós.

* Apenas experiências validadas (status VALIDATED + confiança >= 0.7) se tornam reutilizáveis.
* Proveniência completa (nó de origem, tarefa de origem).
* Detecção de padrões recorrentes via agrupamento por tags.
* Contador de reuso para rastrear aproveitamento.

---

## Decisão 5 — Controlled Self-Improvement

Criado `SelfImprovementEngine` que gerencia propostas de melhoria com governança.

* Melhorias são propostas, não aplicadas automaticamente.
* Mudanças de política de segurança SEMPRE exigem aprovação humana (`requires_human_approval=True`).
* Políticas de segurança congeladas por padrão (`security_policies_frozen=True`).
* Fluxo: PROPOSED → UNDER_REVIEW → APPROVED → APPLIED/ROLLED_BACK/REJECTED.
* Plano de rollback obrigatório para reversão.
* Nunca modifica políticas de segurança automaticamente.

---

## Consequências

* **Positivo:** suporte a múltiplos nós, agendamento baseado em recursos, failover, autoaperfeiçoamento governado, experiência consolidada.
* **Negativo (Fase 7):** comunicação entre nós ainda usa o IPC/local existente; rede remota (UDS sobre TCP/WebSocket) fica para integração futura com o `nexus-daemon` Rust.
* **Risco conhecido:** o `DistributedTaskScheduler` roda em memória no nó controller; persistência de tarefas em banco fica para integração com o Event Store/Execution Journal.

---

## Testes

* `tests/test_distributed.py`: 20 testes cobrindo Node, Node Manager, Resources, Scheduler, Experience Consolidation e Self-Improvement.
* Regressão completa: 250 testes passando.
