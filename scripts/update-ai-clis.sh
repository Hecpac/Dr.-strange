#!/usr/bin/env bash
set -euo pipefail

CODEX_VERSION="0.136.0"
CLAUDE_CODE_VERSION="2.1.152"

codex_version="$(codex --version 2>/dev/null || true)"
claude_version="$(claude --version 2>/dev/null || true)"

if [[ "$codex_version" == *"$CODEX_VERSION"* && "$claude_version" == *"$CLAUDE_CODE_VERSION"* ]]; then
  echo "Codex and Claude Code CLIs are already pinned and verified."
  if command -v npm >/dev/null 2>&1; then
    npm list -g --depth=0 @openai/codex @anthropic-ai/claude-code
  fi
  exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to install Codex and Claude Code CLIs" >&2
  exit 1
fi

echo "==> Installing Codex CLI ${CODEX_VERSION} and Claude Code ${CLAUDE_CODE_VERSION}..."
npm install -g --fetch-timeout=15000 "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

echo "==> Verifying installed CLIs..."
codex_version="$(codex --version)"
claude_version="$(claude --version)"

case "$codex_version" in
  *"$CODEX_VERSION"*) ;;
  *)
    echo "expected codex ${CODEX_VERSION}, got: ${codex_version}" >&2
    exit 1
    ;;
esac

case "$claude_version" in
  *"$CLAUDE_CODE_VERSION"*) ;;
  *)
    echo "expected claude ${CLAUDE_CODE_VERSION}, got: ${claude_version}" >&2
    exit 1
    ;;
esac

npm list -g --depth=0 @openai/codex @anthropic-ai/claude-code
echo "Codex and Claude Code CLIs are pinned and verified."
