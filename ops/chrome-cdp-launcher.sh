#!/bin/bash
# launchd-safe Chrome CDP supervisor. It never removes SingletonLock while the
# configured profile is in use, which prevents Chrome profile corruption loops.

CDP_PORT="${CLAW_CHROME_PORT:-9250}"
CDP_DIR="${CLAW_CHROME_PROFILE_DIR:-$HOME/.claw/chrome-profile}"
CHROME_BIN="${CLAW_CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
CHECK_INTERVAL_SECONDS="${CLAW_CHROME_CHECK_INTERVAL_SECONDS:-30}"
BLOCKED_INTERVAL_SECONDS="${CLAW_CHROME_BLOCKED_INTERVAL_SECONDS:-60}"

cdp_ready() {
    curl -sf "http://127.0.0.1:$CDP_PORT/json/version" > /dev/null 2>&1
}

profile_pids() {
    ps -ax -o pid=,command= 2>/dev/null \
        | awk -v dir="$CDP_DIR" 'BEGIN { IGNORECASE = 1 } /Chrome/ && index($0, "--user-data-dir=" dir) { print $1 }'
}

if [ ! -x "$CHROME_BIN" ]; then
    echo "Chrome binary not found or not executable: $CHROME_BIN"
    exit 1
fi

mkdir -p "$CDP_DIR/Default"

while true; do
    if cdp_ready; then
        sleep "$CHECK_INTERVAL_SECONDS"
        continue
    fi

    ACTIVE_PROFILE_PIDS="$(profile_pids)"
    if [ -n "$ACTIVE_PROFILE_PIDS" ]; then
        echo "Chrome profile $CDP_DIR is active in PID(s): $ACTIVE_PROFILE_PIDS"
        echo "Waiting; not removing SingletonLock while the profile is active."
        sleep "$BLOCKED_INTERVAL_SECONDS"
        continue
    fi

    rm -f "$CDP_DIR/SingletonLock"
    "$CHROME_BIN" \
        --remote-debugging-port="$CDP_PORT" \
        --remote-allow-origins="*" \
        --user-data-dir="$CDP_DIR" \
        --no-first-run \
        --disable-default-apps &
    CHILD_PID="$!"
    echo "Chrome CDP launched on port $CDP_PORT with PID $CHILD_PID"
    wait "$CHILD_PID"
    STATUS="$?"
    echo "Chrome CDP process exited with status $STATUS"
    sleep 5
done
