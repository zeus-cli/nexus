# ADR 0018 — Knowledge Graph persistente: nós, arestas, proveniência e confiança (T3)

- **Status:** aceito
- **Data:** 2026-11-14
- **Escopo:** T3 do roadmap — persistir o conhecimento em SQLite com nós,
  arestas, proveniência, confiança, validade temporal e consultas
  determinísticas, sem promover simulação a fato
- **Depende de:** ADR 0001 (SQLite/EventStore), ADR 0005 (Knowledge Engine),
  ADR 0017 (banco governado único após o T2)

---

## Contexto

O `KnowledgeEngine` da Fase 2 mantinha registros apenas em memória. Isso era
suficiente para o loop cognitivo de um processo, mas não para o requisito de
continuidade do NEXUS: fatos, hipóteses, predições e simulações precisam
sobreviver a reinícios, manter proveniência auditável e permitir consultas
relacionais entre entidades.

O roadmap do T3 exige exatamente:

> SQLite com nós, arestas, proveniência e confiança; migrações; consultas;
> nenhuma promoção automática de simulação para fato.

Restrições:

- preservar o backend Python e o comportamento em memória do
  `KnowledgeEngine` para usos isolados;
- usar o mesmo SQLite governado do EventStore/outbox, sem banco de grafo
  externo nem dependência nova;
- validação determinística e fail-closed, independente de LLM;
- não expor API remota nem alterar a autoridade de segurança (T4–T6);
- não reescrever eventos ou memórias existentes.

---

## Decisão

1. **Grafo no mesmo SQLite governado, migração 5.**
   `nexus_core/knowledge_schema.py` define o DDL idempotente de
   `knowledge_nodes` e `knowledge_edges`, com índices por tipo, `kind`,
   validade e extremidades. A migração 5 (`knowledge_graph`) é aplicada por
   `Database.migrate()` no bootstrap e em testes; bancos existentes sobem de
   `schema_version = 4` para `5` sem reescrever dados anteriores. As arestas
   usam `FOREIGN KEY ... ON DELETE CASCADE`, e o `Database` já habilita
   `PRAGMA foreign_keys = ON`.

2. **Nós e arestas carregam proveniência e validade.**
   Cada registro tem `kind` (`FACT`, `ASSUMPTION`, `HYPOTHESIS`, `PREDICTION`,
   `SIMULATION`), `source` (`USER`, `OBSERVATION`, `EXTERNAL`,
   `MODEL_INFERENCE`, `SIMULATION_OUTPUT`, `VALIDATED_EXPERIENCE`),
   `source_ref`, `confidence` em `[0,1]`, `created_at`, `updated_at`,
   `expires_at`, `validated_by`, `validation_evidence`, `properties` e
   `metadata`. Os enums são persistidos pelo `.name` para permanecerem
   legíveis e estáveis. `confidence` também tem `CHECK` no banco.

3. **Toda escrita valida antes de tocar o banco.**
   Identificadores seguem padrões explícitos; `node_type` e `relation` são
   identificadores minúsculos; strings rejeitam NUL e limites de tamanho;
   `confidence` rejeita `bool`, `NaN`, infinito e valores fora do intervalo;
   JSON é serializado com `allow_nan=False` e chaves string; `expires_at`
   exige timezone ISO-8601 e é normalizado para UTC; `limit` e `max_depth`
   têm tetos. Escritas usam `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` sob o
   `RLock` do `Database`, e leituras usam consultas parametrizadas.

4. **Consultas determinísticas e limitadas.**
   `KnowledgeGraph` oferece `get_node`, `list_nodes`, `get_edge`, `list_edges`,
   `neighbors`, `traverse`, `search_nodes`, `node_stats` e `stats`.
   `list_nodes`/`list_edges` excluem expirados por padrão; `search_nodes`
   ranqueia termos de label e propriedades com ordenação estável;
   `neighbors`/`traverse` fazem BFS com arestas ordenadas por
   `(relation, edge_id)`, limite de profundidade e `visited` para impedir
   ciclos e repetição de nós. Nós vizinhos/arestas expirados são filtrados na
   travessia salvo `include_expired=True`; a raiz explícita continua acessível.

5. **Simulação nunca é promovida a fato.**
   `promote_node_to_fact`/`promote_edge_to_fact` exigem `validated_by`
   explícito, registram evidência e recusam `SIMULATION`. Não há caminho
   automático de promoção: o `KnowledgeEngine` só promove quando
   `validate_fact(..., validated_by=...)` é chamado. `mark_simulation` marca
   explicitamente uma saída simulada e recusa rebaixar um fato já validado.

6. **Adapter persistente, sem quebrar o modo em memória.**
   `KnowledgeEngine(store=KnowledgeGraph)` persiste cada registro como nó
   `node_type="knowledge_record"` (conteúdo em `properties.content`) e delega
   `get`, `list`, `search`, `validate_fact`, `mark_simulation`, `delete` e
   `stats` ao grafo. Sem `store`, o dicionário em memória e o comportamento
   anterior são preservados. O `bootstrap()` cria o grafo no banco do runtime,
   injeta o engine no `CognitiveCore` e expõe `runtime.knowledge_graph` /
   `runtime.knowledge`; `health()` publica contagens agregadas do grafo.

---

## Consequências

- `schema_version` sobe para 5. A migração é idempotente e não altera tabelas
  anteriores; o grafo compartilha WAL, `busy_timeout` e a conexão serializada
  do runtime.
- O conhecimento agora sobrevive a reinícios e mantém trilha de proveniência
  para auditoria. Fatos validados ficam distinguíveis de hipóteses,
  predições e simulações.
- Consultas de grafo ainda são uma API Python interna; não há endpoints
  HTTP/TUI dedicados no T3. Expor o grafo na API/HUD pertence ao T12.
- A travessia é deliberadamente BFS e limitada; consultas analíticas amplas
  devem usar `search_nodes`/`list_nodes` ou paginação explícita.
- Não há banco de grafo externo, cache de travessia nem versionamento de
  arestas. Substituições usam `update_node`/`update_edge` com os campos
  completos, e o histórico permanece no EventStore/Journal, não no grafo.
- O `KnowledgeEngine` em memória continua sendo o default quando não há
  `store`; isso evita efeitos colaterais em bibliotecas e testes isolados.

---

## Validação

- `tests/test_knowledge_graph.py`: 26 testes cobrindo migração/idempotência,
  persistência após reopen, CRUD de nós/arestas, enums persistidos por nome,
  proveniência e confiança, rejeição de entradas inválidas/JSON
  inseguro/timezone ausente, `CHECK` de confiança no banco, filtros e
  expiração, cascade, busca, `neighbors` por direção/relação, BFS determinística
  com ciclo e limite, bloqueio de promoção de simulação, validação explícita
  com evidência, estatísticas e adapter persistente do `KnowledgeEngine`.
- `tests/test_app.py`: bootstrap cria o grafo, injeta o engine no
  `CognitiveCore`, `health()` publica as contagens e o registro sobrevive a um
  novo `bootstrap()`.
- Suíte completa: 461 testes + 12 subtests; `cargo test` do daemon permanece em
  39 testes porque o T3 não alterou o Rust.

---

## Próximo passo

T4 — IPC de controle no daemon Rust. O T3 não move autoridade nem expõe o
grafo remotamente; a API continua local e o gate continua em Python até o
Control Plane assumir os contratos de autorização/aprovação (T4–T6).
