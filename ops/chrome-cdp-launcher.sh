#!/bin/bash
# launchd-safe Chrome CDP supervisor. It never removes SingletonLock while the
# configured profile is in use, which prevents Chrome profile corruption loops.

CDP_PORT="${CLAW_CHROME_PORT:-9250}"
CDP_DIR="${CLAW_CHROME_PROFILE_DIR:-$HOME/.claw/chrome-profile}"
CHROME_BIN="${CLAW_CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
CHECK_INTERVAL_SECONDS="${CLAW_CHROME_CHECK_INTERVAL_SECONDS:-30}"
# Refocus/resize Chrome on every healthy poll (steals window focus). Off by
# default; focus on launch is unconditional below. Set to 1 to restore the old
# always-front behavior.
AUTOFOCUS_ON_READY="${CLAW_CHROME_AUTOFOCUS_ON_READY:-0}"
BLOCKED_INTERVAL_SECONDS="${CLAW_CHROME_BLOCKED_INTERVAL_SECONDS:-60}"

cdp_ready() {
    curl -sf "http://127.0.0.1:$CDP_PORT/json/version" > /dev/null 2>&1
}

cdp_page_count() {
    curl -sf "http://127.0.0.1:$CDP_PORT/json/list" 2>/dev/null \
        | grep -c '"type"[[:space:]]*:[[:space:]]*"page"' || true
}

profile_pids() {
    ps -ax -o pid=,command= 2>/dev/null \
        | awk -v dir="$CDP_DIR" 'BEGIN { IGNORECASE = 1 } /Chrome/ && index($0, "--user-data-dir=" dir) { print $1 }'
}

focus_chrome_pid() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return 1
    fi
    local result
    result="$(osascript <<APPLESCRIPT 2>/dev/null || true
tell application "Finder" to set desktopBounds to bounds of window of desktop
tell application "System Events"
  repeat with p in (processes whose name is "Google Chrome")
    if (((unix id of p) as integer) is $pid) and ((count windows of p) > 0) then
      set visible of p to true
      set frontmost of p to true
      set position of window 1 of p to {item 1 of desktopBounds, item 2 of desktopBounds}
      set size of window 1 of p to {(item 3 of desktopBounds) - (item 1 of desktopBounds), (item 4 of desktopBounds) - (item 2 of desktopBounds)}
      return "focused"
    end if
  end repeat
end tell
return "missing"
APPLESCRIPT
)"
    [ "$result" = "focused" ]
}

ensure_chrome_window() {
    local pid
    pid="$(profile_pids | head -n 1)"
    if focus_chrome_pid "$pid"; then
        return 0
    fi
    if [ "$(cdp_page_count)" -gt 0 ]; then
        return 0
    fi
    curl -sf -X PUT "http://127.0.0.1:$CDP_PORT/json/new?about:blank" >/dev/null 2>&1 || true
    sleep 1
    pid="$(profile_pids | head -n 1)"
    focus_chrome_pid "$pid" || true
}

if [ ! -x "$CHROME_BIN" ]; then
    echo "Chrome binary not found or not executable: $CHROME_BIN"
    exit 1
fi

mkdir -p "$CDP_DIR/Default"

while true; do
    if cdp_ready; then
        if [ "$AUTOFOCUS_ON_READY" = "1" ]; then
            ensure_chrome_window
        fi
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
        --disable-default-apps \
        --start-maximized \
        --window-position=0,0 \
        --window-size=1800,1100 &
    CHILD_PID="$!"
    echo "Chrome CDP launched on port $CDP_PORT with PID $CHILD_PID"
    focus_chrome_pid "$CHILD_PID" || true
    for _ in 1 2 3 4 5; do
        sleep 1
        if cdp_ready; then
            ensure_chrome_window
            break
        fi
    done
    wait "$CHILD_PID"
    STATUS="$?"
    echo "Chrome CDP process exited with status $STATUS"
    sleep 5
done
