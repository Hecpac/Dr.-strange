---
name: claw-pr-checkpoint-loop
description: Use for Dr. Strange checkpoint-based PR implementation with a brief spec, verification plan, red contracts when useful, minimal production changes, gates, and review before close.
---

# Claw PR Checkpoint Loop

Use this for non-trivial implementation work that should land as one narrow PR
or a sequence of small checkpoints.

## Rules

- Start with repo inspection and a brief spec before implementation.
- Define verification before mutating files.
- Use red contracts or tests first when the behavior is new or risky.
- Implement the smallest production change that satisfies the checkpoint.
- Run a focused gate after each checkpoint.
- Review the diff before closing.
- Do not mix F2 RuntimeDb, F3 leases, F4 Browser, F5 brief/task, or docs/memory
  changes unless the user explicitly approves it.
- Do not use `git add .`.

## Checkpoint Template

```markdown
## Spec
- Goal:
- Scope:
- Out of scope:
- Expected files:
- Acceptance criteria:

## Verification
- Commands:
- Broader gate if:

## Stop Conditions
- <condition that requires user input or a separate PR>
```

## Output

For each checkpoint, report:

- files touched;
- contracts/tests added or changed;
- implementation summary;
- gate result;
- residual risks;
- whether the next checkpoint is safe to start.
