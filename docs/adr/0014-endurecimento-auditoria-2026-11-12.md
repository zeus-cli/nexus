# ADR 0014 — Endurecimento pós-auditoria: SQLite, Event Fabric, VectorStore e CLI

- **Status:** aceito
- **Data:** 2026-11-12
- **Escopo:** correções de defeitos reais encontrados na auditoria de LLMs, conexões, rotas, banco, backend, frontend e sintaxe
- **Depende de:** ADR 0001–0013 (contratos existentes preservados)

---

## Contexto

A auditoria de 2026-11-12 validou o sistema em execução real (API, CLI, Ollama local, OpenCode remoto, Web HUD, SQLite, LanceDB e WebSocket) e encontrou divergências entre documentação, configuração e comportamento observado. O objetivo foi corrigir defeitos funcionais sem alterar os contratos públicos nem reescrever componentes estáveis.

Restrições:

- não modificar a alteração pré-existente do usuário em `nexus_core/memory/records.py`;
- preservar o fluxo HITL e os testes que dependem de decisões tardias;
- manter a API apenas em loopback e a política de egress para provedores remotos;
- manter compatibilidade com versões antigas de LanceDB e com envelopes de evento legados.

---

## Decisão 1 — Helper SQLite compartilhado

Criado `nexus_core/sqlite_utils.py::connect_db` para centralizar:

- `busy_timeout` configurável;
- `commit`/`rollback` explícitos;
- fechamento garantido da conexão;
- comportamento consistente entre ApprovalManager, Event Fabric, Results Store e Journal.

O helper reduz `database is locked` e conexões vazadas, mas não resolve o bloqueio síncrono do event loop; isso permanece como dívida técnica (TD-008).

## Decisão 2 — Event Fabric canônico e compatível

- Payload canônico (chaves ordenadas, separadores compactos) para dedup e persistência.
- Leitura de envelopes legados sem perder `severity`, `correlation_id` ou `dedup`.
- Dead-letter migrado de forma consistente, com metadados preservados.
- Timestamps de outbox/protocolo em epoch (`time.time()`), não `monotonic`, para sobreviver a reinícios e comparação entre nós.

## Decisão 3 — Integridade recalculada

`ExecutionJournal.update_status()` recalcula `integrity_hash` após qualquer mudança de status, resultado ou erro. O `SimulationAnalyzer.ResultsStore` reconstrói eventos persistidos e valida a integridade ao ler resultados.

## Decisão 4 — VectorStore idempotente e compatível

- `_table_names()` suporta `list_tables()` (retorno `TableListing`) e a API antiga `table_names()`.
- `_literal()` escapa aspas simples em filtros LanceDB (`'` → `''`).
- `upsert()` faz `delete` do vetor anterior antes de `add`, evitando duplicatas para a mesma memória.
- Testes de regressão em `tests/test_vector.py`.

## Decisão 5 — CLI com opções globais recursivas

`_iter_subparsers()` percorre subparsers aninhados e adiciona `--config`/`--json` com `default=argparse.SUPPRESS`, permitindo:

```bash
nexus --config config/nexus.toml llm models
nexus llm models --config config/nexus.toml
nexus events list --json
```

## Decisão 6 — Rotas e frontend

- `/api/v1/approvals/history` é registrada antes de `/api/v1/approvals/{request_id}`.
- `POST /api/v1/simulations/scenarios` normaliza `paradigm` para maiúsculo e retorna 422 para valor inválido.
- O painel Segurança do Web HUD carrega `/api/v1/approvals/pending`, renderiza Aprovar/Negar e usa `#security-notice`.

## Decisão 7 — Configuração LLM alinhada ao host

A cadeia local passou de modelos ausentes (`qwen2.5:7b`, `llama3.1:8b`) para os modelos realmente instalados:

```toml
models = ["nexusorbe-llama3.1:latest", "nexusorbe-llama3.2:latest"]
```

`nexus llm complete` passou a responder com sucesso. Provedores remotos continuam exigindo `authorized = true` e HTTPS; o Ollama remoto usado pelo OpenCode não foi adicionado ao `nexus.toml` por essa política.

## Decisão 8 — Expiração de aprovação no claim

`ApprovalManager.decide()` continua aceitando decisões tardias para preservar o registro de auditoria. A expiração é aplicada em `claim_approval()`, que retorna `False` para solicitações expiradas. Isso mantém os testes existentes e impede execução após o TTL.

---

## Consequências

- **Positivo:** defeitos reais corrigidos em todas as camadas; suíte verde; configuração LLM funcional; melhor compatibilidade com LanceDB e eventos legados; frontend HITL operacional.
- **Negativo:** novas dependências internas (`sqlite_utils`) e testes adicionais; a configuração local agora depende dos tags `nexusorbe-*` instalados.
- **Riscos remanescentes:** SQLite síncrono em async, criptografia do Task Store desativada por padrão, branding `zeus`, catálogo de ferramentas somente-leitura e sandbox como UID 0.

## Testes

- `tests/test_vector.py` (novo): compatibilidade de API, escape e upsert idempotente.
- `tests/test_task_store.py` (novo): atomicidade, permissões, chave vazia e leitura UTF-8.
- `tests/test_cli.py`: opções globais antes/depois de subcomandos aninhados.
- `tests/test_api.py`, `tests/test_event_fabric.py`, `tests/test_simulation.py`, `tests/test_distributed_network.py`, `tests/test_secure_agency.py` e `tests/test_config.py` atualizados.
- **Regressão completa:** 305 testes + 12 subtests passando (~129 s).
- **Estáticos:** `ruff` (E9/F63/F7/F82/F821/F822/F823), `compileall`, `node --check` e `git diff --check` limpos.
