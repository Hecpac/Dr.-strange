#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$HOME/.claw/env" 2>/dev/null || true
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m claw_v2.main
