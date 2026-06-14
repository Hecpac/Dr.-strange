#!/bin/bash
# Launch Chrome with CDP remote debugging enabled.
# Uses a dedicated user-data-dir because the default profile blocks CDP
# in Chrome 146. Cookie reuse is opt-in to avoid copying authenticated
# browser state by default.

CDP_PORT="${1:-${CLAW_CHROME_PORT:-9250}}"
CDP_DIR="${CLAW_CHROME_PROFILE_DIR:-$HOME/.claw/chrome-profile}"
SRC_PROFILE="${CLAW_CHROME_COOKIE_SOURCE:-$HOME/Library/Application Support/Google/Chrome/Profile 3}"
COPY_COOKIES="${CLAW_CHROME_COPY_COOKIES:-0}"

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

focus_profile_window() {
    local pid
    pid="$(profile_pids | head -n 1)"
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

ensure_profile_window() {
    if focus_profile_window; then
        return 0
    fi
    if [ "$(cdp_page_count)" -gt 0 ]; then
        return 0
    fi
    curl -sf -X PUT "http://127.0.0.1:$CDP_PORT/json/new?about:blank" >/dev/null 2>&1 || true
    sleep 1
    focus_profile_window || true
}

if cdp_ready; then
    ensure_profile_window
    echo "CDP already ready on port $CDP_PORT"
    exit 0
fi

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

ACTIVE_PROFILE_PIDS="$(profile_pids)"
if [ -n "$ACTIVE_PROFILE_PIDS" ]; then
    echo "Chrome profile $CDP_DIR is already in use by PID(s): $ACTIVE_PROFILE_PIDS"
    echo "Refusing to remove SingletonLock while the profile is active."
    exit 1
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
    --remote-allow-origins="http://127.0.0.1:${CDP_PORT},http://localhost:${CDP_PORT}" \
    --user-data-dir="$CDP_DIR" \
    --no-first-run \
    --start-maximized \
    --window-position=0,0 \
    --window-size=1800,1100

echo "Chrome CDP launching on port $CDP_PORT..."
for i in 1 2 3 4 5; do
    sleep 1
    if cdp_ready; then
        ensure_profile_window
        echo "CDP ready on port $CDP_PORT"
        exit 0
    fi
done
echo "WARNING: CDP not responding after 5s — check manually"
