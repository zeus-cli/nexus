# NEXUS Master Architecture

**Versão:** 1.0  
**Projeto:** NEXUS — Cognitive Operating System (local-first, híbrido Rust + Python)  
**Classificação:** Arquitetura autoritativa — descreve o estado REAL do sistema (não apenas o desejado)

---

## 1. Visão Geral

NEXUS é um sistema operacional cognitivo local-first organizado em seis planos arquiteturais:

| Plano | Linguagem | Responsabilidade | Estado |
|-------|-----------|------------------|--------|
| **Control Plane** | Rust | Autoridade operacional: daemon, ciclo de vida, IPC, segurança determinística | **Parcial** (nexus-daemon v0.1.0 implementado; partes em Python) |
| **Cognitive Plane** | Python | Interpretação de objetivos, raciocínio, plano, memória, roteamento de modelos | **Parcial** (nexus_core v0.2.0 + 6 motores cognitivos da Fase 2) |
| **Intelligence Plane** | Python | Multi-agente: registry, runtime, orquestrador, colaboração, revisão adversária | **Implementado** (Fase 4) |
| **Simulation Plane** | Python | Simulação universal: mundos, entidades, regras, clock, scheduler, cenários, experimentos, métricas, análise | **Parcial** (Fase 5: tipos base, clock, state manager, rules engine, event scheduler, world builder, scenario manager, metrics, analyzer, results store) |
| **Execution Plane** | Python | Tool registry, automação, sandbox, adaptadores (FS/Process/Network/…) | **Parcial** (7 ferramentas de diagnóstico somente-leitura + Execution Gate, Tool Registry, Rollback Manager, SudoBroker, Execution Journal da Fase 3) |
| **Distributed & Adaptive Plane** | Python | Multi-nó, agendamento distribuído, gerenciamento avançado de recursos, consolidação de experiência, autoaperfeiçoamento controlado | **Parcial** (Fase 7: nós, node manager, resource monitor, task scheduler, experience consolidation, self-improvement) |

Os três sistemas (MIND, WORLD, OS) compartilham identidade, memória, contratos de evento, políticas de segurança e contexto operacional.

---

## 2. Topologia Atual (estado real)

```
USUÁRIO / CLI / TUI / WEB HUD
      │ HTTP/WS (127.0.0.1:8765, Bearer token)
      ▼
NEXUS API (FastAPI) ─► NEXUS COGNITIVE CORE (Python)
      │                  ├ Cognitive Core (loop Plan→Execute→Reflect)
      │                  ├ Automation Engine (7 ferramentas diag. read-only)
      │                  ├ Policy Engine + Approval Manager (HITL, SHA-256)
      │                  ├ LLM Gateway (Ollama + OpenAI-compatível)
      │                  ├ Memory Store (SQLite + FTS5 + LanceDB)
      │                  ├ Event Store + Event Bus (persistir antes de publicar)
      │                  ├ Sandbox (bwrap), Sentinel Receiver (UDS)
      │                  └ Task Store (tarefas suspensas, checksum SHA-256)
      │ UDS (newline-JSON)
      ▼
NEXUS SENTINEL (Rust, core-rust) ─► /proc telemetry + fs watch
```

**Novo (Fase 1):** `core/nexus-daemon/` (Rust, Tokio) — Control Plane em construção:
* barramento de eventos (memória) + store SQLite WAL com dedup;
* servidor IPC sobre Unix Domain Socket (protocolo JSON por linha, compatível com o sentinel);
* supervisor de ciclo de vida de serviços;
 * health check, logging estruturado, configuração TOML estrita;
 * security guard, policy engine e execution gate mínimos (Fase 1).

---

## 3. Planos Detalhados

### Control Plane (Rust)
Componentes: Nexus Daemon, System Supervisor, Identity Manager, Policy Engine, Execution Gate, Permission Manager, Approval Manager, Security Guard, Resource Manager, Service Lifecycle Manager.
* Não depende de LLM para segurança.
* Decisões de autorização determinísticas, explícitas e verificáveis.

### Cognitive Plane (Python)
Componentes: Cognitive Core, Intent Engine, Context Engine, Reasoning Engine, Knowledge Engine, Decision Engine, Planner, Reflection Engine, LLM Router, Memory Manager.

**Motores cognitivos da Fase 2 (implementados e testados):**

| Módulo | Arquivo | Responsabilidade |
|--------|---------|------------------|
| Intent Engine | `nexus_core/intent_engine.py` | Interpretação e classificação de solicitações (objetivo, intenção, restrições, resultado esperado, informação necessária, riscos, capacidades) |
| Context Engine | `nexus_core/context_engine.py` | Representações contextuais enxutas com retrieval + ranqueamento de relevância (máx. 8 blocos, 2400 chars) |
| Reasoning Engine | `nexus_core/reasoning_engine.py` | Raciocínio estruturado (decomposição, evidências, hipóteses, alternativas, incerteza, contradições) sem expor CoT privado |
| Decision Engine | `nexus_core/decision_engine.py` | Comparação de decisões candidatas (evidência, risco, recursos, reversibilidade, confiança, aprovação humana) |
| Planner Engine | `nexus_core/planner_engine.py` | Planos executáveis (ID, etapas, dependências, executor, ferramentas, recursos, risco, pré-condições, critérios de sucesso, falha, estado) |
| Knowledge Engine | `nexus_core/knowledge_engine.py` | Conhecimento com proveniência (fato/suposição/hipótese/predição/simulação); nunca promove simulação a fato |

O `Cognitive Core` (`cognitive.py`) integra esses motores de forma opcional e não destrutiva: quando fornecidos, enriquecem o loop adaptativo; quando ausentes, o comportamento existente é preservado.
* Loop: Perceber → Entender → Raciocinar → Simular → Planejar → Governar → Agir → Avaliar → Evoluir.
* Memória híbrida: episódica, semântica, procedimental, relacional, operacional, refletiva.

### Intelligence Plane
Componentes: Agent Registry, Agent Manifest, Agent Runtime, Agent Orchestrator, Multi-Agent Swarm, Collaboration Engine, Adversarial Review, Agent Lifecycle Manager.
* Agentes especializados: ORCHESTRATOR, RESEARCHER, ANALYST, STRATEGIST, ENGINEER, CRITIC, SIMULATOR, SECURITY, MEMORY CURATOR.
* Agentes nunca bypassam o Execution Gate.

**Componentes de Multi-Agent Intelligence da Fase 4 (implementados e testados):**

| Módulo | Arquivo | Responsabilidade |
|--------|---------|------------------|
| Agent Manifest | `nexus_core/agent_manifest.py` | Declaração de identidade, papel, capacidades, modelo, memória, ferramentas, limites de recurso, timeout, esquema de saída |
| Agent Registry | `nexus_core/agent_registry.py` | Registro central com validação de manifesto, estado de ciclo de vida, consultas por papel |
| Agent Runtime | `nexus_core/agent_runtime.py` | Execução isolada de agentes, validação de permissões, integração com Execution Gate, timeout, isolamento de falhas |
| Task Dispatcher | `nexus_core/task_dispatcher.py` | Atribuição de tarefas por capacidade/carga, despacho paralelo com isolamento de falhas |
| Agent Orchestrator | `nexus_core/agent_orchestrator.py` | Decomposição de objetivos em tarefas, despacho coordenado, consolidação de resultados |
| Collaboration Engine | `nexus_core/collaboration_engine.py` | Compartilhamento de contexto entre agentes, detecção de conflitos, consenso simples |
| Adversarial Review | `nexus_core/adversarial_review.py` | Revisão crítica de decisões; agentes podem discordar; consenso ≠ correção |
| Agent Lifecycle Manager | `nexus_core/agent_lifecycle.py` | Spawn, activate, suspend, resume, terminate, registro de resultados por agente |

### Simulation Plane
Componentes: World Builder, World Registry, Entity Manager, State Manager, Rules Engine, Event Scheduler, Simulation Clock, Scenario Manager, Experiment Runner, Metrics Engine, Simulation Analyzer, Results Store.
* 10 paradigmas: Agent-Based Modeling, Discrete-Event, Rule-Based, Mathematical, Probabilistic, Social, Economic, Network, Technical, Digital Twin.
* Mundos paralelos isolados com random seed e estado próprio.
* Resultados explicitamente labelados como simulados/estimados/inferidos (não predições garantidas).

**Componentes de Universal Simulation da Fase 5 (implementados e testados):**

| Módulo | Arquivo | Responsabilidade |
|--------|---------|------------------|
| Simulation Types | `nexus_core/simulation_types.py` | Contratos base: paradigmas, estados, entidades, eventos, definição de mundo, resultado |
| Simulation Clock | `nexus_core/simulation_clock.py` | Relógio de simulação por mundo (step-based ou contínuo, pausa/retomada) |
| State Manager | `nexus_core/state_manager.py` | Estado do mundo, snapshots, restauração, reprodutibilidade |
| Rules Engine | `nexus_core/rules_engine.py` | Regras declarativas determinísticas (condição → ação) |
| Event Scheduler | `nexus_core/event_scheduler.py` | Fila de eventos discretos ordenada por timestamp |
| World Builder | `nexus_core/world_builder.py` | Construção fluente de WorldDefinition; registro de mundos isolados |
| Scenario Manager | `nexus_core/scenario_manager.py` | Cenários baseline/alternativas; experimentos (Monte Carlo, sensibilidade, repetições) |
| Metrics Engine | `nexus_core/metrics_engine.py` | Coleta e agregação de métricas por mundo |
| Simulation Analyzer | `nexus_core/simulation_analyzer.py` | Análise comparativa, outliers, insights; Results Store persistente e imutável |


Componentes: Tool Registry, Automation Engine, Execution Engine, Sandbox Runtime, adaptadores (Filesystem, Process, Network, Browser, Application, API, Developer Tools).
* Todas as operações do mundo real passam pelo Control Plane.

**Componentes de Secure Agency da Fase 3 (implementados e testados):**

| Módulo | Arquivo | Responsabilidade |
|--------|---------|------------------|
| Tool Registry | `nexus_core/tool_registry.py` | Catálogo padronizado de ferramentas com metadados (risco, acesso, idempotência, timeout, schemas, limites de recurso) |
| Execution Gate | `nexus_core/execution_gate.py` | Ciclo completo de execução governada: identidade → capacidade → política → risco → aprovação → recurso → execução isolada → verificação → commit → auditoria |
| Rollback Manager | `nexus_core/rollback_manager.py` | Registro de operações e reversão via handlers (não assume reversão para ações externas irreversíveis) |
| SudoBroker | `nexus_core/sudo_broker.py` | Mediação de execuções com privilégios elevados; aprovação vinculada à operação exata; proteção contra replay e parameter swapping |
| Execution Journal | `nexus_core/execution_journal.py` | Journal imutável e auditável de execuções (SHA-256 integrity_hash por entrada) |

O `Automation Engine` existente (`automation.py`) continua responsável pela execução de ferramentas com HITL, policy gating e sandbox bwrap; o Execution Gate pode ser consumido por ele para autorização centralizada.

### Distributed & Adaptive Plane
Componentes: Node Manager, Distributed Resource Monitor, Distributed Task Scheduler, Experience Consolidation Engine, Self-Improvement Engine, **Integration Layer**.
* Múltiplos nós isolados com heartbeats e detecção de falha.
* Agendamento de tarefas baseado em recursos (CPU, RAM, GPU, disco) com failover.
* Consolidação de experiências validadas entre nós.
* Autoaperfeiçoamento controlado com aprovação humana para políticas de segurança.
* Camada de integração que conecta todos os componentes em um fluxo cognitivo unificado.

**Componentes de Distributed & Adaptive da Fase 7 (implementados e testados):**

| Módulo | Arquivo | Responsabilidade |
|--------|---------|------------------|
| Distributed Node | `nexus_core/distributed_node.py` | Identidade, estado, recursos e capacidades de um nó no cluster |
| Node Manager | `nexus_core/distributed_node_manager.py` | Registro, heartbeats, detecção de nós inalcançáveis, inventário centralizado |
| Distributed Resource Monitor | `nexus_core/distributed_resources.py` | Agregação de recursos locais e remotos; seleção de nó para tarefas |
| Distributed Task Scheduler | `nexus_core/distributed_scheduler.py` | Agendamento baseado em recursos, failover, retry com limite de tentativas |
| Experience Consolidation | `nexus_core/experience_consolidation.py` | Consolidação de experiências validadas, detecção de padrões, reuso |
| Self-Improvement | `nexus_core/self_improvement.py` | Propostas de melhoria governadas; aprovação humana para políticas de segurança |
| Integration Layer | `nexus_core/integration.py` | Conecta todos os componentes em fluxo cognitivo unificado (Intent → Plan → Decide → Execute → Experience → Improvement) |
| Event Fabric (Outbox/Dead-Letter) | `nexus_core/event_fabric.py` | Transactional Outbox para persistência de eventos críticos; Dead-Letter Queue para eventos que falham repetidamente; retry com backoff exponencial |
| Distributed Network Server | `nexus_core/distributed_network.py` | Servidor WebSocket que aceita conexões de outros nós; propaga eventos entre nós |
| Distributed Network Client | `nexus_core/distributed_network.py` | Cliente WebSocket que se conecta a nós remotos; broadcast de eventos; heartbeat remoto |

---

## 4. Pilha de Tecnologia

| Domínio | Tecnologia |
|-----------|------------|
| Autoridade do sistema | Rust |
| Async runtime | Tokio |
| Backend cognitivo | Python 3.12 + FastAPI |
| Validação | Pydantic |
| Banco de dados | SQLite (WAL) |
| Armazenamento vetorial | LanceDB |
| Grafo de conhecimento | Modelo relacional (inicial) |
| LLM local | Ollama / endpoints compatíveis |
| Comunicação | UDS / WebSocket |
| Sandbox | Bubblewrap / systemd isolation |
| Testes | pytest / Rust (cargo test) |
| Empacotamento | Pacotes nativos Linux |

---

## 5. Perfis de Recursos

| Perfil | Componentes |
|--------|-------------|
| **MINIMAL** | Daemon, memória básica, API, um modelo leve, monitoramento essencial |
| **STANDARD** | Cognitive Core, Planner, recuperação de memória, concorrência limitada de agentes |
| **ADVANCED** | Colaboração multi-agente, múltiplos modelos, simulações paralelas, GPU |
| **DISTRIBUTED** | Vários nós, provedores remotos, agentes distribuídos, agendamento de carga |

Componentes pesados são opcionais; máquina de baixo recurso retém o núcleo funcional.

---

## 6. Interface com o Usuário

* CLI (operacional), TUI (curses, somente-leitura), Web HUD (dashboard single-page), Desktop GUI (GTK4 — planejado, não implementado).
* Identidade visual: fundo grafite escuro, acentos esmeralda/teal, superfícies vidro, alta densidade de informação.

---

## 7. Consistência com a Especificação Mestre

A arquitetura atual segue os princípios: local-first, model-agnostic, modular, persistente, event-driven, secure-by-design, observable, reversible, resource-aware, extensible, reproducible, human-governed.

Gaps principais em relação ao alvo: GTK4 GUI, perfis de recurso implementados como código ativo.
