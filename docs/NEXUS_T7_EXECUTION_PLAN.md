# NEXUS T7 — Host principal e rede segura

**Versão:** 1.0
**Atualizado:** 2026-11-20
**Depende de:** T6 (concluído em 2026-11-16)
**Bloqueia:** T8 (Identity & Permission Manager)
**Fonte da verdade:** `ROADMAP.md` + diagrama de topologia alvo

---

## 0. Resultado esperado

Ao final do T7:

* o **Acer Nitro V15** é o host principal, com `nexus-daemon`, `nexus-core` e
  `nexus-sentinel` supervisionados pelo systemd e subindo no boot;
* GPU/Ollama operacionais, com o catálogo de modelos alinhado ao hardware;
* acesso remoto apenas por rede privada criptografada, sem portas abertas na LAN;
* o **Execution Gate autoritativo do Rust (T6)** validado no host real, incluindo
  fail-closed e recuperação de execuções órfãs;
* backup baseline criptografado rodando diariamente, com **restore testado** e
  RPO/RTO iniciais documentados.

**Fora de escopo do T7:** pairing de dispositivos e papéis (T8), exposição da API
com autenticação de dispositivo (T9), mesh de LLMs (T15), DR completo com
snapshots de identidade/memória (T19). O T7 entrega a base sobre a qual esses
itens serão construídos.

---

## 1. Decisões a tomar antes de provisionar (T7.0)

Registrar em **ADR 0022 — Main Host, Network and Backup Baseline**, seguindo o
formato de `docs/adr/`.

| # | Decisão | Recomendação | Alternativa |
|---|---------|--------------|-------------|
| D1 | Rede privada remota | **Tailscale** (NAT-friendly, ACLs, MagicDNS) | WireGuard puro com VPS/porta fixa |
| D2 | TLS no T7 | Criptografia da tailnet + HTTPS obrigatório para provedor remoto | Reverse proxy (Caddy) já no T7 |
| D3 | Diretório de dados | **Manter `data/` relativo ao repositório** no baseline; avaliar `/srv/nexus-data` após inventariar e testar disco dedicado | Migrar para disco separado imediatamente |
| D4 | Ferramenta de backup | **restic** em disco externo ou destino de rede, com senha fora do checkout | borg / tar+SHA-256 em disco LUKS |
| D5 | Caminho de instalação | Manter `%h/Documentos/nexus` (as units já assumem esse caminho) | Padronizar `/opt/nexus` (exige editar as 3 units) |
| D6 | Orçamento de VRAM/modelos | **Um modelo por vez** (`OLLAMA_MAX_LOADED_MODELS=1`), contexto inicial 2048 até benchmark no Acer | Aumentar para 4096 tokens após medir memória e latência |

- [ ] **T7.0.1** Inventariar o host: CPU, GPU/VRAM, RAM, discos, versão do Linux
  Mint/Ubuntu, driver NVIDIA disponível. O inventário do ambiente desta sessão
  está registrado no runbook, mas é de um Dell Inspiron 15 3511 e não conta como
  evidência do Acer Nitro V15.
- [x] **T7.0.2** Definir D1–D6 e registrar no ADR 0022. As escolhas de caminho de
  dados e orçamento de contexto são conservadoras até o inventário do host alvo.
- [ ] **T7.0.3** Confirmar que o host será dedicado (sem uso de desktop que
  dispute GPU/RAM com o Ollama) ou definir limites de recurso.

**Aceite:** ADR 0022 mergeado; nenhuma decisão de topologia em aberto.

---

## 2. Provisionamento base (T7.1)

- [ ] **T7.1.1** SO atualizado; usuário `nexus` sem privilégios de root; SSH
  somente por chave; login de root desabilitado.
- [ ] **T7.1.2** Instalar dependências: `python3.12-venv`, `build-essential`,
  `bubblewrap`, `sqlite3`, `git`, `curl`, `jq`, `restic`, `ufw`; Rust via
  `rustup` na toolchain registrada.
- [ ] **T7.1.3** Clonar o repo no caminho decidido (D5), criar `.venv`,
  `pip install -e .`, compilar em release:
  `core/nexus-daemon` e `core-rust` (sentinel).
  **Preparação no Dell dev (2026-11-20):** pacote Python instalado em modo
  editável; os dois binários Rust release compilados. Repetir e validar no Acer.
- [ ] **T7.1.4** Criar `data/` e subdiretórios com `0700`; `data/secrets` com
  `0700`; gerar token inicial com `nexus token rotate`.
- [ ] **T7.1.5** **Habilitar linger** para o usuário (`loginctl enable-linger
  nexus`) — units de usuário sem linger só sobem no login, o que não serve para
  um servidor.
- [ ] **T7.1.6** Rodar a suíte no host definitivo e registrar os números:
  `pytest tests/` e `cargo test` no daemon. O último baseline completo conhecido
  é do T6 (2026-11-16): 516 testes Python + 12 subtests e 86 testes Rust; não
  representa uma execução da árvore atual.
- [ ] **T7.1.7** Validar `nexus status`, `nexus diagnostics run sys.uptime` e
  `nexus db backup`.

**Aceite:** suíte verde no host novo; `nexus status` reporta `ok`; linger
habilitado; token e diretórios privados com as permissões corretas.

---

## 3. Daemon supervisionado pelo systemd (T7.2) — validação no host pendente

**Status no workspace (2026-11-20):** a unit `nexus-daemon`, a dependência
soft do core, o shutdown gracioso e a cobertura de configuração/socket path
estão presentes. A instalação, permissões reais e comportamento do systemd
continuam pendentes no Acer Nitro V15.

- [x] **T7.2.1** Criar `systemd/nexus-daemon.service` (user unit) com:
  `WorkingDirectory=%h/Documentos/nexus`,
  `Environment=NEXUS_CONFIG=%h/Documentos/nexus/config/nexus.toml`,
  `ExecStart=.../core/nexus-daemon/target/release/nexus-daemon`,
  `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=20`, e o mesmo
  endurecimento do core (`NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome=read-only`, `ReadWritePaths=%h/Documentos/nexus/data`,
  `RestrictAddressFamilies=AF_UNIX` — o daemon só fala UDS e SQLite local).
- [x] **T7.2.2** Ajustar a ordem das units: `nexus-core` com
  `After=nexus-daemon.service` e `Wants=nexus-daemon.service` (o core usa o
  daemon quando `[control_plane] enabled = true`). O `nexus-sentinel` continua
  apenas `After`/`Requires` do core: ele alimenta o core, não o daemon.
- [x] **T7.2.3** **Não** adicionar `[ipc]` ao `config/nexus.toml` compartilhado:
  o parser Python rejeita chaves de topo desconhecidas. O default do daemon
  (`data/run/nexus-daemon.sock`) deve permanecer igual ao
  `[control_plane] socket_path` do Python; há cobertura do valor default dos
  dois lados. A execução dos testes segue pendente no host alvo.
- [x] **T7.2.4** Fechar o **TD-013**: tratar SIGTERM/SIGINT no daemon para
  emitir `system.stopped`, parar serviços supervisionados e sair com código 0.
  `tokio` com feature `full` já inclui `signal`; o build existe no workspace,
  mas a validação systemd ainda está pendente no host.
- [ ] **T7.2.5** Validar permissões do socket (`0600`, dono `nexus`) e recusa de
  conexão por outro UID (ex.: `sudo -u nobody`).
- [ ] **T7.2.6** `systemd-analyze --user verify` limpo para as três units.

**Aceite:**
`systemctl --user is-active` = `active` e `is-enabled` = `enabled` para
`nexus-daemon`, `nexus-core` e `nexus-sentinel`; matar o daemon faz o systemd
reiniciar; `systemctl --user stop nexus-daemon` registra `system.stopped`;
socket `0600`; outro UID recebe `permission denied`.

---

## 4. GPU e Ollama (T7.3)

- [ ] **T7.3.1** Driver NVIDIA proprietário + CUDA instalados; `nvidia-smi`
  funcional; Ollama usando GPU (`ollama ps` mostra a camada GPU).
- [ ] **T7.3.2** Definir o catálogo do host (ex.: um 8B quantizado como principal
  e um 3B como fallback), puxar os modelos e alinhar `[[llm.providers]]` /
  `[[llm.chain]]` ao que está realmente instalado (TD-018).
- [ ] **T7.3.3** Configurar Ollama apenas em loopback (`OLLAMA_HOST=127.0.0.1`),
  `OLLAMA_KEEP_ALIVE` e `OLLAMA_MAX_LOADED_MODELS=1` para não estourar VRAM.
- [ ] **T7.3.4** Ajustar `[llm]` ao hardware novo (budget de contexto, timeout,
  retries) sem afrouxar a política de egress.
- [ ] **T7.3.5** Benchmark: tokens/s e latência p95 para 16 e 256 tokens; registrar
  em `PERFORMANCE_BASELINE.md`.

**Aceite:** `nexus llm models` e `nexus llm complete` passam; Ollama escuta só em
`127.0.0.1:11434`; benchmark registrado; nenhum provedor remoto habilitado sem
`authorized = true` + HTTPS.

---

## 5. Rede privada, TLS e firewall (T7.4)

- [ ] **T7.4.1** Instalar Tailscale (ou WireGuard) e autenticar o host; ACLs
  restringindo o acesso aos dispositivos do owner.
- [ ] **T7.4.2** Documentar a decisão de TLS do T7: a criptografia da tailnet cobre
  o acesso administrativo; a API continua em `127.0.0.1:8765` e o HTTPS público
  com autenticação de dispositivo é **T9**. Nenhum proxy público é criado agora.
- [ ] **T7.4.3** Firewall `ufw`: `default deny incoming`; permitir SSH e tráfego
  apenas em `tailscale0`; **não** abrir 8765 nem 11434 na LAN.
- [ ] **T7.4.4** Verificar com `ss -lntp` que 8765 e 11434 escutam apenas em
  loopback; validar por scan a partir de outro dispositivo da LAN.

**Aceite:** host acessível via tailnet; portas 8765/11434 invisíveis na LAN;
`ufw status verbose` coerente; decisão de TLS registrada no ADR 0022.

---

## 6. Backup baseline com restore testado (T7.5)

O backup do T7 é **baseline**, não o DR completo do T19. Ele precisa cobrir os
dados que já existem e provar restore.

**Status no workspace (2026-11-20):** `scripts/backup-baseline.sh`,
`config/backup.env.example` e as units `nexus-backup.service`/`nexus-backup.timer`
criados. O arquivo `config/backup.env` não existe e o repositório restic não foi
configurado; faltam execução e restore drill no host alvo.

- [x] **T7.5.1** Criar `scripts/backup-baseline.sh` cobrindo:
  `nexus db backup` (event store), `.backup` SQLite de
  `data/databases/nexus_events.db` e `nexus_approvals.db`, `config/`,
  `data/tasks/`, `data/memory/`, `data/vectors/` e `data/secrets/`
  (criptografado pelo restic).
- [ ] **T7.5.2** Inicializar repositório restic em disco externo/rede, com senha
  em local seguro; retenção inicial 7 diários + 4 semanais.
- [x] **T7.5.3** Criar o serviço e timer diário; falhas geram entrada no
  journal do systemd e evento NEXUS. Instalação e ativação do timer no host
  continuam pendentes.
- [ ] **T7.5.4** **Restore drill**: restaurar em diretório temporário, rodar
  `PRAGMA integrity_check` nos SQLite, `nexus events verify`, subir o core com o
  data dir restaurado e validar `nexus status` + aprovações históricas.
- [ ] **T7.5.5** Rotacionar o token da API após o drill (o segredo antigo esteve
  no backup) e registrar o procedimento.
- [ ] **T7.5.6** Definir RPO/RTO iniciais (sugestão: RPO 24 h, RTO 1 h) e medir o
  RTO real no drill.

**Aceite:** backup diário rodando; `restic check` limpo; restore completo
executado e cronometrado; RPO/RTO documentados; runbook de restore escrito.

---

## 7. Validação operacional do T6 no host (T7.6)

Só habilitar `[control_plane] enabled = true` depois de T7.2–T7.5 verdes.

- [ ] **T7.6.1** Rodar a suíte completa no host definitivo e anexar a saída.
- [ ] **T7.6.2** Habilitar o control plane, reiniciar as units e validar o ciclo
  real: solicitação → `REQUIRES_APPROVAL` com `approval_id` do daemon → decisão →
  `CLAIMED → CONSUMED` → execução → `ExecutionComplete`.
- [ ] **T7.6.3** Reiniciar o daemon e confirmar que aprovações/histórico
  persistem em `data/databases/nexus_approvals.db`.
- [ ] **T7.6.4** Derrubar o daemon e confirmar **fail-closed**: execução negada e
  `health().control_plane.status = "unavailable"`.
- [ ] **T7.6.5** Matar uma execução em andamento, reiniciar e confirmar a
  recuperação de órfãos (`STARTING`/`RUNNING` → `UNKNOWN`).
- [ ] **T7.6.6** Soak de 72 h: heartbeats regulares, RSS estável, logs
  rotacionando, dead-letter sem crescimento anormal.

**Aceite:** checklist do T6 assinado no host real; nenhum caminho de execução
sem gate; modo local permanece documentado como fallback explícito.

---

## 8. Documentação e encerramento (T7.7)

- [x] **T7.7.1** Registrar no ADR 0022 as decisões baseline de host, rede, TLS,
  backup e autoridade. Inventário e validação operacional do Acer continuam
  pendentes no T7.0/T7.2–T7.6.
- [ ] **T7.7.2** Runbook operacional: start/stop, rotação de token, restore,
  diagnóstico de daemon indisponível.
- [ ] **T7.7.3** Registrar evidências em `docs/runbooks/T7_VALIDATION.md`
  (comando + saída + data). **Template criado em 2026-11-16**; preencher no host.
- [ ] **T7.7.4** Atualizar `ROADMAP.md` (T7 concluído) e a seção de topologia do
  `NEXUS_MASTER_ARCHITECTURE.md`.

**Aceite:** outro operador consegue subir e restaurar o host seguindo apenas o
runbook.

---

## 9. Definition of Done (T7)

- [ ] Três serviços `active`/`enabled`, subindo no boot, com linger.
- [ ] Daemon com shutdown gracioso e socket `0600` validado.
- [ ] GPU/Ollama operacionais e catálogo alinhado à config.
- [ ] Nenhuma porta do NEXUS exposta na LAN; acesso remoto só pela tailnet.
- [ ] Backup diário criptografado + restore drill cronometrado.
- [ ] T6 autoritativo validado com fail-closed, persistência e recuperação de
  órfãos.
- [ ] Soak de 72 h sem incidente crítico.
- [ ] ADR 0022, runbook e evidências mergeados; ROADMAP atualizado.

---

## 10. Riscos e mitigação

| Risco | Impacto | Mitigação |
|-------|---------|-----------|
| Unit do daemon inexistente | Daemon sem supervisão no boot | T7.2.1 antes de qualquer teste de autoridade |
| TD-013 (sem sinais) | Shutdown sujo, `system.stopped` ausente | T7.2.4; se o build bloquear, documentar e usar `KillSignal` |
| Drift do socket path (Python `[control_plane]` vs Rust `[ipc]`) | Core não encontra o daemon | Teste de paridade em T7.2.3; não duplicar seção no TOML |
| Units fixam `Documentos/nexus` | Serviço não sobe se o path mudar | Decidir D5 e validar `systemd-analyze` |
| VRAM insuficiente para o modelo escolhido | Ollama em CPU, latência alta | Budget D6, um modelo por vez, fallback 3B |
| Backup sem restore testado | Falsa sensação de segurança | T7.5.4 obrigatório antes de fechar o T7 |
| Habilitar control plane cedo | Execuções negadas ou bypass | Gate de T7.6 após T7.2–T7.5 |
| Token exposto no backup | Comprometimento da API local | restic criptografado + rotação pós-restore (T7.5.5) |

---

## 11. Ordem de execução resumida

```
T7.0 decisões
  └─ T7.1 provisionamento base
       ├─ T7.2 daemon sob systemd + TD-013
       ├─ T7.3 GPU/Ollama
       └─ T7.4 rede privada/firewall
            └─ T7.5 backup baseline + restore drill
                 └─ T7.6 validação do T6 autoritativo
                      └─ T7.7 documentação + soak
```

T7.2, T7.3 e T7.4 podem avançar em paralelo depois do T7.1; T7.5 depende de T7.1
e T7.3; T7.6 é o portão final e não pode ser antecipado.
