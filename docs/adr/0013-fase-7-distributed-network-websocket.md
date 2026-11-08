# ADR 0013 — Distributed Network: WebSocket entre nós (Fase 7+)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/distributed_network.py` — comunicação remota entre nós via WebSocket
- **Depende de:** ADR 0001–0012 (Node Manager, Event Fabric com outbox)

---

## Contexto

A especificação mestre (seção 17, perfil DISTRIBUTED) exige:

* Múltiplos nós computacionais
* Agendamento de carga distribuído
* Comunicação entre nós para propagação de eventos e estado

O `Node Manager` (ADR 0010) gerencia nós localmente, mas não há mecanismo de comunicação remota entre nós. Sem rede, os nós não podem compartilhar eventos, estado ou coordenar execução.

---

## Decisão 1 — WebSocket para comunicação entre nós

Criado `DistributedNetwork` com servidor e cliente WebSocket:

* **Servidor:** aceita conexões de outros nós, processa mensagens `hello`, `event`, `heartbeat`, `goodbye`.
* **Cliente:** conecta a nós remotos, propaga eventos via `broadcast_event`, mantém conexão ativa com heartbeat.
* **Handshake:** protocolo `hello`/`hello_ack` para identificação de nós.
* **Event propagation:** eventos são serializados como JSON e enviados para todos os nós conectados.
* **Reconexão:** o cliente detecta desconexão e limpa conexões mortas automaticamente.

Usamos a biblioteca `websockets` (já disponível no requirements.lock) para evitar dependências adicionais.

---

## Decisão 2 — Protocolo de mensagens JSON

Cada mensagem WebSocket é um objeto JSON com campo `type`:

| Tipo | Descrição |
|------|-----------|
| `hello` | Identificação do nó ao conectar |
| `hello_ack` | Confirmação de conexão |
| `event` | Propagação de evento do NEXUS |
| `event_ack` | Confirmação de recebimento de evento |
| `heartbeat` | Sinal de vida periódico |
| `heartbeat_ack` | Confirmação de heartbeat |
| `goodbye` | Desconexão ordenada |
| `error` | Mensagem de erro |

Os eventos do NEXUS são serializados usando o contrato existente (`events.Event.to_dict()`), garantindo compatibilidade com o EventStore e EventBus.

---

## Decisão 3 — Integração com o Node Manager

O `DistributedNetwork` é independente do `Node Manager` mas pode ser integrado:

* O `Node Manager` registra nós e seus endereços.
* O `DistributedNetwork` estabelece conexões WebSocket com esses endereços.
* Eventos recebidos de nós remotos são entregues ao `EventBus` local via `event_handler`.

Esta integração é deixada para o operador do sistema configurar conforme a topologia do cluster.

---

## Consequências

* **Positivo:** Nós podem se comunicar entre si para propagação de eventos e coordenação distribuída.
* **Negativo:** Não há criptografia TLS (ambiente local-first); autenticação entre nós é por ID de nó simples.
* **Risco conhecido:** Conexões WebSocket podem ser instáveis em redes com alta latência; reconexão automática mitiga parcialmente.

---

## Testes

* `tests/test_distributed_network.py`: 7 testes cobrindo servidor, cliente, handshake, propagação de evento e conexão entre dois nós.
* Regressão completa: 287 testes passando.
