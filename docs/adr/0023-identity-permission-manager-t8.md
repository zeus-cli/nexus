# ADR 0023 — Identity & Permission Manager: catálogo versionado e autoridade no Rust (T8)

- **Status:** aceito
- **Data:** 2026-11-17
- **Escopo:** T8 do roadmap — principals, dispositivos, agentes e serviços;
  catálogo de escopos; papéis built-in; pairing; revogação; integração com o
  Execution Gate e aprovações; auditoria
- **Depende de:** ADR 0016 (contratos v1.0.0), ADR 0019 (IPC de controle T4),
  ADR 0020 (paridade T5), ADR 0021 (Execution Gate autoritativo T6)
- **Design completo:** `docs/NEXUS_T8_IDENTITY_DESIGN.md`
- **Implementação:** T8.1–T8.4 concluídos (catálogo, validador, migração
  versionada, `IdentityStore`, IPC de identidade e bootstrap/CLI do owner);
  T8.5–T8.9 permanecem planejados. O ADR 0022 registra decisões baseline do
  T7; a validação operacional no Acer Nitro V15 ainda está pendente.

---

## Contexto

Até o T6, a autoridade de execução já está no daemon Rust, mas a identidade
continua fragmentada:

- o IPC autentica o processo por `SO_PEERCRED`/UID, não um usuário ou
  dispositivo;
- a API usa um token local único, que equivale à máquina inteira;
- `_verify_identity` no Execution Gate usa um conjunto fixo de componentes e o
  prefixo `agent-*`;
- `_validate_capability` sempre retorna `True`;
- `decided_by` de uma aprovação é texto livre;
- os três dispositivos do diagrama (POCO, Xiaomi e Moto G34) não têm
  enrollment, papel nem revogação.

Sem identidade, não é possível cumprir os critérios de T9 (gateway), T10
(Event Fabric multi-device) e T11/T12 (namespaces de memória e dados). O T8
fecha esse gap introduzindo principals, credenciais, dispositivos e escopos
determinísticos, sem depender de LLM.

---

## Decisão

1. **Autoridade de identidade no daemon Rust.**
   O Control Plane é a fonte da verdade para principals, papéis, credenciais,
   dispositivos e pairing. O Python deixa de inventar identidade; ele consulta
   e obedece ao daemon. Isso evita dois cérebros de autorização e mantém a
   mesma autoridade já usada para aprovações e execução.

2. **Catálogo versionado como fonte única.**
   `contracts/identity/scopes.json` define os escopos atômicos e
   `contracts/identity/roles.json` os papéis built-in. Ambos têm
   `catalog_version` e `contract_version` iguais e são validados antes do uso.
   Python (`nexus_core/identity_contracts.py`) e Rust (T8.2) consomem os mesmos
   arquivos; nenhum dos dois mantém uma lista divergente.

3. **Papéis built-in com listas explícitas.**
   `owner`, `family`, `guest` e `service` são obrigatórios e `builtin=true`.
   Não existe curinga: o `owner` lista os 28 escopos da versão 1.0.0 e um
   escopo novo não é concedido automaticamente. `family`, `guest` e `service`
   têm escopos proibidos verificados na carga do catálogo.

4. **Escopo efetivo é interseção, nunca união.**
   ```text
   role      = scopes(role(principal))
   granted   = principal_scopes(principal)
   device    = constraints(device)
   agent     = manifest ∩ parent_effective
   effective = role ∩ granted ∩ device ∩ agent
   autonomy  = min(machine, principal, device, task)
   ```
   `principal_scopes` só reduz. Pedir um escopo fora do papel é
   `PrivilegeEscalationError`; uma coleção vazia reduz o escopo a nada e é
   válida.

5. **Credenciais opacas com prova de posse.**
   Access token opaco de 15 min, refresh de 30 dias com rotação e assinatura
   P-256, chave de dispositivo não exportável e token de agente sem refresh.
   Nenhum segredo em texto plano é persistido; apenas hash. Prefixos `nxo_`,
   `nxr_` e `nxa_` facilitam detecção em logs e scanners.

6. **Persistência no control-plane DB.**
   As tabelas de identidade vivem no mesmo SQLite do T6
   (`data/databases/nexus_approvals.db`), com versionamento próprio
   (`schema_migrations`: versão 1 = tabelas do T6, versão 2 = identidade; não
   confundir com a migração 5 do event store Python). A migração é idempotente
   e as invariantes I1–I7 do design são aplicadas no store.

7. **Pairing single-use.**
   QR com segredo de 256 bits e código de 8 dígitos como fallback, TTL de
   5 min, 5 tentativas e consumo atômico `pending → consumed`. O dispositivo
   gera um par P-256 e assina `session_id|nonce|timestamp`; o servidor valida
   segredo, TTL, tentativas, assinatura e constraints antes de criar principal,
   device e credencial na mesma transação.

8. **Revogação em cascata.**
   Revogar um principal revoga credenciais, dispositivos e agentes filhos na
   mesma transação e emite evento. Reuse de refresh rotacionado revoga a
   família inteira e o dispositivo (`identity.credential.reuse_detected`).

9. **Agentes são subordinados, nunca aprovadores.**
   `agents.spawn` cria um principal `agent` filho com TTL de tarefa e escopos
   `manifest ∩ parent_effective`. Agente não recebe `identity.*.manage` nem
   `security.approvals.decide.*`; ao terminar, token e principal são revogados.

10. **Gate e aprovações fail-closed.**
    O fluxo autoritativo é credencial → principal → status → escopos →
    autonomia → política/risco → aprovação → execução → commit → auditoria.
    Principal/device inexistente, revogado ou suspenso, escopo ausente,
    aprovador sem `decide.*`, agente tentando aprovar ou daemon indisponível
    resultam em `DENY`. A aprovação passa a registrar `principal_id`,
    `device_id`, `decided_by_principal_id` e `required_scope`; `decided_by`
    deixa de ser texto livre.

11. **Superfície local até o T9.**
    As rotas de identidade ficam em loopback nesta fase. TLS, rate limit de
    rede, autenticação de dispositivo no gateway e exposição pública pertencem
    ao T9. Nenhuma rota de identidade é pública no T8.

12. **Rollout opt-in.**
    `[identity] enabled = false` mantém o caminho do T6. O `nexus identity init`
    cria o owner e o token local vira credencial `local_owner`; clientes
    legados sem `principal_id` são mapeados para `local-owner`. Desligar a
    flag restaura o comportamento anterior sem apagar os dados de identidade.

---

## Consequências

- O catálogo de escopos passa a ser um contrato entre Python, Rust e clientes.
  Adicionar um escopo exige atualizar catálogo, owner, papéis afetados, testes
  e a versão do catálogo.
- O Execution Gate deixa de aceitar identidade hardcoded e capacidade sempre
  verdadeira no modo autoritativo; o modo local continua funcional.
- Aprovações ficam vinculadas a principal, dispositivo e hash de parâmetros,
  permitindo auditoria e revogação coerentes.
- O banco de controle acumula responsabilidade de identidade; backup/restore
  do T7/T19 passa a cobrir principals, devices e credenciais.
- O Python mantém um validador do catálogo para falhar cedo, mas não é
  autoridade de identidade.
- Um dispositivo compartilhado (por exemplo, o Xiaomi) não herda o papel de
  outro usuário: cada par (usuário, hardware) é um principal próprio.

---

## Implicações de segurança

- Escopo desconhecido é erro, não é ignorado; não há fallback permissivo.
- `owner` não usa curinga, então um escopo novo não vira privilégio silencioso.
- Agentes não aprovam e não gerenciam identidade, mesmo que o manifesto peça.
- Tokens são opacos, com hash em repouso e TTL curto para access.
- Reuse de refresh é tratado como comprometimento e revoga a família.
- Aprovações de legado ficam com `principal_id NULL`, somente leitura, sem
  fingir uma identidade verificada.
- O trust boundary de mesmo UID continua existindo; a mitigação completa é
  usuário/serviço separado (T7) e gateway/TLS (T9).

---

## Validação do T8.1

- `contracts/identity/scopes.json`: 28 escopos em 9 categorias.
- `contracts/identity/roles.json`: `owner` (28), `family` (14), `guest` (6) e
  `service` (6), todos com lista explícita.
- `nexus_core/identity_contracts.py`: loader/validador com `build_catalog`,
  `load_catalog`, `scopes_for_role` e `narrow_scopes`; erros
  `IdentityContractError`, `UnknownScopeError`, `UnknownRoleError` e
  `PrivilegeEscalationError`.
- `tests/test_identity_contracts.py`: 26 testes cobrindo catálogo, cobertura do
  owner, escopos proibidos, narrowing, escopo desconhecido, escalada, duplicata,
  versão divergente e mutações de papel.
- `git diff --check` limpo.

## Validação do T8.2

- `core/nexus-daemon/src/control_schema.rs`: migração 1 (T6) e 2 (T8)
  idempotentes, transacionais e com adoção de banco legado; versão futura é
  rejeitada em vez de aberta.
- `core/nexus-daemon/src/identity/catalog.rs`: validação do mesmo JSON do
  Python, incluindo owner, papéis proibidos e narrowing.
- `core/nexus-daemon/src/identity/store.rs`: principals, grants, autonomia
  efetiva, credenciais, devices, pairing single-use, revogação em cascata,
  rotação de refresh e reuse detection.
- Invariantes cobertas por teste negativo: I2 (grant fora do papel), I3 (filho
  acima do pai), I4 (agente não recebe `security.approvals.decide.*` nem gestão
  de identidade), I5 (cascata), I6 (pairing single-use) e I7 (reuse de refresh
  revoga a família e o device).
- `cargo test` no daemon: 128 testes, incluindo 13 de catálogo e 29 de store,
  migração idempotente/legado/futuro, pairing e cascata.
- Migração validada numa cópia do banco real: schema v2, 198 aprovações
  preservadas, 4 papéis, 54 `role_scopes`, 0 principals e índice único parcial
  em `credentials(secret_hash)`. O banco real permaneceu intocado.
- `git diff --check` limpo.

## Validação do T8.3

- `core/nexus-daemon/src/ipc/protocol.rs`: frames `IdentityList`,
  `IdentityGet`, `IdentityCreatePrincipal`, `IdentityRevokePrincipal`,
  `IdentityEffectiveScopes`, `IdentityPermissionCheck`, `IdentityDeviceList`,
  `IdentityDeviceRevoke` e `IdentityPairStart`, com respostas tipadas. O
  protocolo continua major 1 e não há campo desconhecido aceito no wire.
- `core/nexus-daemon/src/ipc/server.rs`: comandos de leitura exigem
  `identity.principals.read`/`identity.devices.read`; criação e revogação
  exigem `manage`; pairing exige `identity.pairing.create`. Ator sem escopo
  recebe `Error`, nunca resposta permissiva.
- `identity_permission_check` é fail-closed: principal inexistente, suspenso,
  revogado, escopo desconhecido ou ausente retornam `allowed=false` com motivo;
  apenas falha de storage vira erro do comando.
- `nexus_core/event_bridge.py` e `nexus_core/control_plane.py`: cliente dos
  frames acima. `identity_pair_start` gera segredo e código no Python e envia
  somente SHA-256; o segredo bruto nunca cruza o IPC nem é persistido.
- `cargo test` no daemon: 130 testes. `pytest` em
  `tests/test_event_bridge.py` + `tests/test_control_plane.py`: 41 testes,
  incluindo daemon Rust real para listar, criar guest, verificar permissão,
  iniciar pairing e revogar com cascata.
- Teste negativo: `family` sem `identity.principals.read` recebe erro ao listar
  principals; guest nega `tools.write` e permite `tools.read`.
- Frames de conclusão de pairing e refresh/verify token pertencem ao T8.5; a
  rotação do token local foi entregue no T8.4.

## Validação do T8.4

- `core/nexus-daemon/src/identity/store.rs`: `init_owner` cria no máximo um
  principal `user`/`owner` e uma credencial `local_owner` ativa; repetições com
  o mesmo token não duplicam registros. Se o token local mudou, a credencial
  antiga é revogada e a nova é emitida na mesma transação.
- `local_owner_status` resolve o owner apenas por credencial `local_owner`
  ativa de principal ativo; token desconhecido, revogado ou de outro tipo
  retorna `None`.
- Owner revogado não é recriado silenciosamente: `init` falha com
  `PrincipalNotActive`, reservando a recuperação para o fluxo físico explícito.
- `core/nexus-daemon/src/ipc/protocol.rs`: frames `IdentityInitOwner` e
  `IdentityOwnerStatus`, com `IdentityOwnerResponse`. O wire carrega somente o
  SHA-256 do token local, nunca o token bruto.
- `core/nexus-daemon/src/ipc/server.rs`: bootstrap exige o mesmo UID do
  handshake; emite `identity.principal.created` e `identity.credential.issued`
  apenas quando há criação real, sempre com `dedup=false`.
- `EventStore::query_by_kind` corrigido: payload e severidade eram lidos de
  colunas trocadas, o que mascarava a auditoria.
- `nexus_core/event_bridge.py` e `nexus_core/control_plane.py`: métodos
  `identity_init_owner` e `identity_owner_status`; o cliente Python calcula o
  SHA-256 do token e nunca o envia.
- `nexus_core/cli.py`: `nexus identity init|status|list|revoke`. `init` é
  idempotente e mostra a origem do token; `status`, `list` e `revoke` resolvem
  o ator local pelo token e recusam revogar o próprio owner pela CLI.
- `cargo test` no daemon: 134 testes. `pytest` em
  `tests/test_identity_contracts.py`, `tests/test_contracts.py`,
  `tests/test_event_bridge.py`, `tests/test_control_plane.py` e
  `tests/test_cli.py`: 109 testes.
- Testes de integração com o daemon Rust real: bootstrap idempotente, auditoria
  sem segredo/hash no payload, status/list pela CLI e revogação de guest com
  cascata. Teste de wire garante que o token bruto não aparece em nenhum frame.
- Sem mudança de schema: migração continua na versão 2.

---

## Fora do escopo

- TLS, rate limit de rede, exposição pública e autenticação no gateway (T9).
- Namespaces de memória, retenção final e quotas (T11/T12).
- WebAuthn/biometria como fator obrigatório (T18, opcional).
- Substituir o token local de recuperação do host.
- Dispatch remoto e quotas de agentes (T17).
