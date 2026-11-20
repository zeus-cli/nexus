#!/usr/bin/env bash
# NEXUS — backup baseline criptografado (T7.5)
#
# Faz um snapshot consistente dos bancos SQLite, configuração e estado local,
# envia para um repositório restic e aplica retenção. Não substitui o DR
# completo do T19 (identidade, memória consolidada e pesos de modelos).
#
# Uso:
#   scripts/backup-baseline.sh
#   NEXUS_BACKUP_DRY_RUN=1 scripts/backup-baseline.sh   # só prepara o staging
#
# Configuração (config/backup.env, não versionado — veja backup.env.example):
#   NEXUS_BACKUP_REPO=/mnt/nexus-backup/restic
#   NEXUS_BACKUP_PASSWORD_FILE=/home/nexus/.config/nexus/restic-password
#   NEXUS_BACKUP_KEEP_DAILY=7
#   NEXUS_BACKUP_KEEP_WEEKLY=4

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_ROOT="${NEXUS_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
CONFIG_FILE="${NEXUS_CONFIG:-$NEXUS_ROOT/config/nexus.toml}"
DATA_DIR="${NEXUS_DATA_DIR:-$NEXUS_ROOT/data}"
PYTHON="${NEXUS_PYTHON:-$NEXUS_ROOT/.venv/bin/python}"

# shellcheck source=/dev/null
if [[ -f "${NEXUS_BACKUP_ENV:-$NEXUS_ROOT/config/backup.env}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${NEXUS_BACKUP_ENV:-$NEXUS_ROOT/config/backup.env}"
  set +a
fi

BACKUP_REPO="${NEXUS_BACKUP_REPO:-}"
BACKUP_PASSWORD_FILE="${NEXUS_BACKUP_PASSWORD_FILE:-}"
KEEP_DAILY="${NEXUS_BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${NEXUS_BACKUP_KEEP_WEEKLY:-4}"
DRY_RUN="${NEXUS_BACKUP_DRY_RUN:-0}"
CHECK_DATA="${NEXUS_BACKUP_CHECK_DATA:-0}"
TAG="nexus-baseline"

log() { printf '[backup] %s\n' "$*" >&2; }

fail() {
  log "ERRO: $*"
  exit 1
}

emit_event() {
  local kind="$1" payload="$2"
  [[ -x "$PYTHON" ]] || return 0
  "$PYTHON" -m nexus_core --config "$CONFIG_FILE" events emit \
    --kind "$kind" --source backup-baseline --payload "$payload" >/dev/null 2>&1 \
    || log "aviso: não foi possível emitir o evento $kind"
}

STAGE=""
cleanup() {
  if [[ -n "$STAGE" && -d "$STAGE" ]]; then
    rm -rf -- "$STAGE"
  fi
}

on_error() {
  local code=$?
  trap - ERR
  emit_event "system.backup.failed" "{\"exit_code\":$code}"
  log "backup falhou (exit=$code)"
  exit "$code"
}

trap cleanup EXIT
trap on_error ERR

verify_sqlite() {
  local name="$1" path="$2" result
  [[ -f "$path" ]] || fail "backup ausente: $path"
  if command -v sqlite3 >/dev/null 2>&1; then
    result="$(sqlite3 "$path" 'PRAGMA integrity_check;')"
  else
    result="$("$PYTHON" - "$path" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    print(connection.execute("PRAGMA integrity_check;").fetchone()[0])
finally:
    connection.close()
PY
)"
  fi
  [[ "$result" == "ok" ]] || fail "integrity_check de $name falhou: $result"
}

sqlite_copy() {
  local src="$1" dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'"
  else
    "$PYTHON" - "$src" "$dst" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
  fi
}

backup_sqlite() {
  local name="$1"
  local src="$DATA_DIR/databases/$name.db"
  local dst="$STAGE/databases/$name.db"

  if [[ ! -f "$src" ]]; then
    log "aviso: $src ausente; ignorado"
    return 0
  fi

  sqlite_copy "$src" "$dst"
  verify_sqlite "$name" "$dst"
  log "sqlite copiado: $name"
}

copy_state_dir() {
  local name="$1"
  local src="$DATA_DIR/$name"
  [[ -d "$src" ]] || { log "aviso: $src ausente; ignorado"; return 0; }
  cp -a "$src" "$STAGE/state/$name"
  log "estado copiado: $name"
}

command -v sqlite3 >/dev/null 2>&1 || [[ -x "$PYTHON" ]] \
  || fail "nem sqlite3 nem o python do venv estão disponíveis"
[[ -d "$DATA_DIR" ]] || fail "data dir não encontrado: $DATA_DIR"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nexus-backup.XXXXXX")"
mkdir -p "$STAGE/databases" "$STAGE/config" "$STAGE/state"

# 1) Bancos SQLite (WAL-safe via backup API).
if [[ -x "$PYTHON" ]] && "$PYTHON" -m nexus_core --config "$CONFIG_FILE" \
  db backup --to "$STAGE/databases/nexus_events.db" >/dev/null 2>&1; then
  verify_sqlite "nexus_events" "$STAGE/databases/nexus_events.db"
  log "sqlite copiado: nexus_events (nexus db backup)"
else
  log "aviso: 'nexus db backup' indisponível; usando sqlite3"
  backup_sqlite "nexus_events"
fi

backup_sqlite "nexus_daemon_events"
backup_sqlite "nexus_approvals"

# 2) Configuração (exceto o arquivo de senha do restic, se estiver em config/).
mkdir -p "$STAGE/config"
for file in "$NEXUS_ROOT"/config/*; do
  [[ -f "$file" ]] || continue
  if [[ -n "$BACKUP_PASSWORD_FILE" && "$file" == "$BACKUP_PASSWORD_FILE" ]]; then
    continue
  fi
  cp -a "$file" "$STAGE/config/"
done

# 3) Estado local que não vive nos bancos.
copy_state_dir "tasks"
copy_state_dir "memory"
copy_state_dir "vectors"
copy_state_dir "secrets"

STAGE_SIZE="$(du -sb "$STAGE" | cut -f1)"
log "staging pronto: $STAGE ($STAGE_SIZE bytes)"

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run: nenhum envio ao restic"
  find "$STAGE" -maxdepth 3 -type f | sort | sed 's/^/  /' >&2
  exit 0
fi

# 4) Envio criptografado + retenção.
[[ -n "$BACKUP_REPO" ]] || fail "NEXUS_BACKUP_REPO não definido (config/backup.env)"
command -v restic >/dev/null 2>&1 || fail "restic não encontrado no PATH"

export RESTIC_REPOSITORY="$BACKUP_REPO"
if [[ -n "$BACKUP_PASSWORD_FILE" ]]; then
  [[ -r "$BACKUP_PASSWORD_FILE" ]] || fail "senha do restic ilegível: $BACKUP_PASSWORD_FILE"
  export RESTIC_PASSWORD_FILE="$BACKUP_PASSWORD_FILE"
fi

# O cache do restic precisa ser gravável mesmo com ProtectHome=read-only.
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-$DATA_DIR/cache/restic}"
mkdir -p "$RESTIC_CACHE_DIR"

if ! restic cat config >/dev/null 2>&1; then
  log "repositório ausente ou inacessível; inicializando $BACKUP_REPO"
  restic init
fi

restic backup "$STAGE" \
  --tag "$TAG" \
  --host "$(hostname -s)"

restic forget \
  --tag "$TAG" \
  --keep-daily "$KEEP_DAILY" \
  --keep-weekly "$KEEP_WEEKLY" \
  --prune

if [[ "$CHECK_DATA" == "1" ]]; then
  restic check --read-data-subset=1/20
else
  restic check
fi

emit_event "system.backup.completed" \
  "{\"repo\":\"$BACKUP_REPO\",\"stage_bytes\":$STAGE_SIZE,\"keep_daily\":$KEEP_DAILY,\"keep_weekly\":$KEEP_WEEKLY}"
log "backup concluído: $BACKUP_REPO"
