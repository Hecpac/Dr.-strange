#!/usr/bin/env bash
set -euo pipefail

# One-shot bootstrap: upgrade claude CLI to 2.1.120+ and install the pre-push hook.
# Run this once, then claw takes over.

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

echo "==> 1/3 Upgrading claude CLI to 2.1.120..."
if command -v npm >/dev/null 2>&1; then
  npm install -g @anthropic-ai/claude-code@2.1.120
elif command -v pnpm >/dev/null 2>&1; then
  pnpm add -g @anthropic-ai/claude-code@2.1.120
elif command -v brew >/dev/null 2>&1 && brew list claude-code >/dev/null 2>&1; then
  brew upgrade claude-code
else
  echo "no supported installer found (npm/pnpm/brew)" >&2
  exit 1
fi

echo "==> 2/3 Verifying claude version..."
claude --version || { echo "claude CLI not on PATH after install" >&2; exit 1; }

echo "==> 3/3 Installing pre-push hook..."
chmod +x scripts/git-hooks/pre-push scripts/install-hooks.sh
./scripts/install-hooks.sh

echo
echo "✅ Bootstrap complete. Pre-push hook active. Claw will handle the rest."
