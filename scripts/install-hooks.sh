#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
src="$repo_root/scripts/git-hooks/pre-push"
dst="$repo_root/.git/hooks/pre-push"

if [[ ! -f "$src" ]]; then
  echo "missing source hook: $src" >&2
  exit 1
fi

ln -sf "$src" "$dst"
chmod +x "$src"
echo "installed pre-push hook -> $dst"
