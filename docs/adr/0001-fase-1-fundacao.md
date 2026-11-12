# ADR 0001 — Fundação local-first (Fase 1)

> **Nota de atualização (2026-11-12):** decisão histórica preservada; as contagens de testes referem-se à data original. Estado atual e validação consolidados em `docs/adr/0014-endurecimento-auditoria-2026-11-12.md`.

- **Status:** aceito
- **Data:** 2026-11-03
- **Escopo:** estrutura, configuração, logging, SQLite, event store, event bus, serviço

## Contexto

O NEXUS é uma camada cognitiva local-first composta por sete camadas. Antes de
qualquer LLM, agente ou interface, é necessário um substrato confiável:
configuração explícita, observabilidade, persistência durável e um canal de
eventos desacoplado. Tudo o que as fases seguintes fizerem deve ser auditável
nesse substrato.

Ambiente inspecionado (não presumido):

| Item | Estado | Consequência |
|---|---|---|
| Python 3.12.3 | presente | runtime do backend |
| `pip` / `ensurepip` / `venv` | **ausentes** | nenhum pacote de terceiros é instalável sem ação privilegiada |
| Rust (`cargo`, `rustc`) | **ausentes** | watcher/núcleo Rust adiados |
| GTK4 / Libadwaita | runtime presente, headers `-dev` ausentes | interfaces nativas adiadas |
| `sqlite3` CLI | ausente | manutenção via módulo `sqlite3` da stdlib |
| systemd 255 | presente | unit de usuário disponível |

## Decisão 1 — Fase 1 sem dependências externas

Implementar a fundação apenas com biblioteca padrão (`sqlite3`, `tomllib`,
`asyncio`, `logging`, `argparse`). Motivo: sem `pip` no ambiente, qualquer
dependência declarada seria fictícia. FastAPI/Pydantic entram na Fase 2 quando
`python3-venv` estiver disponível (requer autorização para `apt install`).

## Decisão 2 — Uma conexão SQLite serializada por processo

`Database` mantém uma conexão única com `check_same_thread=False` protegida por
`RLock`, em modo WAL, `synchronous=NORMAL` e `busy_timeout=5000`. O volume de
eventos da Fase 1 não justifica pool, e a serialização elimina `database is
locked` previsível. Se a Fase 5 (telemetria contínua) saturar, a evolução é
particionar escritores por fila, não multiplicar conexões concorrentes.

## Decisão 3 — Migrações versionadas em tabela, não `user_version`

`schema_migrations(version, name, applied_at)` registra *o que* foi aplicado e
*quando*: auditoria explícita, exigida pelo princípio de observabilidade.
Cada migração roda em `BEGIN IMMEDIATE`/`COMMIT` com `ROLLBACK` em erro, então
o schema nunca fica parcialmente aplicado.

## Decisão 4 — Identidade e idempotência de evento

- `event_id` (UUID4) é **identidade**: reutilizá-lo com conteúdo diferente é
  erro (`EventStoreError`), nunca sobrescrita silenciosa.
- `dedup_key = sha256(kind, source, severity, payload_canônico)` com janela
  configurável (`events.dedup_window_hours`, 0 desliga): o mesmo fato reemitido
  dentro da janela retorna o registro existente com `inserido=False`.

Isso permite reprocessamento seguro por consumidores e observadores
(filesystem watcher, monitor) sem inflar o histórico.

## Decisão 5 — Persistir antes de publicar

`EventRecorder.record()` grava no EventStore e só então publica no EventBus.
Assim o bus nunca transporta um evento que não possa ser reconstruído depois.
Falha de assinante é isolada (logada, contabilizada) e não derruba o emissor.

## Decisão 6 — Estrutura mínima, sem módulos vazios

Apenas diretórios com conteúdo real foram criados. `apps/`, `nexus_core/agents`,
`planner`, `llm_router`, `security` e `core-rust` nascem nas fases 3–7, quando
tiverem responsabilidade verificável. `data/` já existe com os cinco
subdiretórios previstos para evitar migração de caminhos depois.

## Decisão 7 — Serviço residente mínimo

`nexus run` mantém o runtime, emite `system.heartbeat`, aplica retenção e
registra `system.started`/`system.stopped`. Não expõe rede nem privilégio: é a
base sobre a qual a Fase 2 montará o FastAPI/WebSocket. A unit
`systemd/nexus-core.service` é de **usuário** (`systemctl --user`) com
`ProtectSystem=strict` e escrita restrita a `data/`.

## Riscos conhecidos

1. **Schema v1 vira contrato.** Alterações exigem migração nova, nunca edição
   da migração 1.
2. **Retenção apaga dados.** `retention_days = 0` desliga a poda; o padrão é 90.
   Backup (`nexus db backup`) é manual nesta fase.
3. **Sem autenticação local ainda.** O serviço não abre porta na Fase 1; quando
   a API abrir, autenticação e permissões entram antes do primeiro endpoint.
4. **Escrita de log no mesmo volume do banco.** Aceitável localmente; rotação
   por tamanho está configurada.

## Plano de testes (executado)

- `test_config.py` — defaults, precedência de env, chaves desconhecidas,
  intervalos inválidos, arquivo ausente.
- `test_eventstore.py` — roundtrip, dedup por janela, evento com `event_id`
  reutilizado, filtros/ordenação/paginação, retenção, persistência entre
  reconexões, payload corrompido, backup legível, WAL ativo, migração idempotente.
- `test_eventbus.py` — padrões, handler síncrono, falha isolada, timeout,
  ordem por assinante, cancelamento de assinatura.
- `test_app.py` — bootstrap cria banco/log, health `ok`, ciclo completo do
  serviço (started → heartbeats → stopped), argumentos inválidos.

## Critérios de conclusão — atendidos

- [x] `python3 -m unittest discover -s tests -t .` verde (46 testes).
- [x] `nexus status` reporta `ok` com integridade do banco.
- [x] `nexus run` grava ciclo completo em `zeus_events.db`.
- [x] Dedup comprovado: segundo `events emit` idêntico não cria registro.
- [x] Backup consistente gerado e verificado.

## Fora de escopo (Fases 2–9)

API/WebSocket, LLM Router, agentes, planner, SudoBroker/Command Policy,
watcher em Rust, memória vetorial, GTK4/Cinnamon/HUD/TUI, Vision/ASR/Browser.
