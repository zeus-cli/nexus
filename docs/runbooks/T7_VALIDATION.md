# NEXUS T7 — Runbook de Validação no Host

**Versão:** 1.1
**Atualizado:** 2026-11-20
**Host alvo:** Acer Nitro V15 (`nexus` como usuário de serviço)
**Plano:** `docs/NEXUS_T7_EXECUTION_PLAN.md`

Este runbook executa e comprova o T7 no host real. Cada fase tem comandos,
resultado esperado e um espaço de evidência. **Não feche o T7 sem o restore
drill (Fase G) e o teste fail-closed (Fase H).**

Convenções:

```bash
export NEXUS_ROOT="$HOME/Documentos/nexus"
export NEXUS_PY="$NEXUS_ROOT/.venv/bin/python"
cd "$NEXUS_ROOT"
```

Registre no início:

```bash
date -Is
hostnamectl --static
git rev-parse HEAD
```

| Evidência | Valor |
|---|---|
| Data/hora | |
| Hostname | |
| Commit | |

### Inventário preliminar desta sessão — 2026-11-20

Este levantamento foi feito na estação Dell de desenvolvimento e **não** é
evidência do Acer Nitro V15, que continua sendo o servidor alvo. A tarefa
T7.0.1 permanece aberta até o inventário ser repetido no Acer.

| Item | Ambiente desta sessão |
|---|---|
| Equipamento | Dell Inspiron 15 3511 |
| Sistema | Linux Mint 22.3, kernel 7.0.0-43-generic |
| CPU | Intel Core i3-1115G4, 2 núcleos / 4 threads |
| Memória | 11 GiB RAM; swap de 2 GiB |
| GPU | Intel UHD Graphics G4; sem dispositivo NVIDIA listado |
| Disco | NVMe único de 238,5 GB; sem volume de dados separado identificado |

Não instalar dependências, configurar firewall/rede nem ativar serviços neste
ambiente como substituto do host alvo. As fases seguintes devem registrar a
saída observada no Acer Nitro V15.

---

## Fase A — Inventário e pré-requisitos

```bash
lsb_release -a
uname -r
lscpu | sed -n '1,25p'
free -h
lsblk -f
df -h / /home /mnt
nvidia-smi || echo "driver NVIDIA ausente"
```

- [ ] GPU/VRAM identificadas e compatíveis com o modelo escolhido (D6).
- [ ] Disco de dados e disco de backup identificados (D3).
- [ ] Host dedicado ou limites de recurso definidos.

**Evidência:** colar a saída resumida (CPU, RAM, GPU, discos).

---

## Fase B — Dependências e build

```bash
sudo apt update
sudo apt install -y python3.12-venv build-essential bubblewrap sqlite3 \
  git curl jq restic ufw

# Rust (se ainda não instalado)
command -v cargo || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

python3 -m venv .venv
.venv/bin/pip install -e .
cargo build --release --manifest-path core/nexus-daemon/Cargo.toml
cargo build --release --manifest-path core-rust/Cargo.toml

ls -l core/nexus-daemon/target/release/nexus-daemon \
      core-rust/target/release/nexus-sentinel \
      .venv/bin/python
```

- [ ] Binários release existem e são executáveis.
- [ ] `.venv/bin/python -m nexus_core status` responde (mesmo sem as units).

**Evidência:** versões de Python/Rust/restic e caminho dos binários.

---

## Fase C — Units, linger e boot

```bash
mkdir -p ~/.config/systemd/user
cp systemd/nexus-daemon.service \
   systemd/nexus-core.service \
   systemd/nexus-sentinel.service \
   systemd/nexus-backup.service \
   systemd/nexus-backup.timer \
   ~/.config/systemd/user/

systemctl --user daemon-reload
systemd-analyze --user verify

# Sem linger, units de usuário só sobem no login.
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger    # esperado: Linger=yes

systemctl --user enable --now nexus-daemon.service
systemctl --user enable --now nexus-core.service
systemctl --user enable --now nexus-sentinel.service
systemctl --user enable --now nexus-backup.timer

systemctl --user list-units 'nexus*' --all
systemctl --user is-active nexus-daemon nexus-core nexus-sentinel
systemctl --user is-enabled nexus-daemon nexus-core nexus-sentinel
```

- [ ] Três serviços `active` e `enabled`.
- [ ] `Linger=yes`.
- [ ] `systemd-analyze --user verify` sem erros.
- [ ] Após `sudo reboot`, os três voltam sozinhos e
  `$NEXUS_PY -m nexus_core status` responde.

**Evidência:** saída de `is-active`/`is-enabled` antes e depois do reboot.

---

## Fase D — Socket, permissões e shutdown gracioso

```bash
stat -c '%a %U:%G %n' data/run/nexus-daemon.sock
# esperado: 600 nexus nexus

ss -xl | grep nexus-daemon
```

Teste de encerramento gracioso:

```bash
systemctl --user stop nexus-daemon.service
journalctl --user -u nexus-daemon -n 30 --no-pager

$NEXUS_PY - <<'PY'
import sqlite3
conn = sqlite3.connect("data/databases/nexus_daemon_events.db")
rows = conn.execute("SELECT kind FROM events ORDER BY rowid DESC LIMIT 5").fetchall()
print([row[0] for row in rows])
conn.close()
PY
# esperado conter system.stopped

systemctl --user start nexus-daemon.service
```

Teste de restart automático:

```bash
systemctl --user kill --signal=KILL nexus-daemon.service
sleep 6
systemctl --user is-active nexus-daemon.service   # esperado: active
```

- [ ] Socket `0600`, dono `nexus`.
- [ ] `stop` produz `system.stopped` e sai com código 0.
- [ ] `SIGKILL` faz o systemd reiniciar o daemon.
- [ ] Outro UID não conecta (a recusa por `SO_PEERCRED` é coberta por
  `cargo test ipc::server::tests::peer_uid_mismatch_is_rejected_before_handshake`).

**Evidência:** `stat`, últimas linhas do journal e lista de eventos.

---

## Fase E — GPU, Ollama e catálogo de modelos

```bash
nvidia-smi
ollama --version
systemctl status ollama --no-pager
ollama list
ollama ps
curl -s http://127.0.0.1:11434/api/tags | jq -r '.models[].name'

$NEXUS_PY -m nexus_core llm models
time $NEXUS_PY -m nexus_core llm complete \
  --prompt "Responda apenas: ok" --max-tokens 16
```

Se o Ollama precisar de ajuste de VRAM/loopback:

```bash
sudo systemctl edit ollama
# [Service]
# Environment="OLLAMA_HOST=127.0.0.1:11434"
# Environment="OLLAMA_MAX_LOADED_MODELS=1"
# Environment="OLLAMA_KEEP_ALIVE=10m"
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

- [ ] `ollama ps` mostra GPU (não 100% CPU).
- [ ] Catálogo em `config/nexus.toml` bate com `ollama list` (TD-018).
- [ ] Completion local responde; latência registrada.
- [ ] Ollama escuta apenas em `127.0.0.1:11434`.

**Evidência:** `nvidia-smi`, `ollama ps`, tempo da completion.

---

## Fase F — Rede privada, TLS e firewall

```bash
# Tailscale (ou WireGuard, conforme ADR 0022)
sudo tailscale up --ssh
tailscale status

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
# Só habilite SSH na LAN se realmente precisar:
# sudo ufw allow ssh
sudo ufw --force enable
sudo ufw status verbose

ss -lntp | grep -E ':(8765|11434)\b'
```

De outro dispositivo da LAN:

```bash
nmap -Pn -p 8765,11434 <IP_LAN_DO_ACER>
# esperado: closed/filtered
# sem nmap: nc -zv <IP_LAN_DO_ACER> 8765 ; nc -zv <IP_LAN_DO_ACER> 11434
```

- [ ] Acesso administrativo funciona pela tailnet.
- [ ] 8765 e 11434 invisíveis na LAN.
- [ ] TLS do T7 = criptografia da tailnet; **não** há proxy público (T9).
- [ ] Decisão registrada no ADR 0022.

**Evidência:** `tailscale status`, `ufw status verbose`, saída do scan.

---

## Fase G — Backup baseline e restore drill

### G.1 Configurar o repositório

Monte o disco externo (de preferência LUKS) em `/mnt/nexus-backup` antes de
continuar; o repositório **não** deve ficar em `/home` por causa do
`ProtectHome=read-only` da unit.

```bash
sudo mkdir -p /mnt/nexus-backup
sudo chown "$USER":"$USER" /mnt/nexus-backup

install -d -m 700 "$HOME/.config/nexus"
openssl rand -base64 32 > "$HOME/.config/nexus/restic-password"
chmod 600 "$HOME/.config/nexus/restic-password"

cp config/backup.env.example config/backup.env
chmod 600 config/backup.env
# editar NEXUS_BACKUP_REPO e NEXUS_BACKUP_PASSWORD_FILE

scripts/backup-baseline.sh
```

Verificação:

```bash
set -a; source config/backup.env; set +a
export RESTIC_REPOSITORY="$NEXUS_BACKUP_REPO"
export RESTIC_PASSWORD_FILE="$NEXUS_BACKUP_PASSWORD_FILE"
restic snapshots
restic check
```

- [ ] Backup manual concluído e `restic check` limpo.
- [ ] `nexus-backup.service` roda via timer:
  `systemctl --user start nexus-backup.service` + journal sem erro.
- [ ] Retenção 7 diários / 4 semanais configurada.

### G.2 Restore drill (obrigatório)

```bash
RESTORE=$(mktemp -d /tmp/nexus-restore.XXXXXX)
set -a; source config/backup.env; set +a
RESTIC_REPOSITORY="$NEXUS_BACKUP_REPO" \
RESTIC_PASSWORD_FILE="$NEXUS_BACKUP_PASSWORD_FILE" \
  restic restore latest --target "$RESTORE"

SNAP=$(find "$RESTORE" -type d -name 'nexus-backup.*' | head -1)
echo "$SNAP"
mkdir -p "$SNAP/data"
cp -a "$SNAP/databases" "$SNAP/data/"
for d in tasks memory vectors secrets; do
  [ -d "$SNAP/state/$d" ] && cp -a "$SNAP/state/$d" "$SNAP/data/"
done

$NEXUS_PY - "$SNAP/data/databases" <<'PY'
import pathlib
import sqlite3
import sys

for db in sorted(pathlib.Path(sys.argv[1]).glob("*.db")):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        print(db.name, conn.execute("PRAGMA integrity_check;").fetchone()[0])
    finally:
        conn.close()
PY

# A configuração restaurada resolve data_dir relativo ao snapshot.
time $NEXUS_PY --config "$SNAP/config/nexus.toml" events verify
time $NEXUS_PY --config "$SNAP/config/nexus.toml" status --json | jq '.status'
```

- [ ] `PRAGMA integrity_check` = `ok` em todos os bancos.
- [ ] `events verify` passa no snapshot.
- [ ] `status` do snapshot responde.
- [ ] RTO medido (tempo entre `restic restore` e status ok).
- [ ] Token da API rotacionado depois do drill
  (`$NEXUS_PY -m nexus_core token rotate`).
- [ ] RPO/RTO registrados (baseline sugerido: RPO 24 h, RTO 1 h).

**Evidência:** ID do snapshot, saída do integrity check, tempo do restore.

---

## Fase H — Control Plane autoritativo (T6 no host)

Só execute esta fase com G concluída.

### H.1 Habilitar

```bash
cp config/nexus.toml /tmp/nexus.toml.bak

$NEXUS_PY - <<'PY'
from pathlib import Path

path = Path("config/nexus.toml")
text = path.read_text()
head, separator, tail = text.partition("[control_plane]")
assert separator, "seção [control_plane] ausente"
path.write_text(head + separator + tail.replace("enabled = false", "enabled = true", 1))
PY

systemctl --user restart nexus-core.service
$NEXUS_PY -m nexus_core status --json | jq '.control_plane'
# esperado: enabled=true, authoritative=true, status="ok"
```

### H.2 Caminho feliz (execução via daemon)

```bash
$NEXUS_PY -m nexus_core diagnostics run sys.uptime
# esperado: saída do uptime; autorização via IPC do daemon
```

### H.3 Aprovação HITL e `CLAIMED → CONSUMED`

```bash
# Força aprovação numa ferramenta de leitura, reduzindo a autonomia.
cp config/nexus.toml /tmp/nexus-autonomy.toml.bak
$NEXUS_PY - <<'PY'
from pathlib import Path
path = Path("config/nexus.toml")
text = path.read_text()
path.write_text(text.replace("autonomy_level = 1", "autonomy_level = 0", 1))
PY
systemctl --user restart nexus-core.service

$NEXUS_PY -m nexus_core diagnostics run sys.uptime; echo "exit=$?"
# esperado: exit=3 e "aprovação necessária: <ID>"

$NEXUS_PY -m nexus_core approvals list
ID=<ID_DA_SAIDA_ANTERIOR>
$NEXUS_PY -m nexus_core approvals approve "$ID"
$NEXUS_PY -m nexus_core diagnostics run sys.uptime --approval-id "$ID"
# esperado: execução concluída, aprovação consumida

# Restaura a autonomia original (mantendo o control plane habilitado).
cp /tmp/nexus-autonomy.toml.bak config/nexus.toml
systemctl --user restart nexus-core.service
```

### H.4 Persistência e fail-closed

```bash
sqlite3 data/databases/nexus_approvals.db ".tables"
sqlite3 data/databases/nexus_approvals.db \
  "SELECT status, COUNT(*) FROM approvals GROUP BY status;"

systemctl --user restart nexus-daemon.service
sqlite3 data/databases/nexus_approvals.db \
  "SELECT status, COUNT(*) FROM approvals GROUP BY status;"
# esperado: histórico preservado

systemctl --user stop nexus-daemon.service
$NEXUS_PY -m nexus_core diagnostics run sys.uptime; echo "exit=$?"
# esperado: exit != 0 (fail-closed)

$NEXUS_PY -m nexus_core status --json | jq '.control_plane'
# esperado: status="unavailable"

systemctl --user start nexus-daemon.service
$NEXUS_PY -m nexus_core diagnostics run sys.uptime
# esperado: volta a funcionar
```

- [ ] `status.control_plane.status = "ok"` com o daemon no ar.
- [ ] Ferramenta executa via daemon.
- [ ] Aprovação pendente → approve → execução com `--approval-id`.
- [ ] Histórico sobrevive ao restart do daemon.
- [ ] Daemon parado = execução negada; `status = unavailable`; volta ao normal.
- [ ] Log de recuperação de órfãos observado após interrupção real
  (`execuções órfãs foram marcadas como UNKNOWN`), se aplicável.

**Evidência:** `status --json`, saída dos três comandos de diagnostics com
exit codes, contagem de aprovações antes/depois do restart.

---

## Fase I — Soak de 72 h

```bash
# Uma vez por dia, durante 3 dias:
journalctl --user -u nexus-daemon --since "24 hours ago" | grep -c 'system.heartbeat'
systemctl --user show -p MainPID --value nexus-daemon.service
ps -o rss=,etime= -p "$(systemctl --user show -p MainPID --value nexus-daemon.service)"
systemctl --user is-active nexus-daemon nexus-core nexus-sentinel
journalctl --user -u nexus-core -p warning --since "24 hours ago"
```

- [ ] Heartbeats regulares todos os dias.
- [ ] RSS estável (sem crescimento monotônico).
- [ ] Nenhum reinício inesperado.
- [ ] Sem erro crítico no journal.
- [ ] Logs rotacionando; disco com folga.

**Evidência:** três medições diárias (data, RSS, uptime, warnings).

---

## Rollback

| Situação | Ação |
|---|---|
| Control plane quebrou a execução | `[control_plane] enabled = false` → `systemctl --user restart nexus-core.service` |
| Daemon não sobe | `journalctl --user -u nexus-daemon -n 100`; conferir `nexus-daemon` release, config e permissões do socket |
| Unit falha no boot | `systemd-analyze --user verify`; conferir `Linger=yes` e caminhos `%h/Documentos/nexus` |
| Backup quebrado | `journalctl --user -u nexus-backup.service`; rodar `scripts/backup-baseline.sh` manualmente |
| Perda de dados | parar os três serviços; restaurar snapshot restic em `data/`; validar `integrity_check`; subir na ordem daemon → core → sentinel |
| GPU/driver instável | `nvidia-smi`; reverter driver; Ollama em CPU como fallback temporário |

---

## Sign-off do T7

| Item | OK | Evidência |
|---|---|---|
| Units ativas/enabled + linger + reboot | ☐ | |
| Socket `0600` + shutdown gracioso | ☐ | |
| GPU/Ollama + catálogo alinhado | ☐ | |
| Tailnet + firewall + portas fechadas | ☐ | |
| Backup diário + `restic check` | ☐ | |
| Restore drill + RTO/RPO | ☐ | |
| Control plane ok + HITL + persistência | ☐ | |
| Fail-closed do daemon | ☐ | |
| Soak 72 h | ☐ | |
| ADR 0022 + runbook + ROADMAP atualizados | ☐ | |

**Assinatura:** ______________________  **Data:** __________
