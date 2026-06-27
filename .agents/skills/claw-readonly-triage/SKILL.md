---
name: claw-readonly-triage
description: Use for Dr. Strange read-only working tree triage, front classification, dependency mapping, candidate manifests, and stop conditions before edits, staging, or commits.
---

# Claw Read-Only Triage

Use this when the user asks to inspect, classify, audit, or plan work without
mutating the repo.

## Rules

- Do not edit files.
- Do not stage or commit.
- Do not install dependencies.
- Do not delete files or data.
- Do not use `git stash`, `git reset`, or `git checkout`.
- Do not use `git add .`.
- Keep F2 RuntimeDb, F3 leases, F4 Browser, F5 brief/task, and docs/memory as
  separate fronts unless the user explicitly approves a cumulative branch.

## Commands

```bash
git status --short
git diff --name-status
git ls-files --others --exclude-standard
git diff --stat
```

Read focused diffs only after grouping files by front.

## Output

Report for each front:

- name;
- files;
- what it appears to implement or fix;
- dependencies on committed or unstaged work;
- whether it is self-contained;
- candidate manifest;
- focused gate;
- stop condition.

Finish with a recommended next PR and the exact manifest to inspect before any
staging.
