#!/usr/bin/env bash
set -euo pipefail

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to install Codex and Claude Code CLIs" >&2
  exit 1
fi

extract_semver() {
  printf '%s\n' "$1" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?' | head -n 1
}

CODEX_VERSION="${CODEX_VERSION:-$(npm view @openai/codex version)}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-$(npm view @anthropic-ai/claude-code version)}"

codex_version_raw="$(codex --version 2>/dev/null || true)"
claude_version_raw="$(claude --version 2>/dev/null || true)"
codex_version="$(extract_semver "$codex_version_raw" || true)"
claude_version="$(extract_semver "$claude_version_raw" || true)"

packages=()
if [[ "$codex_version" != "$CODEX_VERSION" ]]; then
  packages+=("@openai/codex@${CODEX_VERSION}")
fi
if [[ "$claude_version" != "$CLAUDE_CODE_VERSION" ]]; then
  packages+=("@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}")
fi

if [[ "${#packages[@]}" -eq 0 ]]; then
  echo "Codex and Claude Code CLIs are already current and verified."
  npm list -g --depth=0 @openai/codex @anthropic-ai/claude-code
  exit 0
fi

echo "==> Installing AI CLI packages: ${packages[*]}"
npm install -g --fetch-timeout=300000 "${packages[@]}"

echo "==> Verifying installed CLIs..."
codex_version="$(extract_semver "$(codex --version)")"
claude_version="$(extract_semver "$(claude --version)")"

if [[ "$codex_version" != "$CODEX_VERSION" ]]; then
  echo "expected codex ${CODEX_VERSION}, got: ${codex_version}" >&2
  exit 1
fi

if [[ "$claude_version" != "$CLAUDE_CODE_VERSION" ]]; then
  echo "expected claude ${CLAUDE_CODE_VERSION}, got: ${claude_version}" >&2
  exit 1
fi

npm list -g --depth=0 @openai/codex @anthropic-ai/claude-code
echo "Codex and Claude Code CLIs are current and verified."
