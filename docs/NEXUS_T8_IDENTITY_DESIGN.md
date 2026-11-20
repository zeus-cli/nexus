# NEXUS T8 — Identity & Permission Manager (Design)

**Versão:** 1.1 (estado da implementação)
**Atualizado:** 2026-11-20
**Depende de:** T6 (autoridade Rust), T7 (host e rede)
**Bloqueia:** T9 (API Gateway), T10 (Event Fabric multi-device), T11/T12 (dados e memória)
**Status:** T8.1–T8.4 implementados no workspace (catálogo, validador, ADR 0023,
`IdentityStore`, IPC de identidade e bootstrap/CLI do owner). T8.5–T8.9 estão
pendentes. O ADR 0022 registra o baseline operacional do T7; a validação do
host Acer continua pendente.

---

## 1. Contexto e estado atual

O que existe hoje:

| Área | Estado real | Limitação |
|---|---|---|
| IPC local | `SO_PEERCRED` + UID, socket `0600` (T4) | Autentica o **processo**, não o usuário/dispositivo |
| API | token local único (`data/secrets/api_token`) | Um token = a máquina inteira; sem identidade HTTP por pessoa/device |
| Autonomia | `[safety] autonomy_level` global | Não varia por usuário, dispositivo ou tarefa |
| Execution Gate | `_verify_identity` usa set fixo + prefixo `agent-*` | `_validate_capability` retorna `True` sempre |
| Agentes | `AgentManifest` declara `tool_access` e `memory_scope` | Não há vínculo com um principal nem interseção aplicada |
| Aprovações | `decided_by` é texto livre | Não há identidade verificada por trás da decisão |
| Dispositivos | Store, schema e IPC de identidade existem | POCO, Xiaomi e Moto ainda não têm pairing completo, credenciais de cliente ou revogação operacional |

O T8 fecha esse gap: **quem** é o solicitante, **o que** ele pode fazer, **em qual**
dispositivo, e **como** isso é revogado.

---

## 2. Objetivos e não objetivos

**Objetivos**

1. Um registro único de principals (usuário, dispositivo, agente, serviço) com
   papéis, escopos e estado de ciclo de vida.
2. Pairing seguro dos três dispositivos do diagrama (POCO, Xiaomi, Moto G34),
   com chave por dispositivo e revogação imediata.
3. Autorização determinística no Control Plane (Rust), integrada ao Execution
   Gate e às aprovações do T6.
4. Identidade de agente derivada do principal pai, sem escalada de privilégio.
5. Base para namespaces de memória/dados (T11/T12) e para o gateway (T9).

**Não objetivos**

- OIDC/SSO, login social, identidade na nuvem ou multi-tenant SaaS.
- Biometria/WebAuthn como fator obrigatório (pode ser camada opcional no T18).
- Substituir o token local de recuperação do host.
- Implementar namespaces de memória ou exposição pública da API (T11/T12/T9).

---

## 3. Decisões de arquitetura propostas

| # | Decisão | Recomendação | Por quê |
|---|---|---|---|
| D1 | Onde mora a autoridade | **Rust daemon** (Control Plane) | T6 já é a autoridade final; identidade em Python criaria dois cérebros |
| D2 | Formato de credencial | **Token opaco + PoP P-256** para refresh; access token opaco de 15 min | Revogável, sem JWT, sem chave de assinatura global para vazar |
| D3 | Banco | Tabelas de identidade no **control-plane DB** (`data/databases/nexus_approvals.db`) | Um único store transacional Rust; backup já cobre o arquivo |
| D4 | Chave do dispositivo | **ECDSA P-256** (WebCrypto) | Funciona em PWA/Android/iOS; Ed25519 fica opcional para agentes |
| D5 | Autonomia | `min(máquina, principal, dispositivo, tarefa/agente)` | Mantém o teto global e adiciona limites por identidade |
| D6 | Unidade de device | **um principal por (usuário, hardware)** | Xiaomi compartilhado não pode dar o papel de um usuário ao outro |
| D7 | Pairing | QR com segredo de 256 bits + código de 8 dígitos, TTL 5 min, 5 tentativas | QR é o caminho normal; código é fallback controlado |
| D8 | Recuperação do owner | CLI local via UID (com daemon parado, se necessário) | Evita lockout; exige acesso físico e gera evento de auditoria |

---

## 4. Modelo de domínio

```
roles 1──n role_scopes
  │
  └──n principals 1──n credentials
            │  │
            │  ├──n principal_scopes   (narrowing explícito)
            │  └──1 devices            (só kind='device')
            │
            ├──n principals (parent_id: device/agent → usuário)
            └──1 agent_identities      (só kind='agent')

pairing_sessions ──(consome)──> principal + device + credential
```

### 4.1 Principals

`kind` ∈ `user | device | agent | service`. Todo principal tem um `role_id`;
device e agent têm `parent_id` apontando para o principal que os criou.

- `user`: o owner, a esposa, convidados.
- `device`: um hardware pareado **para um usuário**.
- `agent`: identidade efêmera de tarefa, filha de um user/agent.
- `service`: componentes internos (`python-runtime`, `nexus-daemon`, `sentinel`).

### 4.2 Credenciais

| kind | Segredo | Uso |
|---|---|---|
| `local_owner` | hash do token local | recuperação e administração no host |
| `device_key` | chave pública P-256 | prova de posse no refresh |
| `access` | hash SHA-256 | bearer de 15 min |
| `refresh` | hash SHA-256 + família | rotação de 30 dias com detecção de reuse |
| `agent` | hash SHA-256 | token de tarefa, sem refresh |

Nenhum segredo em texto plano é persistido. Prefixos distintos para detecção
em logs/scanners: `nxo_` (owner local), `nxr_` (refresh) e `nxa_` (access e
agente).

### 4.3 Invariantes

1. **I1** — Todo principal ativo tem exatamente um papel.
2. **I2** — `principal_scopes`, quando presente, é subconjunto dos escopos do
   papel; nunca adiciona privilégio.
3. **I3** — Device e agent nunca têm escopo efetivo maior que o principal pai.
4. **I4** — Agente nunca recebe `security.approvals.decide.*` nem
   `identity.*.manage`.
5. **I5** — Revogar um principal revoga em cascata credenciais, devices e
   agentes filhos, na mesma transação.
6. **I6** — Pairing session é single-use; consumo é atômico
   (`pending → consumed`) e falha se já foi consumida.
7. **I7** — Refresh rotacionado que reaparece revoga a família inteira e o
   device (reuse detection).
8. **I8** — Autonomia efetiva é o mínimo dos tetos; nunca a soma.

---

## 5. Escopos e papéis

### 5.1 Catálogo inicial

| Escopo | Permite |
|---|---|
| `identity.self.read` | ver o próprio principal e dispositivos |
| `identity.principals.read` | listar principals, devices e auditoria |
| `identity.principals.manage` | criar, suspender, revogar principals |
| `identity.devices.manage` | revogar/renomear dispositivos |
| `identity.pairing.create` | iniciar uma sessão de pairing |
| `security.approvals.read.self` | ver as próprias solicitações |
| `security.approvals.read.all` | ver todas as solicitações |
| `security.approvals.decide.self` | decidir solicitações próprias |
| `security.approvals.decide.others` | decidir solicitações de terceiros |
| `tools.read` | ferramentas de leitura |
| `tools.write` | ferramentas de escrita |
| `tools.execute` | execução/processos |
| `memory.read.self` / `memory.write.self` | memória privada |
| `memory.read.shared` / `memory.write.shared` | memória compartilhada da família |
| `memory.read.all` / `memory.delete.any` | administração de memória |
| `agents.read` / `agents.spawn` / `agents.manage` | agentes |
| `simulations.run` | simulações |
| `llm.local` / `llm.remote` | inferência local / provedor remoto |
| `system.status` | health e diagnóstico |
| `system.backup` | operações de backup |
| `system.admin` | manutenção e control plane |

Escopos são versionados em `contracts/identity/scopes.json`; adicionar um escopo
novo exige atualizar o catálogo, os papéis built-in e os testes.

### 5.2 Papéis built-in

| Papel | Escopos (resumo) |
|---|---|
| `owner` | todos os escopos built-in da versão atual (lista explícita, sem curinga), incluindo `identity.*`, `security.approvals.*.all`, `system.*` |
| `family` | `identity.self.read`, `security.approvals.*.self`, `tools.read/write`, `memory.*.self/shared`, `agents.read/spawn`, `simulations.run`, `llm.local`, `system.status` |
| `guest` | `identity.self.read`, `tools.read`, `memory.read/write.self` (efêmera), `simulations.run`, `llm.local` |
| `service` | escopos explícitos por componente; sem identidade, sem aprovação, sem `llm.remote` por padrão |

O papel `family` **não** inclui `llm.remote` nem `system.admin`; habilitar egress
remoto para um membro é um grant explícito do owner.

### 5.3 Cálculo do escopo efetivo

```text
role      = scopes(role(principal))
granted   = principal_scopes(principal)        # se vazio, usa role
device    = constraints(device)                # subconjunto
agent     = manifest ∩ parent_effective
effective = role ∩ granted ∩ device ∩ agent
autonomy  = min(machine, principal.max_autonomy, device.max_autonomy, task.max_autonomy)
```

Ferramentas também checam `scope` por acesso (`tools.read|write|execute`) e, no
futuro, `llm.remote` antes de qualquer egress. A política de risco do T6
continua valendo **depois** da checagem de escopo: escopo não transforma
`CRITICAL` em permitido.

---

## 6. Pairing e ciclo de vida

### 6.1 Sequência

1. Owner autenticado cria uma sessão:
   `POST /api/v1/identity/pairing` com `{role, label, constraints, ttl_s}`.
   Resposta: `{session_id, secret, code, expires_at, qr_uri}`.
2. O dispositivo lê o QR (segredo de 256 bits) ou digita o código.
3. O dispositivo gera um par P-256 **não exportável**, assina
   `session_id|nonce|timestamp` e envia
   `POST /api/v1/identity/pair`.
4. O daemon valida segredo/código, TTL, tentativas, assinatura e constraints;
   em uma transação cria principal (`device`), `devices`, credencial
   `device_key` + `refresh`, marca a sessão como `consumed` e emite
   `identity.device.paired`.
5. O dispositivo recebe `{principal_id, device_id, refresh_token}` e passa a
   pedir access tokens em `POST /api/v1/identity/token`.
6. A cada refresh, o dispositivo assina o token atual; o servidor rotaciona,
   invalida o anterior e detecta reuse.

### 6.2 Ciclo de tokens

| Token | TTL | Armazenamento no cliente | Revogação |
|---|---|---|---|
| access | 15 min | memória | lookup no daemon; efeito imediato |
| refresh | 30 dias | IndexedDB/keystore | rotação + revogação do device |
| device key | enquanto pareado | keystore não exportável | revogação do device |
| agent | duração da tarefa | memória do runtime | fim da tarefa ou revogação do pai |

Reuse de refresh rotacionado → `identity.credential.reuse_detected` + revogação
da família + `identity.device.revoked`. É a resposta a token roubado.

### 6.3 Revogação

- Device: `DELETE /api/v1/identity/devices/{id}` (ou
  `nexus identity revoke <id>`), transação única: principal `revoked`,
  credenciais `revoked`, device `revoked`, agentes filhos `revoked`, evento.
- Usuário: cascata em devices e agentes; aprovações pendentes viram `DENIED`.
- Access tokens de device revogado falham no próximo lookup (≤ 15 min de
  exposição máxima; refresh falha imediatamente).
- O gateway do T9 mantém cache de 30 s e invalida por evento
  `identity.*.revoked` no Event Fabric.

### 6.4 Bootstrap e recuperação

- `nexus identity init` cria o principal `owner` e a credencial
  `local_owner` a partir do token local; idempotente.
- Aprovações antigas (T6) ficam com `principal_id NULL` e são tratadas como
  `system/legacy`, somente leitura.
- Se o banco de identidade corromper, a recuperação exige acesso físico:
  parar o daemon, rodar `nexus identity recover --owner`, revisar o evento
  `identity.recovery.local` no próximo boot. Não existe recuperação remota.

---

## 7. Integração com o Execution Gate

Fluxo autoritativo (com `[control_plane] enabled = true`):

```text
credencial → principal → status → escopos efetivos → autonomia efetiva
   → authz de escopo (tools.*)
   → política/risco do T6
   → aprovação (vinculada a principal/device/params_hash)
   → recurso → execução → commit → auditoria
```

Mudanças de contrato (v1.1.0, compatível com v1.0.0):

- `authorization_request` ganha `principal_id` e `device_id` opcionais. O
  daemon **resolve os escopos**, nunca confia em escopo enviado pelo cliente.
- Sem `principal_id` (cliente legado/local), o daemon mapeia o peer UID para o
  principal `local-owner` e mantém o comportamento do T6.
- `approval_record` ganha `principal_id`, `device_id`, `decided_by_principal_id`
  e `required_scope`; a decisão exige `security.approvals.decide.*` e o
  `decided_by` deixa de ser texto livre.
- O hash canônico de params continua sendo a proteção contra parameter
  swapping; o vínculo agora inclui o principal.

Matriz fail-closed:

| Situação | Resultado |
|---|---|
| principal inexistente/revogado/suspenso | `DENY` |
| device revogado | `DENY` |
| escopo ausente | `DENY` |
| autonomia efetiva insuficiente | `REQUIRE_APPROVAL` |
| aprovador sem `decide.*` | `DENY` |
| agente tentando aprovar | `DENY` |
| daemon indisponível | `DENY` |

---

## 8. Agentes e delegação

- `agents.spawn` cria um principal `agent` com `parent_id`, TTL de tarefa e
  escopos = `manifest ∩ parent_effective`.
- O agente recebe um token `agent` de curta duração, sem refresh.
- `tool_access` do manifesto é interseção, não união.
- Agente nunca aprova, nunca gerencia identidade e nunca cria outro agente com
  escopo maior.
- Ao terminar a tarefa: token revogado, principal `revoked`, evento
  `identity.agent.terminated`.

---

## 9. Dados e migração

### 9.1 Control-plane DB (Rust)

O T6 usa `data/databases/nexus_approvals.db`. O T8 adiciona um versionamento
próprio do control-plane DB (`schema_migrations`; a versão 1 representa as
tabelas do T6 e a versão 2 a identidade — não confundir com a migração 5 do
event store Python). A implementação vive em
`core/nexus-daemon/src/control_schema.rs`; `ApprovalStore` e `IdentityStore`
usam a mesma migração, e o catálogo é semeado na versão 2. DDL:

```sql
CREATE TABLE roles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE role_scopes (
  role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  PRIMARY KEY (role_id, scope)
);

CREATE TABLE principals (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('user','device','agent','service')),
  display_name TEXT NOT NULL,
  role_id TEXT NOT NULL REFERENCES roles(id),
  parent_id TEXT REFERENCES principals(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('active','suspended','revoked')),
  max_autonomy INTEGER NOT NULL DEFAULT 0 CHECK (max_autonomy BETWEEN 0 AND 4),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  revoked_at REAL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE principal_scopes (
  principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  granted_by TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  granted_at REAL NOT NULL,
  expires_at REAL,
  PRIMARY KEY (principal_id, scope)
);

CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('local_owner','device_key','access','refresh','agent')),
  secret_hash TEXT,
  public_key TEXT,
  algorithm TEXT CHECK (algorithm IN ('p256','ed25519')),
  family_id TEXT,
  rotated_from TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','rotated','revoked','expired')),
  created_at REAL NOT NULL,
  last_used_at REAL,
  expires_at REAL,
  revoked_at REAL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  model TEXT,
  app_version TEXT,
  public_key_fingerprint TEXT NOT NULL,
  constraints TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  paired_at REAL NOT NULL,
  last_seen_at REAL,
  revoked_at REAL
);

CREATE TABLE pairing_sessions (
  id TEXT PRIMARY KEY,
  secret_hash TEXT NOT NULL,
  code_hash TEXT,
  role_id TEXT NOT NULL REFERENCES roles(id),
  created_by TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  constraints TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('pending','consumed','expired','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL
);

CREATE TABLE identity_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

ALTER TABLE approvals ADD COLUMN principal_id TEXT;
ALTER TABLE approvals ADD COLUMN device_id TEXT;
ALTER TABLE approvals ADD COLUMN required_scope TEXT;
ALTER TABLE approvals ADD COLUMN decided_by_principal_id TEXT;
```

Índices: `principals(kind,status)`, `credentials(principal_id,status)`,
`credentials(family_id)`, índice único parcial em `credentials(secret_hash)`
(permite detectar reuse sem impedir múltiplos hashes nulos) e
`pairing_sessions(status,expires_at)`.

### 9.2 Regex/validação

- IDs `prn_`/`dev_`/`cred_`/`pair_` + base64url ou UUIDv7.
- `display_name` 1–128 chars, sem caracteres de controle.
- `scope` contra o catálogo; desconhecido = erro, não ignorado.
- Metadados JSON com `allow_nan=False`, limite de 4 KiB.

### 9.3 Retenção

- Sessões de pairing expiradas: purge após 24 h.
- Credenciais revogadas: mantidas 180 dias (hash apenas) para auditoria.
- Eventos de identidade: mínimo 365 dias (hoje `retention_days = 0` significa
  infinito; o T11 define a política final).

---

## 10. Superfície de API e IPC

### 10.1 HTTP (T8: loopback; T9 monta atrás do gateway)

| Método | Rota | Escopo | Uso |
|---|---|---|---|
| POST | `/api/v1/identity/pairing` | `identity.pairing.create` | criar sessão/QR |
| POST | `/api/v1/identity/pair` | segredo da sessão | concluir pairing |
| POST | `/api/v1/identity/token` | refresh + PoP | rotacionar tokens |
| GET | `/api/v1/identity/whoami` | `identity.self.read` | principal atual |
| GET | `/api/v1/identity/principals` | `identity.principals.read` | listar |
| POST | `/api/v1/identity/principals/{id}/revoke` | `identity.principals.manage` | revogar |
| GET | `/api/v1/identity/devices` | `identity.devices.read` | listar devices |
| DELETE | `/api/v1/identity/devices/{id}` | `identity.devices.manage` | revogar device |
| GET | `/api/v1/identity/audit` | `identity.principals.read` | eventos |

A checagem de permissão usada pelo gateway é **IPC** (`IdentityPermissionCheck`),
não uma rota HTTP.

Exemplo de pairing:

```json
// POST /api/v1/identity/pairing
{"role": "family", "label": "Xiaomi da esposa", "ttl_s": 300,
 "constraints": {"max_autonomy": 1, "scopes": ["tools.read", "memory.read.shared"]}}

// resposta
{"session_id": "pair_01J...", "secret": "nxp_...", "code": "482913",
 "expires_at": "2026-11-17T03:05:00Z",
 "qr_uri": "nexus://pair?session=pair_01J...&secret=nxp_..."}
```

### 10.2 IPC (Rust)

Novos `IpcFrame` (PascalCase, no padrão do T6):

- `IdentityList { kind?, status? }`
- `IdentityGet { principal_id }`
- `IdentityCreatePrincipal { kind, display_name, role_id, parent_id?, max_autonomy }`
- `IdentityRevokePrincipal { principal_id, reason }`
- `IdentityPairStart { role_id, constraints, ttl_s }`
- `IdentityPairComplete { session_id, secret, public_key, proof }`
- `IdentityTokenRefresh { refresh_token, signature, timestamp, nonce }`
- `IdentityVerifyToken { token }` → principal + escopos efetivos + autonomia
- `IdentityPermissionCheck { principal_id, scope, resource? }`
- `IdentityDeviceList` / `IdentityDeviceRevoke { device_id, reason }`
- `IdentityRotateLocalOwner { new_token_hash }`

Toda resposta inclui `protocol_version`; o wire continua rejeitando campos
desconhecidos. Comandos de identidade exigem peer UID igual; comandos de
administração exigem escopo `identity.principals.manage` do principal local.

---

## 11. Auditoria

Eventos (payload sem segredos):

| Evento | Campos |
|---|---|
| `identity.principal.created` | `principal_id`, `kind`, `role`, `actor_id` |
| `identity.principal.revoked` | `principal_id`, `reason`, `actor_id` |
| `identity.device.paired` | `principal_id`, `device_id`, `platform`, `role` |
| `identity.device.revoked` | `device_id`, `reason`, `actor_id` |
| `identity.credential.issued` | `credential_id`, `kind`, `expires_at` |
| `identity.credential.reuse_detected` | `family_id`, `device_id` |
| `identity.auth.succeeded/failed` | `principal_id?`, `method`, `reason?` |
| `identity.scope.denied` | `principal_id`, `scope`, `tool_id?` |
| `identity.recovery.local` | `actor_id`, `host` |

O HUD ganha um painel **Identity**: devices ativos, pairing pendente, últimos
eventos e botão de revogação. A CLI ganha:

```bash
nexus identity init
nexus identity status
nexus identity list
nexus identity pair --role family --label "Xiaomi"
nexus identity revoke <principal_id>
nexus identity devices
nexus identity audit --limit 50
```

---

## 12. Threat model

| Ameaça | Vetor | Mitigação |
|---|---|---|
| Brute force do pairing | código de 8 dígitos | TTL 5 min, 5 tentativas, rate limit no T9, segredo de 256 bits no QR |
| Roubo de access token | XSS, log, backup | TTL 15 min, hash em repouso, CSP no PWA, nunca logar token |
| Roubo de refresh token | disco do celular | PoP P-256 obrigatório; reuse revoga a família |
| Replay de refresh | reenvio | assinatura com timestamp/nonce + janela de 60 s |
| Escalada por grant | admin malicioso/erro | grant ⊆ role, validação no store, auditoria |
| Escalada por agente | manifesto amplo | interseção com o pai, sem aprovação, TTL de tarefa |
| Device revogado offline | token em cache | access 15 min + invalidação por evento no gateway |
| Xiaomi compartilhado | um device, duas pessoas | um principal por (usuário, hardware); troca de sessão explícita |
| Mesmo UID malicioso | processo local | permanece no trust boundary; mitigação é usuário dedicado (T7) e gateway/TLS (T9) |
| Prompt injection pedindo ferramenta | LLM | gate + escopo + risco; LLM nunca autoriza |
| Banco adulterado | acesso ao SQLite | hash de token, sem segredo em claro; integridade/backup |

---

## 13. Plano de entrega

| Task | Entrega | Aceite |
|---|---|---|
| **T8.1** | Catálogo de escopos + papéis built-in + ADR 0023 | **Concluído no workspace**; catálogo versionado e testes registrados no ADR 0023 |
| **T8.2** | Migração 2 + `IdentityStore` Rust | **Concluído no workspace**; migração e invariantes validadas conforme ADR 0023 |
| **T8.3** | IPC de identidade + `ControlPlaneClient` Python | **Concluído no workspace**; IPC exige escopos administrativos |
| **T8.4** | Bootstrap do owner + CLI (`init/status/list/revoke`) | **Concluído no workspace**; inicialização idempotente e auditoria |
| **T8.5** | Pairing + tokens (QR, P-256, rotação, reuse detection) | pairing single-use; replay e reuse revogam; testes negativos |
| **T8.6** | Gate integrado (principal/device/escopo/autonomia) | matriz fail-closed; aprovação vinculada; legado v1.0 funciona |
| **T8.7** | Agentes (identidade, delegação, TTL) | agente nunca excede o pai; sem aprovação; término revoga |
| **T8.8** | Rotas loopback + painel Identity no HUD | CRUD auditado; nenhuma rota pública; CSP |
| **T8.9** | Matriz de testes + docs + backup/restore da identidade | suíte verde; restore do control DB valida principals/devices |

**Fora do T8:** TLS, rate limit de rede, exposição pública e autenticação de
dispositivo no gateway (T9); namespaces de memória (T11/T12); quotas e dispatch
remoto de agentes (T17).

---

## 14. Matriz de testes

| Camada | Casos mínimos |
|---|---|
| Rust store | CRUD, unicidade, cascata de revogação, pairing atômico, reuse, TTL, migração |
| Rust IPC | comandos novos, peer UID, campos desconhecidos, versão, admin sem escopo |
| Python gate | escopo ausente, principal revogado, device revogado, autonomia mínima, aprovação errada |
| Contratos | fixtures válidas/inválidas de authorization/approval/identity, paridade Python↔Rust |
| E2E | pair → whoami → diagnostics → refresh → revoke → deny; restart preserva; daemon down nega |
| Segurança | 5 tentativas de código, sessão expirada, refresh replayed, grant acima do papel, agente tentando aprovar |
| Performance | verify < 5 ms p95; pairing < 50 ms; 10k principals sem degradação |

---

## 15. Rollout e rollback

1. Migração + store sem mudar comportamento (`[identity] enabled = false`).
2. `nexus identity init` cria o owner; token local continua funcionando.
3. Gate passa a resolver principal quando `enabled = true`; clientes legados
   caem em `local-owner`.
4. Pairing habilitado para os três devices.
5. T9 passa a exigir identidade para qualquer conexão não-loopback.

Rollback: `[identity] enabled = false` restaura o caminho do T6 (token local +
autonomia global). Dados de identidade permanecem no banco para reativação.

---

## 16. Decisões registradas no ADR 0023

1. A autoridade de identidade mora no daemon Rust.
2. O control DB permanece `data/databases/nexus_approvals.db`.
3. `family` não recebe `llm.remote` por padrão; o grant é explícito.
4. Pairing usa QR e código fallback de oito dígitos, TTL de cinco minutos e
   cinco tentativas; rate limit de rede é parte do T9.
5. Cada relação usuário/hardware tem um principal de dispositivo próprio.
6. WebAuthn/biometria fica para T18 como fator adicional opcional.

---

## 17. Critérios de saída do T8

- [ ] Owner + POCO, Xiaomi e Moto G34 pareados + 1 agente operando com escopos
  corretos.
- [ ] Pairing, refresh, revogação e reuse cobertos por testes negativos.
- [ ] Execution Gate nega principal/device revogado e escopo ausente.
- [ ] Aprovação registra principal e device; agente não aprova.
- [ ] Nenhuma rota de identidade exposta fora de loopback.
- [ ] Auditoria completa, sem segredos em payload.
- [ ] Restore do control DB preserva principals, devices e credenciais.
- [ ] ADR 0023, docs e runbook de recuperação local mergeados.
