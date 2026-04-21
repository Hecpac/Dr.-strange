#!/usr/bin/env bash
set -u

CLAW_DIR="$HOME/.claw"
PID_FILE="$CLAW_DIR/claw.pid"
ALERT_STAMP="$CLAW_DIR/watchdog_last_alert"
ALERT_COOLDOWN_SEC=1800
UID_ME="$(id -u)"
LAUNCHD_LABEL="com.pachano.claw"

set -a
[[ -f "$HOME/.claw/env" ]] && source "$HOME/.claw/env"
set +a

_now() { date +%s; }

_cooldown_active() {
  [[ -f "$ALERT_STAMP" ]] || return 1
  local last
  last="$(cat "$ALERT_STAMP" 2>/dev/null || echo 0)"
  local age=$(( $(_now) - last ))
  [[ $age -lt $ALERT_COOLDOWN_SEC ]]
}

_alert_telegram() {
  local msg="$1"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_ALLOWED_USER_ID:-}" ]]; then
    return
  fi
  curl -fsS --max-time 10 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
    --data-urlencode "text=$msg" >/dev/null 2>&1 || true
  _now > "$ALERT_STAMP"
}

_is_alive() {
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null)" || return 1
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

if _is_alive; then
  exit 0
fi

if ! _cooldown_active; then
  _alert_telegram "🚨 Claw caído — watchdog intentando reiniciar (kickstart)"
fi

launchctl kickstart -k "gui/${UID_ME}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
