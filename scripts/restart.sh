#!/bin/bash
# Restart Claw daemon — wrapper that avoids path tokens in the command
cd "$(dirname "$0")/.." || exit 1
source "$HOME/.claw/env" 2>/dev/null || true

LABEL="${CLAW_LAUNCHD_LABEL:-com.pachano.claw}"
DOMAIN="gui/$(id -u)/$LABEL"

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
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if lsof -nP "-iTCP:$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
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

if launchctl print "$DOMAIN" >/dev/null 2>&1; then
  launchctl kickstart -k "$DOMAIN"
  pid="$(wait_for_process)" || {
    echo "ERROR: launchd restart did not produce a Claw process" >&2
    exit 1
  }
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
