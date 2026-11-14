# ADR 0020 — Paridade de Policy Engine e Approval Manager no Rust (T5)

- **Status:** aceito
- **Data:** 2026-11-14
- **Escopo:** T5 do roadmap — paridade de decisão do Policy Engine, ciclo de
  vida completo do Approval Manager, revogação, TTL por solicitação, uso único
  e testes de paridade Python ↔ Rust
- **Depende de:** ADR 0015 (Execution Gate), ADR 0016 (contratos v1.0.0),
  ADR 0019 (IPC de controle T4)
- **Não substitui:** a autoridade final do runtime Python, que permanece
  obrigatória até o T6

---

## Contexto

O T4 entregou a superfície de controle local e um gate Rust deliberadamente
mínimo. O `PolicyEngine` Rust só avaliava `CRITICAL/HIGH/MEDIUM/LOW` por uma
única função, enquanto o Python tem duas regras distintas:

- `PolicyEngine.evaluate(tool, context)` — usada pelo Automation Engine;
- `ExecutionGate._classify_risk(...)` — usada pelo gate antes da execução.

O `ApprovalManager` Rust tinha `request`, `decide`, `status`, `claim` e
`expire_stale`, mas não tinha revogação, consumo `CLAIMED → CONSUMED`,
`verify_action`, TTL por solicitação nem os estados `REVOKED`/`CONSUMED`. O
Python já tinha todos esses elementos em SQLite. Sem paridade, o Rust não pode
ser promovido a autoridade no T6.

Restrições:

- decisões determinísticas, independentes de LLM e fail-closed;
- nenhuma mudança destrutiva no backend Python nem nos contratos v1.0.0;
- os mesmos fixtures devem ser lidos pelos testes Python e Rust;
- a autoridade final continua no Python até o T6;
- sem exposição remota, TLS ou autenticação por token nesta etapa.

---

## Decisão

1. **Duas funções normativas, espelhando as duas regras Python.**
   `PolicyEngine::evaluate_tool` reproduz exatamente
   `PolicyEngine.evaluate`: `CRITICAL` nega; `HIGH`/`MEDIUM` exigem aprovação;
   `LOW` de leitura exige autonomia suficiente; `LOW` de escrita em
   `SAFE_ZONE` é permitido, e fora dela exige aprovação; risco ou acesso
   desconhecido nega. `PolicyEngine::evaluate_authorization` continua
   reproduzindo `ExecutionGate._classify_risk`, inclusive a exigência de
   autonomia para escrita em `SAFE_ZONE`.

2. **Divergência normativa documentada e testada.**
   A escrita `LOW` em `SAFE_ZONE` sem autonomia suficiente é `ALLOW` no Policy
   Engine puro e `REQUIRES_APPROVAL` no Execution Gate. Não é um bug de
   paridade: são regras distintas no Python, preservadas no Rust e cobertas por
   um teste dedicado.

3. **Fixture compartilhado de policy.**
   `contracts/fixtures/parity/policy_cases.json` contém 10 casos com
   `policy_decision` e `gate_decision`. `tests/test_policy_parity.py` (20
   testes parametrizados) e `policy/mod.rs` leem o mesmo arquivo; qualquer
   divergência falha nos dois lados.

4. **Máquina de estados completa do Approval Manager.**
   Transições aceitas: `PENDING → APPROVED|DENIED|EXPIRED|REVOKED`,
   `APPROVED → CLAIMED|REVOKED|EXPIRED`, `CLAIMED → CONSUMED`. Estados
   terminais (`CONSUMED`, `DENIED`, `EXPIRED`, `REVOKED`) não são revogáveis
   nem decidíveis. Toda transição inválida retorna erro sem alterar o registro.

5. **TTL por solicitação.**
   `request_with_ttl(request, risk_level, ttl_s)` espelha
   `request_approval(req, ttl_s=...)`. TTL ausente usa o padrão de 3600 s;
   TTL negativo, infinito ou NaN é rejeitado. `expire_stale` só toca
   `PENDING`/`APPROVED`.

6. **Revogação, verificação e consumo.**
   `revoke` aceita `PENDING`/`APPROVED` e grava `REVOKED`, sem expirar antes
   (mesma semântica do Python). `verify_action` compara o hash canônico sem
   consumir nem alterar estado e devolve `false` para aprovação inexistente ou
   params divergentes. `mark_consumed` só aceita `CLAIMED` e grava
   `CONSUMED` + `consumed_at`. O `claim` continua atômico sob o mutex e faz
   hash + transição em uma única seção crítica; no Python o gate chama
   `verify_action` e `claim_approval` separadamente, e o fixture de paridade
   modela essa composição.

7. **Fixture compartilhado de ciclo de vida.**
   `contracts/fixtures/parity/approval_lifecycle.json` define 6 cenários
   (`approved_claimed_consumed`, `denied_is_terminal`, `revoked_pending`,
   `revoked_approved`, `ttl_expires_pending`, `hash_binding_blocks_claim`).
   Cada passo declara `expect_ok`/`expect_status`; `verify_action` declara
   `expect`; `expire_stale` declara `expect_expired`. Python e Rust executam a
   mesma sequência.

8. **Revogação pelo IPC e pela bridge.**
   O comando `ApprovalRevoke {approval_id}` retorna `ApprovalResponse` com o
   registro `REVOKED`; o bridge Python expõe `approval_revoke`. Consumo não é
   exposto como comando: `mark_consumed` é chamado pelo ciclo de execução
   (T6), não por um cliente arbitrário.

9. **Convivência com o Python.**
   O Python continua sendo a autoridade final. O Approval Manager Rust é um
   shadow em memória: não persiste em disco, não tem histórico e reiniciar o
   daemon invalida aprovações (fail-closed). O T6 precisa decidir e implementar
   persistência antes de mover a autoridade; até lá, o gate Python é o único
   que pode autorizar execução real.

---

## Consequências

- O Rust decide policy e ciclo de aprovação com as mesmas entradas e saídas do
  Python, verificadas por fixtures compartilhados. A paridade é observável no
  IPC (`Authorize`, `ApprovalStatus`, `ApprovalRevoke`).
- A revogação passa a ser uma operação de primeira classe no Control Plane,
  sem depender de reinício do daemon.
- O estado `CONSUMED` existe no Rust, mas ainda não é produzido pelo IPC;
  será produzido pelo ciclo de execução do T6.
- Persistência, histórico, listagem e recuperação de órfãos continuam só no
  Python. Isso é a maior lacuna de convivência e está registrada como TD-022.
- A API continua local e não deve ser exposta remotamente: o Rust ainda não é
  autoridade final e o isolamento entre processos do mesmo UID continua fora
  de escopo.

---

## Implicações de segurança

- **Fail-closed:** risco desconhecido, acesso desconhecido, TTL inválido,
  hash divergente, estado terminal, aprovação inexistente e consumo fora de
  `CLAIMED` resultam em negação/erro, nunca em permissão.
- **Sem replay:** `claim` é atômico e único; `mark_consumed` só avança de
  `CLAIMED`; revogação remove `PENDING`/`APPROVED` do caminho de autorização.
- **Vínculo forte:** o hash JCS/SHA-256 cobre tool, params, tarefa, plano,
  modo, acesso, escopos, limites e policy version; `verify_action` não consome,
  então uma tentativa divergente não queima a aprovação.
- **Sem LLM no caminho:** todas as decisões são determinísticas e locais.
- **Sem confiança no cliente:** `ApprovalRevoke` só alcança aprovações
  existentes; a identidade continua sendo o UID do peer.

---

## Migração

- Nenhuma migração de banco no T5: o daemon continua em memória e o SQLite
  Python não muda.
- Clientes do IPC podem enviar `ApprovalRevoke`; clientes antigos continuam
  funcionando para os comandos do T4.
- O bridge Python mantém a API existente e adiciona `approval_revoke`; não há
  quebra de contrato.
- O runtime Python não deve usar o gate Rust como autoridade até o T6 fechar
  persistência, consumo e o ciclo `authorize → executar → commit` no Control
  Plane.

---

## Validação

- `tests/test_policy_parity.py`: 20 testes parametrizados sobre 10 casos
  compartilhados de `PolicyEngine` e `ExecutionGate`.
- `tests/test_approval_parity.py`: 6 cenários compartilhados de ciclo de vida,
  cobrindo todos os estados e transições inválidas.
- `tests/test_event_bridge.py`: 16 testes, incluindo `ApprovalRevoke` no fake
  daemon e fluxo real com o binário Rust (revogação bloqueia autorização e
  segunda revogação falha).
- `core/nexus-daemon/src/approval.rs`: 12 testes, incluindo consumo único,
  revogação, verificação sem mutação, TTL por solicitação e o fixture
  compartilhado.
- `core/nexus-daemon/src/policy/mod.rs`: 5 testes, incluindo os dois testes de
  fixture e a divergência normativa de `SAFE_ZONE`.
- Suíte completa: **494 testes + 12 subtests** Python; **77 testes** Rust.
  `cargo test`, `compileall`, `git diff --check` e `ruff` nos arquivos do T5
  limpos.

---

## Próximo passo

T6 — Execution Gate autoritativo no Rust. Antes de mover a autoridade, o T6
precisa fechar: persistência/histórico de aprovações, produção de `CONSUMED`
pelo ciclo de execução, fail-closed quando o daemon estiver indisponível e o
teste com daemon morto. O Python continua sendo a autoridade efetiva até lá.
