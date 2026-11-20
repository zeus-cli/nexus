# NEXUS Security

**Atualizado:** 2026-11-20

## Estado de integração

O bootstrap injeta o `ExecutionGate` no `AutomationEngine` no modo local. O
daemon Rust pode assumir política, aprovação e consumo quando
`[control_plane] enabled = true`; esse modo está desativado por padrão e ainda
aguarda validação operacional no Acer Nitro V15. Identidade por pessoa e
dispositivo está em implementação no T8; a API atual continua usando um token
local único e não aceita clientes móveis autenticados individualmente.

## HITL Execution Gate

Toda execução de ferramenta no NEXUS deve atravessar o Execution Gate:

```
Tool Registry -> PolicyEngine -> ApprovalManager -> Execution Reservation
-> Executor -> Result Validator -> Execution Journal -> Audit Log
```

- Decisões da PolicyEngine: `ALLOW`, `REQUIRE_APPROVAL`, `DENY`.
- Vínculo criptográfico: aprovações são vinculadas ao SHA-256 de `(tool_id, params, task_id, plan_id, mode, access, scope, policy_version)`.
- Protocolo de consumo: at-most-once. Status: `PENDING -> APPROVED -> CLAIMED -> CONSUMED`.
- **Estado atual da integração:** o bootstrap cria e injeta o `ExecutionGate` no
  `AutomationEngine`. A autoridade Rust é opcional e só entra em vigor com
  `[control_plane] enabled = true`.

## HITL Lifecycle and Recovery

O sistema HITL impõe transições estritas para impedir execução dupla:

```
APPROVED -> CLAIMED -> CONSUMED (STARTING) -> COMPLETED/FAILED
```

- O consumo ocorre **antes** do lançamento do subprocesso.
- Aprovações expiradas são rejeitadas em `claim_approval()`. Uma decisão tardia pode ser registrada para auditoria, mas não pode ser consumida.
- `recover_orphans()` marca como `UNKNOWN` execuções presas em `PREPARED`/`STARTING`/`RUNNING` por mais de 5 minutos.
- `SandboxExecutor` é chamado pelo `AutomationEngine` depois das verificações do
  gate; o gate não reexecuta a operação.
- Decisões humanas são atribuídas a um `APIUser` autenticado. A vinculação a
  principal e dispositivo continua pendente no T8.

## API Local

- Bind exclusivo em loopback (`127.0.0.1`, `::1`, `localhost` e variantes `*.localhost`).
- Token local em `data/secrets/api_token` (`0600`) ou `NEXUS_API_TOKEN`.
- Comparação com `hmac.compare_digest`.
- Escopos derivados do nível de autonomia:
  - `read` — autonomia 0 (observação);
  - `write` — autonomia 1 (assistência);
  - `execute` — autonomia 2 (execução controlada).
- Sem TLS por decisão de projeto: a API não deve ser exposta fora do loopback.

## LLM e Egress

- Provedores remotos exigem `authorized = true` e HTTPS.
- O Ollama local em loopback é permitido sem TLS.
- O egress guard aplica níveis de privacidade:
  - `PUBLIC` — pode ir para qualquer provedor autorizado;
  - `PRIVATE` — apenas provedores locais/verificados;
  - `SECRET` — nunca sai da máquina.
- O OpenCode é uma ferramenta externa e não faz parte da fronteira de segurança do NEXUS.

## Persistência

- Diretórios `0700`, arquivos `0600`; symlinks recusados nos caminhos gerenciados.
- SQLite em WAL com `busy_timeout`, commit/rollback explícitos e fechamento garantido (`nexus_core/sqlite_utils.py`).
- Task Store usa escrita atômica com `O_NOFOLLOW`/fsync e checksum SHA-256.
- Criptografia do Task Store (Fernet, com fallback XOR) é opcional e está desativada por padrão; para ambientes de alto sigilo, forneça uma chave.
- Execution Journal recalcula o `integrity_hash` a cada mudança de status e oferece `verify_all_integrity()`.

## Riscos Residuais

- **Fronteira de processo:** se um atacante obtiver execução arbitrária dentro do processo Python, os controles em Python podem ser contornados.
- **Token em texto plano:** qualquer processo do mesmo usuário pode ler o token.
- **Sandbox como UID 0:** o `bwrap` roda como root dentro do namespace; um escape de namespace daria root no host.
- **SQLite síncrono em caminhos assíncronos:** `busy_timeout` reduz contenção, mas não elimina bloqueio do event loop.
- **Criptografia opcional:** o Task Store grava em texto claro quando nenhuma chave é fornecida.
- **Sem TLS:** aceito por design para loopback.
- **Identidade multi-dispositivo:** não há pairing nem autorização HTTP por
  principal/device. Não exponha a API para a LAN ou para a tailnet até o gateway
  do T9 estar implementado e revisado.
