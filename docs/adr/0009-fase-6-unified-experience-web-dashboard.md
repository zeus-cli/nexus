# ADR 0009 — Unified Experience: Web Dashboard aprimorado (Fase 6)

> **Nota de atualização (2026-11-12):** decisão histórica preservada; as contagens de testes referem-se à data original. Estado atual e validação consolidados em `docs/adr/0014-endurecimento-auditoria-2026-11-12.md`.

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/web/index.html`, `nexus_core/web/app.js`, `nexus_core/web/app.css`, `nexus_core/api/app.py`, `nexus_core/app.py`
- **Depende de:** ADR 0001–0008

---

## Contexto

A especificação mestre (seção 15) exige uma experiência unificada com:

* Desktop GUI (GTK4 — primária)
* TUI
* Web HUD
* CLI
* Internal API

Sessões sugeridas para o Command Center: estado cognitivo, agentes ativos, tarefas ativas, ambientes de simulação, estatísticas de memória, disponibilidade de modelos, CPU/RAM/GPU, saúde do serviço, aprovações pendentes, eventos de segurança.

GTK4 não está disponível neste ambiente (nem runtime nem headers de desenvolvimento, conforme ADR 0001). A interface nativa GTK4 está adiada e requer instalação de pacotes de sistema (autorização humana). Como alternativa imediata, o Web Dashboard existente foi aprimorado com as seções da Fase 6.

---

## Decisão 1 — Web Dashboard aprimorado (não GTK4)

Como o GTK4 não está disponível, o Web Dashboard (single-page, servido em `127.0.0.1`) foi expandido com as seções da Fase 6:

| Seção | Painel | Endpoints |
|-------|--------|-----------|
| Command Center | Visão geral (já existente) | `/api/v1/status`, `/api/v1/events` |
| Agent Control | Lista de agentes, papéis, estados | `/api/v1/agents`, `/api/v1/agents/{id}` |
| Simulation Lab | Mundos, cenários | `/api/v1/simulations/worlds`, `/api/v1/simulations/scenarios` |
| Security Center | Aprovações pendentes, eventos de segurança | `/api/v1/events` (filtrado por `security.*`) |

Novos endpoints adicionados à API:
* `GET /api/v1/agents` — lista agentes com manifesto e estado
* `GET /api/v1/agents/{agent_id}` — detalhes de um agente
* `GET /api/v1/simulations/worlds` — lista mundos de simulação
* `POST /api/v1/simulations/worlds` — cria um mundo
* `GET /api/v1/simulations/scenarios` — lista cenários
* `POST /api/v1/simulations/scenarios` — cria um cenário

---

## Decisão 2 — Integração de componentes no runtime

O `NexusRuntime` ganhou campos opcionais para `agent_registry`, `agent_runtime`, `world_registry`, `scenario_manager`, `experiment_runner`, instanciados no bootstrap com agentes de exemplo (Orchestrator, Researcher, Analyst). Isso permite que o Web Dashboard exiba dados reais sem modificar os componentes existentes.

---

## Decisão 3 — TUI e CLI existentes mantidos

O TUI (curses, somente-leitura) e CLI já existem e continuam funcionando. Não foram modificados nesta fase.

---

## Decisão 4 — GTK4 GUI adiada

O GTK4 Desktop GUI requer instalação de `libgtk-4-dev` e `gir1.2-gtk-4.0` (ação privilegiada, requer autorização humana). Não foi instalado neste ambiente. Quando disponível, a GUI nativa será construída sobre os mesmos endpoints da API.

---

## Consequências

* **Positivo:** Web Dashboard agora cobre Command Center, Agent Control, Simulation Lab e Security Center; novos endpoints de API; regressão zero (230 testes passando).
* **Negativo:** Sem GTK4 GUI nativa neste ambiente; o Web Dashboard é a interface desktop disponível.
* **Risco conhecido:** Os endpoints de agentes/simulações usam instâncias criadas no bootstrap; persistência desses componentes (em banco de dados) fica para fases futuras.

---

## Testes

* `tests/test_api.py`: 26 testes passando (incluindo regressão dos endpoints existentes).
* Regressão completa: 230 testes passando.
