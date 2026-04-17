#!/bin/bash
# Restart Claw daemon — wrapper that avoids path tokens in the command
cd "$(dirname "$0")/.." || exit 1
source "$HOME/.claw/env" 2>/dev/null || true

# Kill existing instances with SIGKILL to avoid zombie polling conflicts
pkill -9 -f "claw_v2.main" 2>/dev/null
# Wait until no claw_v2.main process remains (max 5s)
for i in 1 2 3 4 5; do
  pgrep -f "claw_v2.main" >/dev/null 2>&1 || break
  sleep 1
done
# If still alive, bail out
if pgrep -f "claw_v2.main" >/dev/null 2>&1; then
  echo "ERROR: could not kill existing Claw process" >&2
  exit 1
fi

: > "$HOME/.claw/claw.pid" 2>/dev/null || true
mkdir -p logs
source .venv/bin/activate
nohup python -m claw_v2.main > logs/claw.log 2>&1 &
echo "Claw restarted (pid $!)"
