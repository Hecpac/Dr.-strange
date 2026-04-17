#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
source "$HOME/.claw/env" 2>/dev/null || true
set +a
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$REPO_ROOT}"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  for shell_rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.zshenv" "$HOME/.profile"; do
    if [[ -f "$shell_rc" ]]; then
      ANTHROPIC_FROM_SHELL="$(
        sed -nE 's/^[[:space:]]*(export[[:space:]]+)?ANTHROPIC_API_KEY=(.*)$/\2/p' "$shell_rc" \
          | tail -n 1 \
          | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
      )"
      if [[ -n "${ANTHROPIC_FROM_SHELL:-}" ]]; then
        export ANTHROPIC_API_KEY="$ANTHROPIC_FROM_SHELL"
        break
      fi
    fi
  done
fi
if [[ -z "${CLAUDE_CLI_PATH:-}" ]]; then
  if CLAUDE_BIN="$(command -v claude 2>/dev/null)"; then
    export CLAUDE_CLI_PATH="$CLAUDE_BIN"
  fi
fi
if [[ -z "${CODEX_CLI_PATH:-}" ]]; then
  if CODEX_BIN="$(command -v codex 2>/dev/null)"; then
    export CODEX_CLI_PATH="$CODEX_BIN"
  fi
fi
cd "$REPO_ROOT"
exec "$REPO_ROOT/.venv/bin/python" -m claw_v2.main
