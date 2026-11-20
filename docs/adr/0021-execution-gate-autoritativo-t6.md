# ADR 0021 — Execution Gate autoritativo no Rust (T6)

- **Status:** aceito
- **Data:** 2026-11-16
- **Escopo:** T6 do roadmap — persistência e histórico de aprovações,
  produção de `CLAIMED → CONSUMED` pelo ciclo de execução, autoridade final no
  daemon Rust, adaptador Python via IPC e fail-closed quando o daemon está
  indisponível
- **Depende de:** ADR 0015 (Execution Gate), ADR 0016 (contratos v1.0.0),
  ADR 0019 (IPC de controle T4), ADR 0020 (paridade T5)
- **Substitui parcialmente:** a autoridade final Python descrita no ADR 0020;
  o modo local continua o padrão e a migração é opt-in

---

## Contexto

O T5 fechou a paridade de decisão entre Python e Rust, mas o `ApprovalManager`
Rust continuava em memória. Reiniciar o daemon apagava aprovações e o histórico;
uma execução interrompida não tinha estado observável; e o `CLAIMED → CONSUMED`
existia apenas como operação interna, sem produção pelo ciclo de execução.

O roadmap exige que o T6 mova a autoridade final de execução para o Rust, mas
sem quebrar o contrato `authorize → executar → commit` nem o comportamento
fail-closed. Também é obrigatório preservar o modo local dos testes existentes e
não expor o IPC remotamente antes do endurecimento de rede.

Restrições:

- decisões determinísticas e independentes de LLM;
- nenhuma escrita concorrente no mesmo SQLite por Python e Rust (sem
  dual-write);
- identidade e capacidade continuam verificadas no processo Python;
- daemon indisponível deve negar execução, nunca liberar;
- configuração existente não pode mudar de comportamento por padrão;
- o socket IPC continua local, `0600`, restrito ao mesmo UID.

---

## Decisão

1. **Store SQLite persistente no Rust.**
   `ApprovalStore` abre `data/databases/nexus_approvals.db` com o mesmo schema
   de `approvals` e `executions` já usado pelo Python, WAL, `synchronous =
   NORMAL`, `busy_timeout` e permissão `0600`. Inserção, `claim`,
   `start_execution` e `complete_execution` usam transações; o hash canônico e
   os estados do contrato v1.0.0 são preservados.

2. **Ciclo de execução produz `CLAIMED → CONSUMED`.**
   `Authorize` com `approval_id` valida o hash e faz `APPROVED → CLAIMED` na
   mesma transação. `ExecutionStart` faz `CLAIMED → CONSUMED` e grava a
   execução como `STARTING`; `ExecutionComplete` grava o estado terminal
   (`COMPLETED`, `FAILED` ou `INTERRUPTED`) e o resumo. `ExecutionStatus`
   devolve aprovação + execução; `ExecutionRecover` marca execuções órfãs
   antigas como `UNKNOWN`.

3. **Histórico e verificação pelo IPC.**
   O protocolo ganhou `ApprovalList` (filtro por status e paginação),
   `ApprovalVerify` (hash sem consumir), `ApprovalClaim` (claim explícito, com
   request opcional para validar o vínculo), `ApprovalExpire` e os frames de
   execução acima. `ApprovalRequest` aceita `risk_level`/`ttl_s` como metadados
   do frame de controle, fora do contrato `approval_request` v1.

4. **Bootstrap abre o banco e recupera órfãos.**
   O daemon abre o store de aprovações no bootstrap e executa
   `recover_orphans(DEFAULT_ORPHAN_TIMEOUT_SECONDS)`; falha ao abrir é
   `BootstrapError::Approvals`. O caminho do banco deriva do `data_dir`
   absoluto da configuração.

5. **Adaptador Python sem dual-write.**
   `nexus_core/control_plane.py` adiciona `ControlPlaneClient` (conexão
   preguiçosa, serializada, com timeout e classificação de erros) e
   `RustApprovalManager`, que expõe a interface do manager local sobre IPC e
   normaliza `approval_id` → `id` para CLI/API/HUD. Em modo autoritativo o
   Python não abre `nexus_approvals.db` para escrita.

6. **Autoridade opt-in e fail-closed.**
   `[control_plane] enabled = false` é o padrão; o modo local permanece
   inalterado. Com `enabled = true`, `ExecutionGate(... authoritative=True)`
   delega política, aprovação e consumo ao daemon. Identidade e capacidade
   continuam locais. Erro ou indisponibilidade em `authorize` retorna
   `DENIED`; falha em `begin_execution` ou `commit` levanta
   `ExecutionGateError`; métodos booleanos do adaptador retornam `False`.

7. **Automação autoritativa sem aprovação local.**
   No modo autoritativo o `AutomationEngine` não cria aprovação no manager
   Python: o `REQUIRES_APPROVAL` do gate traz o `approval_id` do daemon, e o
   `begin_execution` confirma `CLAIMED → CONSUMED` antes do runner. O commit
   delega o estado terminal ao daemon; o Python não sobrescreve o ciclo.

8. **Bootstrap degradado e health explícito.**
   Se o daemon estiver indisponível na subida, o runtime continua e registra
   warning; `health()` publica `control_plane.status = "unavailable"`, adiciona
   um check falso e marca o runtime como `degraded`. Nenhuma execução é
   autorizada até o daemon voltar.

9. **Compatibilidade e escopo de rede.**
   O schema é compatível com o banco Python existente e os fixtures de
   paridade do T5 continuam válidos. O IPC não é exposto remotamente; TLS,
   autenticação por token, outbox/offsets duráveis por assinante e identidade
   de rede pertencem ao T9/T10.

---

## Consequências

- Aprovações e execuções sobrevivem a reinícios do daemon, com histórico
  paginado e leitura read-only pelo Python para auditoria.
- O ciclo `CLAIMED → CONSUMED` passa a ser observável e produzido pelo caminho
  real de execução, não por um cliente arbitrário.
- O modo local continua sendo o padrão; a autoridade Rust exige configuração
  explícita e daemon operacional.
- A disponibilidade do daemon passa a ser requisito para executar no modo
  autoritativo. A indisponibilidade é fail-closed, mas o health e o bootstrap
  degradado tornam a condição visível.
- O Python deixa de ser autoridade efetiva no modo opt-in, mas mantém as
  verificações de identidade/capacidade e a interface de auditoria.
- Persistência compartilhada exige disciplina de schema: mudanças futuras em
  `approvals`/`executions` precisam manter Python e Rust alinhados.

---

## Fora do escopo

- TLS, autenticação por token, rate limit e exposição remota do IPC.
- Outbox/offsets duráveis por assinante no Control Plane.
- Migração automática de dados legados; o schema compatível é lido diretamente.
- Autoridade de identidade de rede multi-host (T8) e API Gateway (T9).

---

## Validação

- `cargo test` no daemon: 86 testes, incluindo store persistente, ciclo
  `CLAIMED → CONSUMED → COMPLETED`, histórico e recuperação de órfãos.
- Suíte Python completa: 516 passed, 12 subtests passed. Inclui configuração
  opt-in, adaptador, autoridade do gate, bootstrap degradado e automação sem
  aprovação local em `tests/test_control_plane.py` (20 testes).
- Integração com daemon real em `tests/test_event_bridge.py`: ciclo completo
  com aprovação, persistência read-only em `nexus_approvals.db` e daemon morto
  negando autorização.
- `compileall`, `node --check` e `git diff --check` limpos nos arquivos do T6.
