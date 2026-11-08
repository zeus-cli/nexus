# NEXUS Technical Debt

**Versão:** 1.0  
**Projeto:** NEXUS — Cognitive Operating System  
**Data:** 2026-11-07

Registro de dívida técnica conhecida, com prioridade, estado e próxima ação. Mantido como fonte da verdade para planejamento de refatoração.

---

## TD-001 — Branding "zeus" vs "nexus" (P0) — Open

**Descrição:** O logger raiz é `"zeus"`, o banco de fallback é `zeus_events.db` e alguns comentários referenciam "zeus", criando confusão em logs, nomes de banco e imports. O projeto é "NEXUS".

**Impacto:** Confusão operacional; logs e nomes de banco inconsistentes com a marca.

**Próxima ação:** Renomear logger para `"nexus"` e banco fallback para `nexus_events.db`; auditoria de strings "zeus" no código.

---

## TD-002 — Systemd units não ativadas (P0) — Open

**Descrição:** `systemd/nexus-core.service` e `systemd/nexus-sentinel.service` existem mas não estão implantadas/ativadas. O sistema só sobe manualmente (`nexus serve` / `nexus run`).

**Impacto:** Sem auto-start nem recuperação de falhas; não pronto para produção.

**Próxima ação:** Implantar e ativar as units (`systemctl --user enable --now`); verificar `systemd-analyze --user verify`.

---

## TD-003 — Memória somente lexical (P2) — Open

**Descrição:** Busca por FTS5 lexical; vetor/semântico existe (LanceDB) mas não integrado como busca híbrida prioritária em todos os caminhos.

**Impacto:** Busca semântica limitada; memória não aproveita similaridade vetorial.

**Próxima ação:** Consolidar busca híbrida (lexical + vetorial com RRF) como padrão.

---

## TD-004 — Desktop é navegador, não GTK4 nativo (P3) — Open

**Descrição:** `nexus desktop` abre o painel web em Chrome/Chromium. Não há GUI nativa GTK4/tray.

**Impacto:** Experiência de desktop limitada; sem ícone de sistema/bandeja.

**Próxima ação:** Construir GUI GTK4/libadwaita (Command Center, Agent Control, Simulation Lab, Memory Explorer, Security Center).

---

## TD-005 — Automação estreita (P2) — Open

**Descrição:** Apenas 7 ferramentas de diagnóstico somente-leitura (`sys.uptime`, `sys.memory`, `sys.disk`, `sys.kernel`, `proc.list`, `proc.info`, `svc.status`). Sem shell arbitrário, scheduler ou rollback.

**Impacto:** Automação real limitada a diagnósticos de leitura.

**Próxima ação:** Expandir Tool Registry com adaptadores (FS/Process/Network/…), sandboxar todas as ferramentas não-triviais, implementar Rollback Manager.

---

## TD-006 — Hardware limitado para modelos locais (P4) — Open

**Descrição:** Host (i3, 11-12 GB RAM) insuficiente para modelos grandes (Gemma 4 12B/31B) localmente.

**Impacto:** Dependência de modelo remoto ou Ollama leve; falha de tarefas cognitivas se LLM indisponível.

**Próxima ação:** Adicionar modelo de fallback leve ou planejamento determinístico sem LLM; benchmarkar em host com GPU/RAM maior.

---

## TD-007 — Token de API em texto plano no disco (P1) — Open

**Descrição:** Token armazenado em `data/secrets/api_token` (0600). Qualquer processo rodando como o mesmo usuário pode lê-lo.

**Impacto:** Comprometimento da API local se outro processo do mesmo usuário for malicioso.

**Mitigação:** API somente loopback; sem exposição externa.

**Próxima ação:** (Opcional) Mover para chave derivada com reentrega de segredo ao usuário; avaliado contra nesta fase.

---

## TD-008 — ApprovalManager usa síncrono SQLite em contexto assíncrono (P1) — Open

**Descrição:** `decide()`, `claim_approval()`, etc. fazem chamadas síncronas a SQLite a partir de código assíncrono (AutomationEngine).

**Impacto:** Pode bloquear o event loop sob carga.

**Próxima ação:** Envolver chamadas em `tokio::task::spawn_blocking` ou migrar para assíncrono.

---

## TD-009 — Sandbox roda como uid 0 dentro do bubblewrap (P2) — Open

**Descrição:** `bwrap` executa com `--uid 0 --gid 0` dentro do namespace. Um escape de namespace daria root no host.

**Impacto:** Isolamento mais fraco que UID não-privilegiado.

**Próxima ação:** Usar UID/GID não-privilegiado dentro do sandbox.

---

## TD-010 — "zeus" prefixo em módulos Python (P0) — Open (relacionado ao TD-001)

**Descrição:** Muitos módulos/loggers usam prefixo `zeus` em vez de `nexus`.

**Impacto:** Inconsistência de nomes.

**Próxima action:** Renomear sistematicamente para `nexus`.

---

## TD-011 — SudoBroker / Rollback Manager / Execution Journal (P1) — RESOLVIDO

**Descrição:** Mecanismos de sudo broker, rollback e execution journal separados do event store não existiam.

**Resolução (Fase 3):** Implementados `SudoBroker` (`sudo_broker.py`), `RollbackManager` (`rollback_manager.py`) e `ExecutionJournal` (`execution_journal.py`), todos com testes. Rollback não é assumido para ações externas irreversíveis.

**Impacto:** Operações irreversíveis exigem validação pré-execução mais forte; journal de execução dedicado é imutável e auditável.

---

## TD-012 — Sem multi-agente, simulação universal, GTK4 GUI (P1) — PARCIALMENTE RESOLVIDO

**Descrição:** Inteligence Plane (agentes) e Simulation Plane não existiam.

**Resolução (Fase 4 e Fase 5):** Multi-agent intelligence implementado (Agent Registry, Manifest, Runtime, Orchestrator, Dispatcher, Collaboration, Adversarial Review, Lifecycle). Universal simulation implementado (simulation types, clock, state manager, rules engine, event scheduler, world builder, scenario manager, metrics, analyzer, results store).

**Ainda em aberto:** GTK4 Desktop GUI (Command Center, Agent Control, Simulation Lab, Memory Explorer, Security Center).

**Próxima ação:** Construir GUI GTK4/libadwaita na Fase 6.

---

## TD-013 — Sinais de SO não tratados no daemon Rust (P2) — Open

**Descrição:** `nexus-daemon` não trata SIGTERM/SIGINT via `tokio::signal` (feature `unix` indisponível no ambiente de build).

**Impacto:** Encerramento apenas por kill do processo; sem shutdown gracioso via sinal.

**Próxima ação:** Revisar quando o tokio `unix` feature estiver disponível.

---

## TD-014 — Nenhum modelo de fallback sem LLM (P1) — Open

**Descrição:** O loop cognitivo falha se o provedor LLM (Ollama) estiver indisponível. Não há planejamento determinístico sem LLM.

**Impacto:** Indisponibilidade total de tarefas cognitivas sem LLM.

**Próxima ação:** Adicionar modelo leve local ou planejador determinístico como fallback.

---

## Resumo por prioridade

| Prioridade | Itens |
|------------|-------|
| P0 | TD-001, TD-002, TD-010 |
| P1 | TD-007, TD-008, TD-012, TD-014, TD-015, TD-016 |
| P2 | TD-003, TD-005, TD-009, TD-013, TD-017 |
| P3 | TD-004 |
| P4 | TD-006 |

---

## TD-015 — Integração do Execution Gate no Automation Engine (P1) — Open

**Descrição:** O `Automation Engine` existente ainda aplica policy e aprovação diretamente, sem passar pelo `Execution Gate` centralizado criado na Fase 3.

**Impacto:** A autorização centralizada (identidade → capacidade → política → risco → aprovação → recurso → execução isolada → verificação → commit → auditoria) não é acionada automaticamente por todas as execuções de ferramenta.

**Próxima ação:** Adaptar o `AutomationEngine.run()` para consultar o `ExecutionGate` antes de executar, mantendo compatibilidade com o `ApprovalManager` existente.

---

## TD-016 — Parametrizar autonomy_level no Execution Gate (P1) — Open

**Descrição:** O `Execution Gate` da Fase 3 usa `autonomy_level=1` fixo na avaliação de política do `PolicyEngine`.

**Impacto:** A decisão de política não reflete o nível de autonomia real do componente solicitante.

**Próxima ação:** Aceitar `autonomy_level` como parâmetro da autorização e repassá ao `PolicyContext`.

---

## TD-017 — Event Fabric completo e rede remota entre nós (P2) — Open

**Descrição:** O Event Fabric atual opera em processo (EventBus + EventStore SQLite). A comunicação remota entre nós do cluster distribuído ainda usa IPC local/UDS; não há rede remota (TCP/WebSocket) nem transactional outbox nem dead-letter queue.

**Impacto:** O cluster distribuído está limitado a um único nó físico; não há replicação de eventos entre nós nem entrega garantida com outbox transacional.

**Próxima ação:** Estender o IPC para rede remota (WebSocket/TCP), implementar transactional outbox para eventos críticos e dead-letter handler.
