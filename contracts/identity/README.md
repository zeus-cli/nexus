# Contrato de identidade (T8)

Fonte única de verdade para escopos e papéis do NEXUS. Os mesmos arquivos são
consumidos pelo Python (`nexus_core/identity_contracts.py`) e pelo daemon Rust
(T8.2). Não duplique listas em código.

| Arquivo | Conteúdo |
|---|---|
| `scopes.json` | escopos atômicos, categoria e descrição |
| `roles.json` | papéis built-in e a lista explícita de escopos de cada um |

## Regras

- `catalog_version` e `contract_version` seguem semver e devem ser idênticos
  nos dois arquivos.
- IDs de escopo seguem `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`; o primeiro
  segmento é a categoria e deve ser igual ao campo `category`.
- Não existe curinga. `owner` lista todos os escopos explicitamente.
- `principal_scopes` só reduz o papel; pedir escopo fora dele é escalada de
  privilégio.
- `family` não recebe `llm.remote`, `system.admin`, `tools.execute`,
  `memory.read.all`, `memory.delete.any`, `agents.manage`, escopos de
  administração de identidade nem `security.approvals.*.others/all`.
- `guest` é leitura + memória privada + simulações + LLM local.
- `service` não recebe identidade, aprovação, agentes nem `llm.remote`.
- As regras de proibição são verificadas em `FORBIDDEN_SCOPES` no loader
  Python e devem ser espelhadas no store Rust.

## Adicionar ou alterar um escopo

1. Edite `scopes.json` com o ID, a categoria e a descrição.
2. Adicione o escopo ao `owner` em `roles.json` — nunca presuma herança.
3. Avalie `family`, `guest` e `service`; se o escopo for sensível, inclua a
   regra em `FORBIDDEN_SCOPES` (`nexus_core/identity_contracts.py`).
4. Suba `catalog_version` nos dois arquivos e atualize os testes que fixam
   tamanho/versão (`tests/test_identity_contracts.py`).
5. Atualize o espelho Rust do T8.2 e rode as suítes Python e Rust.
6. Registre a mudança no ADR 0023 e no design do T8.

## Uso

```python
from nexus_core.identity_contracts import load_catalog, narrow_scopes

catalog = load_catalog()
effective = narrow_scopes(catalog, "family", {"tools.read", "memory.read.shared"})
```

`narrow_scopes(catalog, role)` sem o terceiro argumento devolve todos os
escopos do papel; uma coleção vazia devolve o conjunto vazio (redução
intencional). Escopo desconhecido ou fora do papel levanta erro.
