#!/usr/bin/env bash
# Idempotent installer for the Claw launchd supervisor.
#
# Installs ~/Library/LaunchAgents/com.pachano.claw.plist from the repo template,
# validates absolute paths (plist DTD + launcher + python), and bootstraps the
# agent into the user's GUI session so macOS auto-restarts Claw on crash/reboot.
#
# Safe to re-run: it bootout's the existing agent before re-loading.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.pachano.claw"
PLIST_SRC="$REPO_ROOT/ops/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHER="$REPO_ROOT/ops/claw-launcher.sh"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$HOME/.claw"

# --- pre-flight: absolute paths must exist (launchd does NOT inherit shell) ---
[[ -f "$PLIST_SRC"  ]] || { echo "ERROR: missing plist template: $PLIST_SRC" >&2; exit 1; }
[[ -x "$LAUNCHER"   ]] || { echo "ERROR: launcher not executable: $LAUNCHER" >&2; exit 1; }
[[ -x "$PYTHON_BIN" ]] || { echo "ERROR: venv python not found: $PYTHON_BIN (run 'python -m venv .venv && .venv/bin/pip install -e .')" >&2; exit 1; }
mkdir -p "$LOG_DIR"

# --- plist validation ---
plutil -lint "$PLIST_SRC" >/dev/null

# --- install ---
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

# --- reload into GUI session idempotently ---
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
fi
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed $LABEL (domain=$DOMAIN)."
echo "  plist:   $PLIST_DST"
echo "  logs:    $LOG_DIR/claw.stdout.log, $LOG_DIR/claw.stderr.log"
echo "  status:  launchctl print $DOMAIN/$LABEL"
