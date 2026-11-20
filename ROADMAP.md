# Roadmap do NEXUS

**Atualizado:** 2026-11-20
**Fonte de arquitetura-alvo:** diagrama de topologia e
[`docs/NEXUS_MASTER_ARCHITECTURE.md`](docs/NEXUS_MASTER_ARCHITECTURE.md)

Legenda: [x] concluído no código/documentação; [ ] pendente ou parcial.
Uma entrega operacional só é concluída quando seus critérios no plano e no
runbook foram comprovados no host alvo.

## Estado atual — 2026-11-20

- **T6 concluído no código:** o daemon Rust pode ser autoridade final de
  execução; o modo é opt-in e está desativado em `config/nexus.toml`.
- **T7 em andamento:** ADR 0022, units do daemon/backup e script baseline estão
  no workspace. O Dell Inspiron 15 3511 é a estação de desenvolvimento; o Acer
  Nitro V15 permanece como servidor alvo e ainda precisa de validação.
- **T8 parcial:** T8.1–T8.4 têm catálogo, store Rust, IPC e CLI local do owner.
  Pairing de dispositivos e integração da identidade ao gate continuam abertos.

## Entregas T6–T20

- [x] **T6 — Execution Gate autoritativo no Rust.** Persistir aprovações e
  execuções, produzir `CLAIMED → CONSUMED` pelo ciclo de execução e negar
  execução quando o daemon está indisponível. Entrega de código registrada em
  2026-11-16; validação operacional no Acer pertence ao T7. **Depende de:** T5.
- [ ] **T7 — Host principal e rede segura.** Provisionar o Acer Nitro V15,
  serviços systemd, GPU/Ollama, rede privada, firewall e backup baseline com
  restore comprovado. Units e script estão preparados; inventário,
  configuração e validação no Acer continuam pendentes. **Depende de:** T6.
  **Plano:** [`docs/NEXUS_T7_EXECUTION_PLAN.md`](docs/NEXUS_T7_EXECUTION_PLAN.md).
- [ ] **T8 — Identidade e permissões.** T8.1–T8.4 implementados no workspace;
  faltam pairing e credenciais dos dispositivos, integração no Execution Gate,
  rotas de identidade, painel e validação de restore. **Depende de:** T6, T7.
  **Design:** [`docs/NEXUS_T8_IDENTITY_DESIGN.md`](docs/NEXUS_T8_IDENTITY_DESIGN.md).
- [ ] **T9 — API Gateway.** Entrada FastAPI/WebSocket única, HTTPS,
  autenticação de dispositivo, limites e timeouts, rotas versionadas e
  auditoria. **Depende de:** T8.
- [ ] **T10 — Event Fabric multi-dispositivo.** Pub/sub autenticado, offsets e
  confirmações duráveis, replay, presença, backpressure e sincronização de
  tarefas/resultados. **Depende de:** T8, T9.
- [ ] **T11 — Camada de dados central v2.** Interfaces de armazenamento,
  SQLite governado como padrão, namespaces por usuário/dispositivo/agente,
  arquivos, conhecimento, eventos e retenção; adapter PostgreSQL opcional.
  **Depende de:** T9.
- [ ] **T12 — API de memória e Knowledge Graph.** Memória escopada, busca
  híbrida, consolidação, privacidade, retenção, proveniência e consulta no HUD.
  **Depende de:** T11.
- [ ] **T13 — Perfil e personalidade.** Preferências, tom, limites, consentimento
  e segregação de memória por usuário. **Depende de:** T8, T11, T12.
- [ ] **T14 — Orquestrador multi-LLM.** Registro de modelos e roteamento por
  tarefa, privacidade, capacidade, latência e custo; concorrência, fallback,
  streaming e métricas. **Depende de:** T9, T13.
- [ ] **T15 — Malha distribuída de LLMs.** Agentes de nó POCO/Acer, manifests,
  health/resources, posicionamento, failover, implantação/quantização e backup
  por modelo. **Depende de:** T10, T14.
- [ ] **T16 — Planejamento e execução v2.** Ferramentas de escrita, adapters,
  capacidades por dispositivo, aprovações, rollback, journal, dry-run, scheduler
  (TD-019) e shell seguro. **Depende de:** T8, T11, T14.
- [ ] **T17 — Multiagentes v2.** Identidade e escopos de agentes, dispatch
  remoto, coordenação por eventos, memória compartilhada escopada, revisão
  adversária e quotas. **Depende de:** T10, T14, T16.
- [ ] **T18 — Clientes dos dispositivos.** PWA para Moto G34, interface Xiaomi
  e cliente POCO, com chat, aprovações, notificações, fila offline e estado do
  dispositivo. **Depende de:** T9, T10, T13.
- [ ] **T19 — Backup e recuperação de desastre.** Snapshots de identidade e
  memória, manifests/pesos LLM, integridade, versionamento, backup externo
  criptografado e drills de restore com RPO/RTO. **Depende de:** T11, T15, T18.
- [ ] **T20 — Expansão futura.** Voz (STT/TTS/wake word), observação consentida
  de contexto/ambiente e robótica ROS 2 com envelope de segurança. **Depende
  de:** T16, T17, T18.

**Caminho crítico:** T6 → T7 → T8 → T9 → (T10 ∥ T11) → T12 → T13 → T14 →
(T15 ∥ T16) → T17 → T18 → T19 → T20.

**MVP multi-dispositivo:** T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13 → T18
(chat, memória, perfil e aprovações).

**Guardrails:** manter um monólito modular; todos os dispositivos entram pelo
API Gateway; usar um Event Fabric e um LLM Router; SQLite é o padrão, com
PostgreSQL opcional e sem dual-write; o Orchestrator escolhe modelos e o
Execution Gate governa ferramentas; personalidade nunca concede permissões;
backup baseline no T7 e DR completo no T19.

## P0 — Infraestrutura e consistência

- [ ] **Nomes unificados:** remover nomes de logger/database `zeus` (TD-001,
  TD-010).
- [ ] **Validação de implantação:** core/sentinel têm evidência histórica no
  Dell de desenvolvimento; validar daemon, core e sentinel no Acer (T7).
- [x] **Ambiente Python/Rust:** `pyproject.toml`, lockfile e toolchains do
  projeto existem para o ambiente Linux alvo.

## P1 — Capacidades cognitivas e execução

- [x] Planejamento discreto e loop cognitivo adaptativo.
- [x] Loop reflexivo com sucesso, retry, replanning, aprovação e abort.
- [x] Sandbox `bubblewrap` para ferramentas.
- [x] Registry, runtime, dispatcher, orquestrador, colaboração e revisão de
  agentes.
- [x] Knowledge Graph persistente com proveniência, confiança e validade
  temporal; simulação não é promovida automaticamente a fato.
- [x] IPC do Control Plane com handshake versionado, autenticação local por
  `SO_PEERCRED`/UID, socket restrito e comandos de controle.
- [x] Paridade Python/Rust de policy e approval com fixtures compartilhadas.
- [ ] **Execução ampliada:** gate, rollback, SudoBroker e journal estão no
  código; catálogo de automação permanece em sete diagnósticos read-only.

## P2 — Modelos e memória

- [x] Memória semântica LanceDB combinada com FTS5 e RRF.
- [x] Propostas de consolidação de memória episódica em semântica.
- [x] Política de egress com níveis de privacidade e autorização/HTTPS para
  provedores remotos.

## P3 — Interfaces

- [ ] Bandeja e shell nativos GTK4/libadwaita.
- [x] Web HUD com painéis de tarefas, memória, eventos, modelos, agentes,
  simulação, segurança, logs e configuração.
- [ ] Visualização de memória e métricas de desempenho em tempo real mais
  completas.

## P4 — Autonomia e integrações

- [ ] Shell seguro governado pelo gate.
- [ ] Integrar `nexus_core/scheduler.py` ao runtime e à API (TD-019).
- [ ] Integrações DBus/API com aplicativos locais.

## Histórico de validação

- **2026-11-12:** auditoria registrou 305 testes Python + 12 subtests e checks
  estáticos limpos. Veja os documentos históricos correspondentes.
- **T6, 2026-11-16:** ADR 0021 registra 516 testes Python + 12 subtests e 86
  testes Rust no daemon.
- Esses números são resultados datados e não representam uma execução da
  árvore atual. Não há validação completa pós-T8 registrada.
