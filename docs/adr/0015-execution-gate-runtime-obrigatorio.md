# ADR 0015 — Execution Gate obrigatório no runtime e ciclo authorize → executar → commit

- **Status:** aceito
- **Data:** 2026-11-13
- **Escopo:** T0 do roadmap — ligar o `ExecutionGate` ao runtime real e eliminar a execução fantasma
- **Depende de:** ADR 0006 (Secure Agency), ADR 0014 (endurecimento pós-auditoria)

---

## Contexto

O `ExecutionGate` existia desde a Fase 3, com testes isolados, mas o `bootstrap()` não o injetava no `AutomationEngine`. Na prática, o runtime executava ferramentas aplicando `PolicyEngine` e `ApprovalManager` diretamente, sem a autorização centralizada que a arquitetura anunciava.

Além do bypass, o `AutomationEngine` chamava `gate.execute(execution_id, runner=None)` depois de já ter executado a ferramenta. Esse caminho não executava nada, mas criava uma segunda execução no gate e podia registrar `automation.gate.commit_failed`. O resultado era uma "execução fantasma": o histórico do gate não correspondia à operação real.

Restrições:

- preservar a interface pública do `ApprovalManager` e do `AutomationEngine`;
- não alterar o fluxo HITL existente (suspensão, decisão humana, retomada);
- manter a decisão de segurança determinística e independente de LLM;
- falhar fechado quando a autorização não puder ser comprovada.

---

## Decisão

1. **Injeção no bootstrap.** `bootstrap()` cria um único `PolicyEngine`, um único `ApprovalManager` e um único `ExecutionGate`, injetando o gate no `AutomationEngine` e expondo-o em `NexusRuntime.execution_gate`. O gate e o engine compartilham a mesma autoridade de aprovação.
2. **Ciclo explícito.** O `AutomationEngine` chama `authorize()` antes de reivindicar a aprovação e executar a ferramenta. Ao final, chama `commit()` com o resultado real. `commit()` não dispara a operação; apenas fecha o registro.
3. **Aprovação existente é validada, não recriada.** Quando o chamador retoma uma execução com `approval_id`, o gate verifica o hash canônico, o status `APPROVED` e a expiração. O `AutomationEngine` continua dono do ciclo `claim → mark_starting → CONSUMED`; o `commit()` do gate não sobrescreve aprovações gerenciadas externamente.
4. **Autonomia real.** `authorize()` recebe `autonomy_level` e `required_autonomy`; o classificador do gate usa a autonomia e o escopo de escrita com a mesma semântica do `PolicyEngine`.
5. **Sem execução fantasma.** Nenhum caminho do runtime chama `gate.execute(runner=None)` após a execução. `commit()` recusa execuções ainda em `pending_approval`.

---

## Consequências

- O runtime Python passa a ter um único caminho de execução governada: `authorize → executar → commit`.
- Eventos `security.execution.authorized` e `security.execution.completed` passam a existir no fluxo real, não só em testes de módulo.
- A autoridade ainda é Python. O daemon Rust continua com gate/política mínimos; a migração de autoridade é T4–T6 e deve preservar este contrato.
- O gate não substitui o `PolicyEngine` do engine; ele aplica uma segunda classificação determinística. A paridade entre os dois é testada e deve ser mantida até a unificação no Rust.
- O `commit()` é fail-closed para estados inválidos, mas uma falha de commit não desfaz uma ação já executada; ela é registrada como `automation.gate.commit_failed` para auditoria.

---

## Validação

- `tests/test_e2e_execution_gate.py`: bootstrap real, gate injetado, risco baixo autorizado/commitado, risco médio exigindo aprovação humana e consumo, risco crítico negado antes do runner.
- `tests/test_execution_gate.py`: aprovação válida, hash adulterado, expiração, commit com aprovação pendente, autonomia real e consumo de aprovação pelo engine.
- Suíte completa: `381 passed, 12 subtests passed`.

---

## Próximo passo

T1 — congelar os contratos Python ↔ Rust (evento, autorização, aprovação, resultado e tool spec) antes de mover a autoridade para o daemon. O contrato `authorize → executar → commit` é a base que o IPC do Rust deve implementar em T4–T6.
