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
decision="$(
  printf '%s' "$report_json" | "$PYTHON_BIN" -c 'import json, sys
report = json.load(sys.stdin)
checks = report.get("checks") or {}
restartable = (
    checks.get("status") == "critical"
    and checks.get("database_readable")
    and (
        not checks.get("process_running")
        or not checks.get("port_listening")
        or checks.get("heartbeat_stale")
        or checks.get("web_transport_serving") is False
    )
)
print("restart" if restartable else checks.get("status", "unknown"))' 2>/dev/null || true
)"
decision="${decision:-unknown}"

if [[ "$decision" != "restart" ]]; then
  echo "claw-watchdog: no restart needed (status=$decision)"
  exit 0
fi

echo "claw-watchdog: critical restartable condition detected; restarting Claw"
bash "$REPO_ROOT/scripts/restart.sh"
sleep 2
"$PYTHON_BIN" -m claw_v2.diagnostics --limit 5
