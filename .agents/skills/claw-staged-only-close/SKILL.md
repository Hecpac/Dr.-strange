---
name: claw-staged-only-close
description: Use when closing a Dr. Strange PR or commit from a dirty working tree with an exact manifest, staged-only validation in a temporary worktree, and no unrelated files.
---

# Claw Staged-Only Close

Use this when the working tree is dirty and the user asks to close one isolated
PR or commit.

## Rules

- Never use `git add .`.
- Never use `git commit -a`.
- Stage only the approved manifest with explicit `git add <file>` commands.
- Do not include `.patch-backups/`, `memory/*.md`, unrelated audit artifacts, or
  other fronts unless explicitly approved.
- Commit only after the staged-only gate passes.

## Pre-Stage Checks

```bash
git status --short
git diff --name-status
git diff --cached --name-status
git ls-files --others --exclude-standard
```

Confirm the exact manifest before staging.

## Required Gate

```bash
git diff --cached --name-status
git diff --cached --check
git status --short
```

Validate staged-only in a temporary worktree:

```bash
ORIG="$PWD"
tmpdir="$(mktemp -d)"
git diff --cached --binary > "$tmpdir/staged.patch"
git worktree add "$tmpdir/wt" HEAD
cd "$tmpdir/wt"
git apply --index "$tmpdir/staged.patch"
# run the focused gate from the PR plan here
git diff --cached --check
cd "$ORIG"
git worktree remove "$tmpdir/wt"
```

## Output

Report:

- staged files;
- unstaged files left out;
- untracked files left out;
- staged-only gate result;
- commit hash if the user approved a commit;
- residual risks.
