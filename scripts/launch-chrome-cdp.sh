#!/bin/bash
# Launch Chrome with CDP remote debugging enabled.
# Uses a dedicated user-data-dir because the default profile blocks CDP
# in Chrome 146. Cookie reuse is opt-in to avoid copying authenticated
# browser state by default.

CDP_PORT="${1:-9222}"
CDP_DIR="$HOME/.claw-chrome-cdp"
SRC_PROFILE="${CLAW_CHROME_COOKIE_SOURCE:-$HOME/Library/Application Support/Google/Chrome/Profile 3}"
COPY_COOKIES="${CLAW_CHROME_COPY_COOKIES:-0}"

PIDS="$(lsof -tiTCP:"$CDP_PORT" -sTCP:LISTEN 2>/dev/null || true)"
for pid in $PIDS; do
    cmd="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
    case "$cmd" in
        *Chrome*)
            kill -TERM "$pid" 2>/dev/null || true
            ;;
        *)
            echo "Port $CDP_PORT is already in use by '$cmd' (PID $pid)."
            exit 1
            ;;
    esac
done

if [ -n "$PIDS" ]; then
    sleep 1
fi

# Bootstrap CDP profile if needed, copy cookies for session reuse
if [ ! -d "$CDP_DIR/Default" ]; then
    mkdir -p "$CDP_DIR/Default"
fi
rm -f "$CDP_DIR/SingletonLock"

# Sync cookies from a chosen source profile only when explicitly requested.
if [ "$COPY_COOKIES" = "1" ] && [ -f "$SRC_PROFILE/Cookies" ]; then
    cp -f "$SRC_PROFILE/Cookies" "$CDP_DIR/Default/Cookies" 2>/dev/null
    if [ -f "$SRC_PROFILE/Cookies-journal" ]; then
        cp -f "$SRC_PROFILE/Cookies-journal" "$CDP_DIR/Default/Cookies-journal" 2>/dev/null
    fi
fi

open -na "Google Chrome" --args \
    --remote-debugging-port="$CDP_PORT" \
    --remote-allow-origins="*" \
    --user-data-dir="$CDP_DIR" \
    --no-first-run

echo "Chrome CDP launching on port $CDP_PORT..."
for i in 1 2 3 4 5; do
    sleep 1
    if curl -sf "http://127.0.0.1:$CDP_PORT/json/version" > /dev/null 2>&1; then
        echo "CDP ready on port $CDP_PORT"
        exit 0
    fi
done
echo "WARNING: CDP not responding after 5s — check manually"
