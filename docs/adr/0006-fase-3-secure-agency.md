# ADR 0006 — Secure Agency: Execution Gate, Rollback, SudoBroker, Execution Journal (Fase 3)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/execution_gate.py`, `tool_registry.py`, `rollback_manager.py`, `sudo_broker.py`, `execution_journal.py`
- **Depende de:** ADR 0001–0005 (ApprovalManager, PolicyEngine, SandboxExecutor existentes)

---

## Contexto

A especificação mestre (seção 12–13) exige uma camada de execução governada onde:

* Todas as operações do mundo real passam pelo Control Plane.
* Um Execution Gate centralizado aplica o ciclo: identidade → capacidade → política → risco → aprovação → recurso → execução isolada → verificação → commit → auditoria.
* Segurança é imposta independentemente de LLMs e agentes.
* Rollback não é assumido para ações externas irreversíveis.
* SudoBroker media execuções com privilégios elevados.
* Execution Journal dedicado registra todas as execuções de forma imutável.

O backend Python já possuía `ApprovalManager` (HITL com SHA-256), `PolicyEngine` (Allow/RequireApproval/Deny) e `SandboxExecutor` (bubblewrap), mas faltavam o Execution Gate unificado, Tool Registry consolidado, Rollback Manager, SudoBroker e Execution Journal.

---

## Decisão 1 — Execution Gate centralizado (`execution_gate.py`)

Criado `ExecutionGate` que orquestra o ciclo completo de autorização:

1. **Identity Verification** — componentes confiáveis (nexus-daemon, python-runtime, cli, tui, web-hud, agent-*).
2. **Capability Validation** — valida que o componente tem capacidade para a ferramenta.
3. **Policy Evaluation + Risk Classification** — decisão determinística (LOW→ALLOW, MEDIUM/HIGH→REQUIRES_APPROVAL, CRITICAL→DENIED), independente de LLM.
4. **Approval Check** — cria solicitação HITL via `ApprovalManager` quando necessário.
5. **Resource Reservation** — validação de limites de recurso.
6. **Isolated Execution** — execução apenas via runner fornecido (nenhum shell arbitrário de modelo).
7. **Result Verification + Commit** — registra resultado e status final.
8. **Audit** — emite eventos de segurança no EventStore.

O gate retorna um `GateResult`; o chamador então chama `execute()` com o mesmo `request_id` para realizar a operação (protocolo CLAIMED → CONSUMED).

---

## Decisão 2 — Tool Registry consolidado (`tool_registry.py`)

Criado `ToolRegistry` com `ToolSpec` padronizado: tool_id, versão, descrição, nível de risco, nível de acesso, modo de execução (HOST/SANDBOX), idempotência, timeout, schemas de entrada/saída, capacidades requeridas, limites de recurso, reversibilidade. Inclui validação de parâmetros contra esquema JSON.

Nenhuma ferramenta executa comandos de shell arbitrários gerados por modelos; todas usam argumentos estruturados com esquema explícito.

---

## Decisão 3 — Rollback Manager (`rollback_manager.py`)

Criado `RollbackManager` que registra operações executadas e tenta reverter via handlers registrados. Operações não reversíveis são marcadas como `NOT_REVERSIBLE` e não tentam reversão automática. Reforça a regra de que rollback não é assumido para ações externas irreversíveis.

---

## Decisão 4 — SudoBroker (`sudo_broker.py`)

Criado `SudoBroker` que media execuções com privilégios elevados:

* Solicitação vinculada à operação exata (tool_id + parâmetros + escopo).
* Aprovação humana vinculada; expiração de aprovação (TTL).
* Proteção contra replay e parameter swapping (SHA-256 canônico dos parâmetros).
* Revogação de aprovações não consumidas.
* Protocolo PENDING → APPROVED → CONSUMED.

---

## Decisão 5 — Execution Journal dedicado (`execution_journal.py`)

Criado `ExecutionJournal` em SQLite (append-only, WAL) separado do EventStore de eventos gerais. Cada entrada tem entry_id, request_id, component_id, tool_id, params_hash, resultado, status, approval_id, task_id, timestamps, duration_ms, error e `integrity_hash` (SHA-256 do conteúdo para imutabilidade auditável). Oferece `verify_all_integrity()` para auditoria.

---

## Consequências

* **Positivo:** camada de execução governada completa, testável, integrada aos mecanismos existentes (ApprovalManager, PolicyEngine, SandboxExecutor) sem quebrá-los.
* **Negativo:** a integração profunda do Execution Gate no loop cognitivo principal e no Automation Engine fica para consumo pelos motores da Fase 2 quando o plano de execução for acionado.
* **Risco conhecido:** o Execution Gate da Fase 3 ainda usa `autonomy_level=1` fixo na avaliação de política; deve ser parametrizado com o nível real do componente quando integrado ao Identity Manager (Fase 4+).

---

## Testes

* `tests/test_secure_agency.py`: 18 testes cobrindo Tool Registry, Execution Gate, Rollback Manager, SudoBroker e Execution Journal.
* Regressão completa: 191 testes passando.
