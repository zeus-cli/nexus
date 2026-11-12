# ADR 0008 — Universal Simulation: Simulation Subsystem (Fase 5)

> **Nota de atualização (2026-11-12):** decisão histórica preservada; as contagens de testes referem-se à data original. Estado atual e validação consolidados em `docs/adr/0014-endurecimento-auditoria-2026-11-12.md`.

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/simulation_types.py`, `simulation_clock.py`, `state_manager.py`, `rules_engine.py`, `event_scheduler.py`, `world_builder.py`, `scenario_manager.py`, `metrics_engine.py`, `simulation_analyzer.py`
- **Depende de:** ADR 0001–0007

---

## Contexto

A especificação mestre (seção 11) exige o NEXUS SIMULATION como subsistema independente (não meramente um workflow conversacional de multi-agente), com:

* Componentes: World Builder, World Registry, Entity Manager, State Manager, Rules Engine, Event Scheduler, Simulation Clock, Scenario Manager, Experiment Runner, Metrics Engine, Simulation Analyzer, Results Store.
* Paradigmas: Agent-Based Modeling, Discrete-Event, Rule-Based, Mathematical, Probabilistic, Social, Economic, Network, Technical, Digital Twin.
* Parallel Worlds: múltiplos ambientes isolados com World ID, estado inicial, entidades, regras, parâmetros, histórico de eventos, clock, random seed, versões de modelo, limites de recurso, métricas, estado final.
* Experiment Management: baseline, alternativas, variação de parâmetros, Monte Carlo, sensibilidade, múltiplas execuções, análise comparativa, reprodutibilidade.
* Simulation Safety: isolamento do ambiente operacional real; agentes simulados não recebem privilégios de execução real; resultados explicitamente labelados como simulados/estimados/inferidos.
* MiroFish-inspired: avaliado como referência, não dependência obrigatória.

---

## Decisão 1 — Subsistema de simulação independente (`nexus_core/simulation_*.py`)

Criado um conjunto de módulos sob `nexus_core/` (não um pacote separado, para evitar diretórios vazios), cada um com responsabilidade clara:

| Módulo | Responsabilidade |
|--------|------------------|
| `simulation_types.py` | Contratos base: paradigmas, estados, entidades, eventos, definição de mundo, resultado |
| `simulation_clock.py` | Relógio de simulação por mundo (step-based ou contínuo, pausa/retomada) |
| `state_manager.py` | Estado do mundo, snapshots, restauração, reprodutibilidade |
| `rules_engine.py` | Regras declarativas determinísticas (condição → ação) |
| `event_scheduler.py` | Fila de eventos discretos ordenada por timestamp |
| `world_builder.py` | Construção fluente de WorldDefinition |
| `scenario_manager.py` | Cenários baseline/alternativas, experimentos (Monte Carlo, sensibilidade, repetições) |
| `metrics_engine.py` | Coleta e agregação de métricas por mundo |
| `simulation_analyzer.py` | Análise comparativa, outliers, insights |
| `simulation_analyzer.py` (Results Store) | Armazenamento persistente imutável de resultados (SQLite, SHA-256 integrity_hash) |

---

## Decisão 2 — Mundos paralelos isolados

Cada mundo tem seu próprio estado, clock, random seed e regras. Não há comunicação automática entre mundos paralelos. Isso garante isolamento de execuções de cenários alternativos.

---

## Decisão 3 — Resultados explicitamente labelados

Todo `SimulationResult` tem um campo `label` (`simulated`, `estimated`, `inferred`) e `is_simulation()` para confirmar. Resultados nunca são apresentados como predições garantidas. Isso respeita o princípio de segurança de simulação.

---

## Decisão 4 — Reprodutibilidade

* Random seed persistido nos parâmetros do mundo.
* Snapshots de estado permitem restauração a pontos anteriores.
* Results Store armazena parâmetros, random seed, métricas e integrity_hash (SHA-256) para verificação de integridade e auditoria.

---

## Decisão 5 — Regras determinísticas, sem LLM

O Rules Engine avalia condições de forma determinística (expressões avaliáveis) e executa ações via handlers registrados. Não depende de LLM para decisões de simulação.

---

## Decisão 6 — MiroFish avaliado como referência, não dependência

Conforme a especificação mestre, MiroFish e projetos relacionados são investigados como referência arquitetural (grafo de conhecimento, modelagem de personalidade, ambientes de interação social, memória de agente, análise de resultados), mas não são dependência obrigatória do NEXUS. Licenças, dependências, status de manutenção e compatibilidade arquitetural são verificadas antes de incorporar código externo.

---

## Consequências

* **Positivo:** subsistema de simulação universal completo, testável, com mundos paralelos isolados, experimentos reprodutíveis e resultados labelados.
* **Negativo (Fase 5):** a integração da simulação com o Agent Runtime (agentes simulados executando dentro de mundos) fica para consumo posterior. O Event Scheduler ainda não está conectado ao Simulation Clock para execução automática baseada no tempo.
* **Risco conhecido:** o `RulesEngine._eval_condition` usa `eval()` com builtins restritos; em produção, deve ser substituído por um avaliador de expressões mais seguro ou uma DSL própria.

---

## Testes

* `tests/test_simulation.py`: 21 testes cobrindo tipos, clock, state manager, rules engine, event scheduler, world builder/registry, scenario manager, experiment runner, metrics engine, analyzer e results store.
* Regressão completa: 230 testes passando.
