# NEXUS Technical Debt

**Versão:** 1.2
**Projeto:** NEXUS — Cognitive Operating System
**Atualizado:** 2026-11-13

Registro de dívida técnica conhecida, com prioridade, estado e próxima ação. Mantido como fonte da verdade para planejamento de refatoração.

---

## TD-001 — Branding "zeus" vs "nexus" (P0) — Open

**Descrição:** O logger raiz é `"zeus"`, o banco de fallback é `zeus_events.db` e alguns comentários referenciam "zeus", criando confusão em logs, nomes de banco e imports. O projeto é "NEXUS".

**Impacto:** Confusão operacional; logs e nomes de banco inconsistentes com a marca.

**Próxima ação:** Renomear logger para `"nexus"` e banco fallback para `nexus_events.db`; auditoria de strings "zeus" no código.

---

## TD-002 — Systemd units (P0) — RESOLVIDO (2026-11-12)

**Descrição:** `systemd/nexus-core.service` e `systemd/nexus-sentinel.service` existem e foram implantadas/ativadas como units de usuário.

**Resolução:** `systemctl --user is-active` e `is-enabled` reportam `active`/`enabled` para ambos os serviços. O sistema inicia automaticamente e sobrevive a reinícios do usuário.

**Próxima ação:** manter a validação de `systemd-analyze --user verify` em CI ou checklist de release.

---

## TD-003 — Memória somente lexical (P2) — RESOLVIDO (2026-11-12)

**Descrição:** Busca por FTS5 lexical; vetor/semântico via LanceDB foi integrado ao caminho híbrido.

**Resolução:** `MemoryStore.search(..., hybrid=True)` combina busca lexical e vetorial com Reciprocal Rank Fusion. O `VectorStore` foi endurecido para compatibilidade com APIs antiga/nova do LanceDB, escape seguro de filtros e `upsert` idempotente (sem duplicatas).

**Próxima ação:** manter testes de regressão em `tests/test_vector.py` e `tests/test_hybrid_memory.py`.

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

## TD-006 — Hardware limitado para modelos locais (P4) — PARCIAL

**Descrição:** Host (i3, 11–12 GB RAM) não comporta confortavelmente modelos grandes (Gemma 4 12B/31B) localmente.

**Mitigação atual:** o Ollama local roda `nexusorbe-llama3.1:latest` (8B) e `nexusorbe-llama3.2:latest` (3B); modelos remotos maiores são usados via OpenCode em outro host. O NEXUS mantém a cadeia local por política de egress.

**Próxima ação:** benchmarkar em host com GPU/RAM maior; manter fallback leve local para indisponibilidade de LLM.

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

**Mitigação parcial (2026-11-12):** `nexus_core/sqlite_utils.py::connect_db` aplica `busy_timeout`, commit/rollback e fechamento garantido, reduzindo contenção e conexões vazadas.

**Próxima ação:** Envolver chamadas em `asyncio.to_thread`/`tokio::task::spawn_blocking` ou migrar para driver assíncrono.

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

## TD-014 — Nenhum modelo de fallback sem LLM (P1) — PARCIAL

**Descrição:** O loop cognitivo falha se todos os provedores LLM estiverem indisponíveis. A cadeia local tem fallback entre modelos, mas não há planejamento determinístico sem LLM.

**Impacto:** Indisponibilidade total de tarefas cognitivas se todo o chain falhar.

**Próxima ação:** Adicionar planejador determinístico como fallback final.

---

## Resumo por prioridade

| Prioridade | Itens |
|------------|-------|
| P0 | TD-001, TD-010 (abertos); TD-002 (resolvido) |
| P1 | TD-007 (aceito), TD-008, TD-012, TD-014, TD-020 |
| P2 | TD-003 (resolvido), TD-005, TD-009, TD-013, TD-017 (resolvido), TD-019, TD-021 |
| P3 | TD-004 |
| P4 | TD-006 (parcial) |

---

## TD-015 — Integração do Execution Gate no Automation Engine (P1) — RESOLVIDO (2026-11-13)

**Descrição:** O `Automation Engine` existente ainda aplica policy e aprovação diretamente, sem passar pelo `Execution Gate` centralizado criado na Fase 3.

**Resolução:** `bootstrap()` cria o `ExecutionGate` com o mesmo `PolicyEngine` e `ApprovalManager` do runtime e o injeta no `AutomationEngine`. `AutomationEngine.run()` chama `authorize()` antes de reivindicar/executar a aprovação e `commit()` registra o resultado real da execução. O caminho `gate.execute(runner=None)` foi removido; não há mais execução fantasma. Cobertura em `tests/test_e2e_execution_gate.py` e `tests/test_execution_gate.py`.

**Próxima ação:** substituir a autoridade Python pelo daemon Rust (T4–T6), mantendo a interface `authorize → executar → commit` e o fail-closed.

---

## TD-016 — Parametrizar autonomy_level no Execution Gate (P1) — RESOLVIDO (2026-11-13)

**Descrição:** O `Execution Gate` da Fase 3 usa `autonomy_level=1` fixo na avaliação de política do `PolicyEngine`.

**Resolução:** `authorize()` aceita `autonomy_level` e `required_autonomy`; o `AutomationEngine` repassa `config.safety.autonomy_level` e `tool.required_autonomy`. O classificador do gate usa a autonomia real e o escopo de escrita com a mesma semântica do `PolicyEngine` (LOW READ exige autonomia suficiente; LOW WRITE fora de `SAFE_ZONE` exige aprovação). Cobertura em `tests/test_execution_gate.py::test_gate_uses_real_autonomy_and_scope_for_low_risk`.

**Próxima ação:** manter a paridade de decisão quando a política migrar para o Rust (T5).

---

## TD-017 — Event Fabric completo e rede remota entre nós (P2) — RESOLVIDO (2026-11-12)

**Descrição:** O Event Fabric opera em processo (EventBus + EventStore SQLite), com transactional outbox, dead-letter queue e comunicação remota WebSocket entre nós.

**Resolução:** `event_fabric.py` implementa outbox transacional, retry com backoff e dead-letter; `distributed_network.py` implementa servidor/cliente WebSocket com heartbeat e propagação de eventos preservando `created_at`/dedup.

**Próxima ação:** monitorar tamanho da dead-letter queue e métricas de retry em operação distribuída.

---

## TD-018 — Configuração LLM desalinhada do Ollama local (P1) — RESOLVIDO (2026-11-12)

**Descrição:** `config/nexus.toml` referenciava `qwen2.5:7b` e `llama3.1:8b`, ausentes no Ollama local. Toda completion falhava com HTTP 404 antes de chegar ao fallback.

**Resolução:** a cadeia foi alinhada para `nexusorbe-llama3.1:latest` e `nexusorbe-llama3.2:latest`, presentes no host. `nexus llm complete` passou a responder com sucesso.

**Próxima ação:** validar a cadeia no bootstrap ou em `nexus llm models` para detectar divergências entre config e modelos instalados.

---

## TD-019 — Scheduler de background não integrado ao runtime (P2) — Open

**Descrição:** `nexus_core/scheduler.py` implementa tarefas periódicas/cron-like com Execution Gate, mas não está instanciado no runtime, não é exposto pela API e não tem testes dedicados.

**Impacto:** agendamento de background não está disponível na operação normal.

**Próxima ação:** integrar o Scheduler ao bootstrap/API, adicionar testes e documentar o ciclo de vida das tarefas agendadas.

---

## TD-020 — Criptografia do Task Store desativada por padrão (P2) — Open

**Descrição:** `TaskStore` suporta Fernet (com fallback XOR), mas `bootstrap()` cria a instância sem chave. O estado de tarefas suspensas fica em texto claro por padrão.

**Impacto:** prompts, parâmetros e estado de tarefas podem ser lidos por processos do mesmo usuário.

**Próxima ação:** derivar uma chave de `data/secrets/` ou de variável de ambiente e passá-la ao `TaskStore`; documentar migração de arquivos existentes.

---

## TD-021 — Limites do Event Fabric após o T2 (P2) — Open

**Descrição:** O T2 fechou a atomicidade `EventStore + outbox` e adicionou
consumidor idempotente, mas três fronteiras continuam deliberadamente abertas:

1. o outbox durável e os offsets/acks por assinante ainda não existem no daemon
   Rust — o `EventBus` é em memória e um cliente desconectado perde eventos;
2. `IdempotentConsumer` garante efeito at-most-once apenas dentro de um processo;
   múltiplos processos consumidores exigem claim transacional;
3. a bridge UDS não tem autenticação/TLS própria; qualquer processo do mesmo
   usuário pode publicar/assinar no socket.

**Impacto:** entrega confiável no runtime Python atual, mas ainda não há
durabilidade de assinatura no Control Plane nem isolamento entre processos
locais não confiáveis.

**Próxima ação:** implementar outbox/offsets no Rust e promover os contratos de
autorização a produção no T4; introduzir claim transacional de consumidor e
identidade de componente no IPC antes de mover o gate para o Control Plane
(T5–T6). Não expor o socket remotamente até lá.
