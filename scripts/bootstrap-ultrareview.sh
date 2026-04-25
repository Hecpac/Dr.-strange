#!/usr/bin/env bash
set -euo pipefail

# One-shot bridge: upgrade claude CLI -> install pre-push hook -> open PR ->
# watch CI ultrareview -> exit when green. Run this once; claw takes over.

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

branch="$(git rev-parse --abbrev-ref HEAD)"

echo "==> 1/5 Upgrading claude CLI to 2.1.120..."
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

echo "==> 2/5 Verifying claude version..."
claude --version || { echo "claude CLI not on PATH after install" >&2; exit 1; }

echo "==> 3/5 Installing pre-push hook..."
chmod +x scripts/git-hooks/pre-push scripts/install-hooks.sh
./scripts/install-hooks.sh

echo "==> 4/5 Opening PR for $branch..."
if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI missing — install with: brew install gh && gh auth login" >&2
  exit 1
fi

if gh pr view "$branch" >/dev/null 2>&1; then
  pr_url="$(gh pr view "$branch" --json url -q .url)"
  echo "PR already exists: $pr_url"
else
  pr_url="$(gh pr create \
    --title "feat(ci): ultrareview pre-push hook + bootstrap installer" \
    --base main \
    --head "$branch" \
    --body "$(cat <<'BODY'
## Summary

- `scripts/git-hooks/pre-push` runs `claude ultrareview --json` against the current branch and blocks push on findings (override `CLAW_SKIP_ULTRAREVIEW=1`).
- `scripts/install-hooks.sh` symlinks the hook into `.git/hooks/`.
- `scripts/bootstrap-ultrareview.sh` one-shot installer (CLI upgrade + hook + PR + CI watch).

Requires `claude` CLI 2.1.120+.

## Test plan

- [ ] `./scripts/bootstrap-ultrareview.sh` produces `.git/hooks/pre-push` symlink and `claude --version` >= 2.1.120.
- [ ] Clean push: hook reports "ultrareview passed".
- [ ] Dirty push: hook blocks with non-zero exit and `/tmp/claw-ultrareview.json`.
- [ ] `CLAW_SKIP_ULTRAREVIEW=1 git push` bypasses.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)")
  echo "PR opened: $pr_url"
fi

echo "==> 5/5 Watching CI ultrareview..."
gh pr checks "$branch" --watch --interval 15 || {
  echo "CI failed or timed out. Inspect: $pr_url" >&2
  exit 1
}

echo
echo "✅ Bootstrap complete. Pre-push hook active, PR green: $pr_url"
echo "Tell claw: 'PR verde, retoma'."
