#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
source "$HOME/.claw/env" 2>/dev/null || true
set +a
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$REPO_ROOT}"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  for shell_rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.zshenv" "$HOME/.profile"; do
    if [[ -f "$shell_rc" ]]; then
      ANTHROPIC_FROM_SHELL="$(
        sed -nE 's/^[[:space:]]*(export[[:space:]]+)?ANTHROPIC_API_KEY=(.*)$/\2/p' "$shell_rc" \
          | tail -n 1 \
          | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
      )"
      if [[ -n "${ANTHROPIC_FROM_SHELL:-}" ]]; then
        export ANTHROPIC_API_KEY="$ANTHROPIC_FROM_SHELL"
        break
      fi
    fi
  done
fi
if [[ -z "${CLAUDE_CLI_PATH:-}" ]]; then
  if CLAUDE_BIN="$(command -v claude 2>/dev/null)"; then
    export CLAUDE_CLI_PATH="$CLAUDE_BIN"
  fi
fi
if [[ -z "${CODEX_CLI_PATH:-}" ]]; then
  if CODEX_BIN="$(command -v codex 2>/dev/null)"; then
    export CODEX_CLI_PATH="$CODEX_BIN"
  fi
fi
cd "$REPO_ROOT"

# Slice 2a: if a runtime-DB halt marker exists (corruption detected by the
# preflight or by a previous boot), HOLD here instead of exec'ing the daemon.
# Staying alive without exec is what stops launchd KeepAlive from crash-looping
# the boot. The preflight re-runs each cycle and is the only thing that clears
# the marker — and only after the DB passes a thorough integrity check.
notify_owner() {
  # The token travels via curl --config on stdin so it is not exposed in the
  # process argument list while the alert is in flight.
  [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_ALLOWED_USER_ID:-}" ]] || return 0
  curl -sS -m 10 --config - \
    --data-urlencode "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
    --data-urlencode "text=$1" >/dev/null 2>&1 <<EOF || true
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
EOF
}

CLAW_DB_PATH="${DB_PATH:-data/claw.db}"
HALT_MARKER="$(dirname "$CLAW_DB_PATH")/runtime_db_halt.json"
# Non-numeric would kill sleep under set -e (recreating the crash-loop this
# guard exists to stop) and 0 would tight-loop the preflight — floor to 5s.
HALT_RECHECK_S="${CLAW_DB_HALT_RECHECK_S:-300}"
case "$HALT_RECHECK_S" in
  '' | *[!0-9]*) HALT_RECHECK_S=300 ;;
esac
(( HALT_RECHECK_S >= 5 )) || HALT_RECHECK_S=5
hold_cycles=0
while [[ -f "$HALT_MARKER" ]]; do
  if (( hold_cycles % 6 == 0 )); then
    notify_owner "🛑 Claw en HOLD: marca de halt de DB presente ($HALT_MARKER). El daemon NO arranca hasta que la DB pase integrity. Restaura un backup verificado de data/backups/restart/ — el hold se libera solo."
  fi
  hold_cycles=$((hold_cycles + 1))
  "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/runtime_db_preflight.py" \
    --db "$CLAW_DB_PATH" \
    --backup-dir "${CLAW_RESTART_DB_BACKUP_DIR:-data/backups/restart}" || true
  [[ -f "$HALT_MARKER" ]] || break
  sleep "$HALT_RECHECK_S"
done
if (( hold_cycles > 0 )); then
  notify_owner "✅ Claw sale del HOLD: la DB volvió a pasar integrity tras $hold_cycles ciclo(s). Arrancando daemon."
fi

if [[ "${CLAW_LAUNCHER_DRY_EXEC:-}" == "1" ]]; then
  echo "DRY_EXEC: would exec $REPO_ROOT/.venv/bin/python -m claw_v2.main"
  exit 0
fi
exec "$REPO_ROOT/.venv/bin/python" -m claw_v2.main
