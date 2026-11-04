# ADR 0002 — Backend local (Fase 2)

- **Status:** aceito
- **Data:** 2026-11-03
- **Escopo:** API HTTP, WebSocket, autenticação local, permissões, ciclo do serviço
- **Depende de:** ADR 0001

## Contexto

A Fase 1 entregou configuração, logging, SQLite/WAL, EventStore e EventBus, mas
nenhuma forma de outro processo (HUD, applet, CLI remota) observar o sistema.
A Fase 2 expõe o runtime sem abrir autoridade: a API apenas lê e registra.

Toolchain: o ADR 0001 registrou `pip`/`venv` ausentes. Resolvido **sem root**,
com `python3 -m venv --without-pip .venv` + `get-pip.py` do PyPA, seguido de
`pip install fastapi uvicorn websockets httpx` dentro do `.venv` do projeto.
Nenhum pacote foi instalado no Python do sistema.

## Decisão 1 — FastAPI + uvicorn, um processo, loopback

`uvicorn` com `workers=1` em `127.0.0.1:8765` (configurável). Um único worker
porque o SQLite é serializado e o estado do EventBus/hub é em memória: múltiplos
workers quebrariam a coerência do stream. Bind em loopback por padrão;
`server_header=False`; `/docs`, `/redoc` e `/openapi.json` desabilitados.

## Decisão 2 — Token local com permissões derivadas da autonomia

- Token em `data/secrets/api_token` (0600, diretório 0700), gerado no primeiro
  `nexus serve`, ou `NEXUS_API_TOKEN` (precedência).
- Comparação com `hmac.compare_digest`.
- O token **identifica**, não autoriza: cada rota exige escopo e o escopo só é
  liberado conforme o nível de autonomia configurado.

| escopo | autonomia mínima | rotas |
|---|---|---|
| `read` | 0 (observação) | status, config, events GET, kinds, stats, sessions |
| `write` | 1 (assistência) | POST /events |
| `execute` | 2 (execução controlada) | nenhuma ainda (Fase 3+) |

Consequência prática: com `autonomy_level = 0` (padrão de produção) a API é
somente leitura, e o 403 explica o motivo em vez de falhar em silêncio.

## Decisão 3 — Autenticação do WebSocket no primeiro frame

Navegadores não enviam cabeçalhos em `WebSocket`. Em vez de token na query
string (que aparece em logs e histórico), o servidor aceita a conexão e exige
`{"type":"auth","token":"..."}` em até 5 s; caso contrário fecha com código
`4401`. Rejeições são auditadas.

## Decisão 4 — Backpressure explícito no tempo real

Cada cliente tem fila limitada (256). Se o consumidor atrasa, o evento mais
antigo é descartado, o descarte é contado e o cliente recebe
`{"type":"events.dropped","count":n}`. O HUD nunca supõe stream completa.
Ping do servidor a cada 20 s mantém liveness; limite de 32 sessões.

## Decisão 5 — Manutenção dentro do processo da API

A API hospeda o laço de heartbeat/retenção existente (`serve`), em vez de exigir
dois processos residentes. `nexus run` continua disponível para operação sem
API. Regra operacional: apenas **um** processo residente por banco.

## Decisão 6 — Auditoria de acesso negado com throttle

Cada negação vira `security.api.denied` (severity `warning`) com motivo, caminho
e cliente. Throttle de 2 s evita que tentativas repetidas inchem o banco; o
campo `suppressed_since_last` informa quantas foram agregadas.

## Decisão 7 — Dedup opt-out por evento (descoberto em teste real)

No teste de integração, o segundo `system.api.stopped` (payload idêntico ao
primeiro) foi descartado pela deduplicação por conteúdo — ou seja, uma transição
de ciclo de vida legítima desaparecia. Correção na primitiva: `Event.dedup`
(`True` por padrão); transições de ciclo de vida usam `dedup=False` e recebem
`dedup_key=None`. Deduplicação continua valendo para fatos repetidos.

## Riscos conhecidos

1. **Token em texto plano no disco.** Quem lê o arquivo controla a API local.
   Aceitável em loopback; mover para hash exigiria reentrega de segredo ao
   usuário (decidido contra nesta fase). TLS não se aplica a loopback.
2. **Sem rate limit geral.** Só negações são limitadas; uma sessão autenticada
   pode emitir eventos em volume. Limite por cliente entra com o Execution
   Engine (Fase 3), quando houver custo real.
3. **Sem CORS.** O HUD (Fase 7) definirá origem permitida explicitamente em vez
   de `*`.
4. **`httpx` é dependência de teste.** Starlette 1.10 avisa sobre `httpx2`;
   troca é uma linha quando necessário.
5. **Stream vive em memória.** Reinício do processo perde assinaturas; o cliente
   reconecta e pode pedir `?replay=N`.

## Plano de testes (executado)

`tests/test_api.py` (16 casos): `/health` aberto; 401 sem/with token errado com
auditoria; nível 0 nega escrita (403 + `security.api.denied`); `/config` não
expõe o token; POST/GET/404; validação de kind/severity/since; cap de `limit`
pelo `query_limit`; `/kinds`, `/stats`, `/sessions`; WebSocket exige frame de
auth e fecha 4401; stream entrega evento publicado e respeita filtro `agent.*`;
`?replay=10` reenvia histórico.

Integração manual com servidor real (uvicorn): `/health`, 401, 200 autenticado,
403 em nível 0, 201 em nível 1, evento entregue por WebSocket com filtro,
`/stats` com contadores de hub, shutdown gracioso registrando `system.api.stopped`.

## Critérios de conclusão — atendidos

- [x] 64 testes verdes (`unittest`, `.venv/bin/python`).
- [x] API responde em loopback com autenticação obrigatória.
- [x] Permissão de escrita segue o nível de autonomia, com auditoria.
- [x] WebSocket entrega eventos em tempo real com filtro e replay.
- [x] Unit systemd validada (`systemd-analyze --user verify`), ainda não ativada.

## Fora de escopo

LLM Router, agentes, planner, memória vetorial, SudoBroker/Command Policy,
watcher Rust, GTK4/Cinnamon/HUD/TUI, Vision/ASR/Browser (Fases 3–9).
