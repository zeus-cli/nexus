# NEXUS Security

**Updated:** 2026-11-12

## HITL Execution Gate

Toda execução de ferramenta no NEXUS deve atravessar o Execution Gate:

```
Tool Registry -> PolicyEngine -> ApprovalManager -> Execution Reservation
-> Executor -> Result Validator -> Execution Journal -> Audit Log
```

- Decisões da PolicyEngine: `ALLOW`, `REQUIRE_APPROVAL`, `DENY`.
- Vínculo criptográfico: aprovações são vinculadas ao SHA-256 de `(tool_id, params, task_id, plan_id, mode, access, scope, policy_version)`.
- Protocolo de consumo: at-most-once. Status: `PENDING -> APPROVED -> CLAIMED -> CONSUMED`.
- **Estado atual da integração:** o `AutomationEngine` aplica `PolicyEngine` + `ApprovalManager` diretamente e aceita um `ExecutionGate` opcional; o bootstrap ainda não injeta o gate centralizado por padrão (TD-015).

## HITL Lifecycle and Recovery

O sistema HITL impõe transições estritas para impedir execução dupla:

```
APPROVED -> CLAIMED -> CONSUMED (STARTING) -> COMPLETED/FAILED
```

- O consumo ocorre **antes** do lançamento do subprocesso.
- Aprovações expiradas são rejeitadas em `claim_approval()`. Uma decisão tardia pode ser registrada para auditoria, mas não pode ser consumida.
- `recover_orphans()` marca como `UNKNOWN` execuções presas em `PREPARED`/`STARTING`/`RUNNING` por mais de 5 minutos.
- `SandboxExecutor` é usado estritamente via `AutomationEngine` (com `ExecutionGate` opcional).
- Todas as decisões devem ser assinadas por um `APIUser` autenticado.

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
