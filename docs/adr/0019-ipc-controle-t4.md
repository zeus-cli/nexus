# ADR 0019 — IPC de controle no daemon Rust: handshake, autenticação local e autorização (T4)

- **Status:** aceito
- **Data:** 2026-11-14
- **Escopo:** T4 do roadmap — comandos de controle `authorize`, aprovação,
  `health` e `event` no daemon Rust; autenticação local por `SO_PEERCRED`/UID,
  timeout, versionamento e testes de contrato
- **Depende de:** ADR 0004 (nexus-daemon), ADR 0015 (Execution Gate no
  runtime), ADR 0016 (contratos v1.0.0), ADR 0017 (Event Fabric)
- **Não substitui:** a autoridade de segurança do runtime Python, que continua
  obrigatória até T5/T6

---

## Contexto

O IPC do daemon expunha apenas `Event`, `Subscribe`, `Unsubscribe` e `Health`,
sem handshake, sem identidade de peer e sem comandos de controle. O T1 congelou
os contratos de autorização/aprovação em `contracts/`, mas o módulo Rust era
`#[cfg(test)] mod contracts;` e nenhum cliente de produção os utilizava. O T4
precisa tornar essa superfície real sem antecipar a migração de autoridade:

> Comandos `authorize`, `approval`, `health` e `event`; autenticação local por
> `SO_PEERCRED`/UID; timeout; versionamento; testes de contrato.

Restrições:

- o runtime Python continua sendo o gate obrigatório; o T4 não pode remover
  nem enfraquecer o caminho `authorize → executar → commit` do Python;
- nenhuma API remota, TLS ou exposição de rede; o soquete é local;
- decisões determinísticas, independentes de LLM e fail-closed;
- os mesmos fixtures e vetores de hash do T1 devem valer para Python e Rust;
- mudanças não destrutivas no store de eventos e no backend Python.

---

## Decisão

1. **Handshake obrigatório e versionado.**
   Todo cliente envia `Hello {protocol_version, component_id}` como primeiro
   frame; o daemon responde `Welcome {protocol_version, server_version,
   component_id, peer_uid}` ou `VersionMismatch {supported, received}`. A
   constante `IPC_PROTOCOL_VERSION` é `1.0.0` e a compatibilidade é apenas por
   major: `1.x` é aceito, `2.0.0` é recusado. O handshake tem timeout de 10 s e
   vale também para `Health` e `Event`; qualquer outro primeiro frame encerra a
   conexão com erro. Frames são limitados a 1 MiB, sem aceitar linha sem
   terminador, e a escrita tem timeout de 5 s.

2. **Autenticação local por credenciais de peer.**
   Antes do handshake, o daemon lê `SO_PEERCRED` via `getsockopt` e exige o
   mesmo UID efetivo do processo (`geteuid`). Não existe exceção para root: um
   daemon de usuário comum não aceita controle de outro UID, ainda que
   privilegiado. O soquete é criado com permissão `0600`. Falha de leitura de
   credenciais ou UID divergente encerra a conexão sem processar comandos.

3. **Comandos de controle com contratos v1.0.0.**
   O IPC aceita `Authorize {request}`, `ApprovalRequest {request}`,
   `ApprovalDecision {decision}`, `ApprovalStatus {approval_id}`, `Health` e
   `Event`. As respostas `AuthorizationResponse`, `ApprovalResponse` e
   `HealthResponse` carregam `protocol_version`. Os tipos Rust usam
   `deny_unknown_fields`; o evento é validado (`kind`, `source`, `severity`,
   `payload`, `created_at`) antes de entrar no Event Fabric. O módulo
   `contracts.rs` deixa de ser test-only e passa a ser usado em produção; os
   testes de contrato continuam lendo os mesmos fixtures do Python.

4. **Gate determinístico, mas não autoridade final.**
   `ExecutionGate::authorize_request` espelha a classificação de risco do
   Python para o T4: `CRITICAL` nega; `HIGH`/`MEDIUM` exigem aprovação;
   `LOW` com escrita fora de `SAFE_ZONE` ou autonomia insuficiente exige
   aprovação; `LOW` dentro do escopo é permitido; risco desconhecido e
   contrato inválido negam. A resposta usa `ALLOWED`, `REQUIRES_APPROVAL` ou
   `DENIED`, e `execution_id` só existe em `ALLOWED`. A paridade ampla de
   policy, revogação e consumo final pertence ao T5/T6.

5. **Aprovação vinculada ao hash canônico e de uso único.**
   O `ApprovalManager` do daemon mantém registros em memória com
   `params_hash` calculado pelo mesmo algoritmo JCS/SHA-256 do Python, TTL
   padrão de 3600 s, decisão única e estado
   `PENDING/APPROVED/DENIED/CLAIMED/EXPIRED`. Uma autorização com
   `approval_id` faz claim atômico sob mutex: somente `APPROVED` e com hash
   idêntico pode ser consumida, e o registro passa a `CLAIMED`. Replay,
   aprovação negada, expirada ou com parâmetros divergentes é negado sem
   consumir o registro. A transição `CLAIMED → CONSUMED` fica para o ciclo de
   execução (T6). Reiniciar o daemon invalida aprovações pendentes — o
   comportamento é fail-closed até a persistência do T5.

6. **Bridge Python negocia e valida os dois lados.**
   `nexus_core/event_bridge.py` envia `Hello`, exige `Welcome` compatível e
   lança `BridgeVersionMismatch` em `VersionMismatch`. Os métodos
   `authorize`, `approval_request`, `approval_decide` e `approval_status`
   validam o schema antes de enviar e validam a resposta recebida, de modo que
   divergências de contrato falham explicitamente em vez de serem
   interpretadas como dados confiáveis.

---

## Consequências

- O daemon passa a oferecer uma superfície de controle local completa para o
  ciclo de autorização, sem mover a autoridade final. O Python continua
  decidindo e executando; o T4 prepara o T5/T6.
- O handshake e o UID são pré-condições para qualquer comando, inclusive
  `Event` e `Health`. Clientes antigos que enviavam `Health` como primeiro
  frame deixam de funcionar por desenho.
- A aprovação em memória não sobrevive a reinício e não tem revogação nem
  histórico persistente. Isso é aceitável para a superfície de controle do T4,
  mas não para a autoridade final: reiniciar é fail-closed, não fail-open.
- O gate Rust é uma cópia deliberada e limitada da política Python. Qualquer
  divergência de classificação é um risco de paridade até o T5, por isso a
  autoridade permanece no Python e a API não deve ser exposta remotamente.
- `contracts.rs` agora é código de produção; mudanças nos schemas continuam
  exigindo atualização de fixtures, testes Python/Rust e versão de contrato.
- O soquete `0600` e o UID impedem controle por outros usuários locais, mas não
  isolam processos do mesmo usuário. TLS, autenticação por token e rate limit
  pertencem ao endurecimento pós-Control-Plane.
- O outbox durável e offsets/acks por assinante no Rust continuam pendentes
  (TD-021); o barramento segue em memória.

---

## Implicações de segurança

- **Fail-closed:** falha de credenciais, handshake inválido, versão
  incompatível, frame acima do limite, evento inválido, risco desconhecido ou
  aprovação inválida resultam em encerramento/negação, nunca em permissão.
- **Sem confiança no cliente:** `component_id` é identidade declarada, não
  autorização; a autorização efetiva vem do UID e, futuramente, do T5/T6.
- **Sem replay:** o claim da aprovação é atômico e único; o hash cobre tool,
  params, tarefa, plano, modo, acesso, escopos, limites e policy version.
- **Sem LLM no caminho de decisão:** a política é determinística e local.
- **Limites de recurso:** frame de 1 MiB, timeout de handshake e de escrita
  evitam conexões ociosas indefinidas e leituras sem limite.

---

## Migração

- Clientes do IPC devem enviar `Hello` antes de qualquer frame e tratar
  `Welcome`/`VersionMismatch`.
- O bridge Python mantém a API pública existente (`publish`, `subscribe`,
  `health`) e adiciona os métodos de controle; chamadas antigas passam a exigir
  o handshake automático feito por `connect()`.
- Nenhuma migração de banco é necessária no T4: aprovações do daemon são em
  memória e o EventStore não muda.
- O runtime Python não deve usar o gate Rust como autoridade até que o T5/T6
  fechem paridade, persistência e o ciclo `CLAIMED → CONSUMED`.

---

## Validação

- `tests/test_event_bridge.py`: 16 testes cobrindo handshake, `Welcome`,
  `VersionMismatch`, erro de handshake, fechamento em falha, validação de
  contrato antes do envio e na resposta, comandos de controle com fake daemon
  e fluxo real com o binário Rust: `LOW` permitido, `HIGH` exigindo aprovação,
  decisão, autorização com `CLAIMED`, replay negado e hash divergente sem
  consumo.
- `core/nexus-daemon/src/ipc/server.rs`: testes de handshake obrigatório,
  versão incompatível, timeout, UID divergente, frame acima de 1 MiB, frame
  sem terminador, health versionado, `authorize` LOW, fluxo HIGH com aprovação
  e replay, e validação dos contratos de aprovação.
- `core/nexus-daemon/src/approval.rs`: uso único, hash divergente, expiração,
  decisão inválida e entradas inválidas.
- `core/nexus-daemon/src/execution/mod.rs`: classificação de risco, escrita
  fora de `SAFE_ZONE`, autonomia insuficiente, contrato inválido e vínculo do
  hash.
- Suíte completa: **468 testes + 12 subtests** Python; `cargo test` no daemon
  com **67 testes**. `compileall` e `git diff --check` limpos nos arquivos do
  T4; `ruff` global continua com achados pré-existentes.

---

## Próximo passo

T5 — Policy Engine + Approval Manager no Rust com paridade de decisão,
revogação, TTL, uso único, persistência/migração e testes de paridade contra o
Python. O T4 permanece uma superfície de controle local; a autoridade final só
migra depois do T5/T6, e a API continua sem exposição remota até o gate Rust
estar fechado.
