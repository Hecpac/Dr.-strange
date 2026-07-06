#!/bin/bash
# Restart Claw daemon — wrapper that avoids path tokens in the command
cd "$(dirname "$0")/.." || exit 1
source "$HOME/.claw/env" 2>/dev/null || true

LABEL="${CLAW_LAUNCHD_LABEL:-com.pachano.claw}"
DOMAIN="gui/$(id -u)/$LABEL"

run_runtime_db_preflight() {
  if [ "${CLAW_RESTART_SKIP_DB_PREFLIGHT:-}" = "1" ]; then
    echo "WARN: skipping runtime DB preflight because CLAW_RESTART_SKIP_DB_PREFLIGHT=1" >&2
    return 0
  fi
  db_path="${DB_PATH:-data/claw.db}"
  backup_dir="${CLAW_RESTART_DB_BACKUP_DIR:-data/backups/restart}"
  "$PWD/.venv/bin/python" scripts/runtime_db_preflight.py \
    --db "$db_path" \
    --backup-dir "$backup_dir"
}

wait_for_process() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pid="$(pgrep -f "claw_v2.main" 2>/dev/null | head -n 1)"
    if [ -n "$pid" ]; then
      echo "$pid"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_port() {
  port="${WEB_CHAT_PORT:-8765}"
  # Seconds to wait for the web port after a restart. The default 10s can be
  # too short for a slow bootstrap (DB contention), which made the watchdog
  # see port_listening=False and restart again; raise via env when needed.
  attempts="${CLAW_RESTART_PORT_WAIT_S:-10}"
  # A non-integer value would make the `-lt` test error and skip the wait
  # entirely (port reported down immediately); fall back to the default.
  case "$attempts" in
    '' | *[!0-9]*) attempts=10 ;;
  esac
  [ "$attempts" -gt 0 ] || attempts=10
  i=0
  while [ "$i" -lt "$attempts" ]; do
    if lsof -nP "-iTCP:$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

stop_pid() {
  pid="$1"
  [ -n "$pid" ] || return 0
  ps -p "$pid" -o command= 2>/dev/null | grep -q "claw_v2.main" || return 0
  kill -TERM "$pid" 2>/dev/null || return 0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ps -p "$pid" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo "WARN: graceful stop timed out for pid $pid; sending SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    ps -p "$pid" >/dev/null 2>&1 || return 0
    sleep 1
  done
  return 1
}

notify_owner() {
  # Best-effort Telegram alert (stdlib curl, never fails the script).
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ALLOWED_USER_ID:-}" ] || return 0
  curl -sS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
    --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# Slice 2a: a failing preflight (DB corruption) must abort the restart before
# the launchd kickstart — restarting onto a corrupt DB just re-enters the
# crash-boot loop. The preflight already wrote the halt marker; the launcher
# will hold any launchd respawn until the DB verifies healthy again.
run_runtime_db_preflight
preflight_rc=$?
if [ "$preflight_rc" -ne 0 ]; then
  echo "ERROR: runtime DB preflight failed (rc=$preflight_rc); aborting restart before kickstart" >&2
  notify_owner "🛑 Claw restart ABORTADO: el preflight de la DB falló (posible corrupción). Revisa data/runtime_db_halt.json y restaura un backup verificado de data/backups/restart/ — el boot queda en hold hasta que la DB pase integrity."
  exit 1
fi

if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  # T10 (2026-06-12): two daemons overlapping on the same SQLite WAL risk
  # sidecar churn. Capture the old PID, kickstart, and wait for the OLD
  # process to fully exit before accepting any "new" PID.
  old_pid="$(pgrep -f "claw_v2.main" 2>/dev/null | head -n 1)"
  launchctl kickstart -k "$DOMAIN"
  if [ -n "$old_pid" ]; then
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      ps -p "$old_pid" >/dev/null 2>&1 || break
      sleep 1
    done
    if ps -p "$old_pid" >/dev/null 2>&1; then
      echo "ERROR: previous Claw process $old_pid is still alive after kickstart" >&2
      exit 1
    fi
  fi
  pid="$(wait_for_process)" || {
    echo "ERROR: launchd restart did not produce a Claw process" >&2
    exit 1
  }
  if [ -n "$old_pid" ] && [ "$pid" = "$old_pid" ]; then
    echo "ERROR: launchd did not replace the Claw process (pid $pid unchanged)" >&2
    exit 1
  fi
  wait_for_port || {
    echo "ERROR: Claw process $pid started but web port did not listen" >&2
    exit 1
  }
  echo "Claw restarted via launchd (pid $pid)"
  exit 0
fi

if [ -s "$HOME/.claw/claw.pid" ]; then
  stop_pid "$(cat "$HOME/.claw/claw.pid" 2>/dev/null)"
fi

for pid in $(pgrep -f "claw_v2.main" 2>/dev/null); do
  stop_pid "$pid"
done

if pgrep -f "claw_v2.main" >/dev/null 2>&1; then
  echo "ERROR: could not stop existing Claw process" >&2
  exit 1
fi

: > "$HOME/.claw/claw.pid" 2>/dev/null || true
mkdir -p logs
source .venv/bin/activate
nohup python -m claw_v2.main > logs/claw.log 2>&1 &
echo "Claw restarted (pid $!)"
