#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

set -a
source "$HOME/.claw/env" 2>/dev/null || true
set +a

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python"
fi

report_json="$("$PYTHON_BIN" -m claw_v2.diagnostics --json --limit 5)"

# Live uptime of the daemon process, passed to the decision module so it can
# apply the bootstrap grace (never restart a daemon that is still coming up).
daemon_pid="$(pgrep -f 'claw_v2.main' 2>/dev/null | head -n 1 || true)"
daemon_etime=""
if [[ -n "$daemon_pid" ]]; then
  daemon_etime="$(ps -p "$daemon_pid" -o etime= 2>/dev/null | tr -d ' ' || true)"
fi

# The decision is debounced (bootstrap grace + persistent N-strikes) in
# claw_v2.watchdog; diagnostics' critical condition itself is unchanged.
decision="$(
  printf '%s' "$report_json" \
    | "$PYTHON_BIN" -m claw_v2.watchdog --uptime "$daemon_etime" 2>/dev/null || true
)"
decision="${decision:-ok}"

if [[ "$decision" != "restart" ]]; then
  echo "claw-watchdog: no action ($decision)"
  exit 0
fi

echo "claw-watchdog: restart threshold reached; restarting Claw"
bash "$REPO_ROOT/scripts/restart.sh"
sleep 2
"$PYTHON_BIN" -m claw_v2.diagnostics --limit 5
