---
name: Verification Pipeline
triggers:
  - verify
  - verification
  - qa check
  - pre-pr
priority: 9
---

# Verification Pipeline — Test → Simplify → PR

Multi-step verification workflow inspired by Boris Cherny's Claude Code workflow.
Execute each phase sequentially. Stop and report if any phase fails.

## Phase 1: Test

1. Detect the test runner (pytest, vitest, jest, cargo test, go test)
2. Run the full test suite (or scoped tests if user specified a target)
3. Report: total passed, failed, skipped
4. If failures exist, diagnose root cause and attempt fix
5. Re-run tests after fixes. If still failing, STOP and report

## Phase 2: Simplify

Review all changed files (use `git diff` against the base branch):

1. **Reuse**: Are there existing utilities, helpers, or patterns being duplicated?
2. **Quality**: Dead code, unused imports, overly complex logic, missing edge cases?
3. **Efficiency**: Unnecessary allocations, N+1 queries, redundant computations?
4. **Style**: Does the code match surrounding conventions?

Fix any issues found. Re-run tests after each fix.

## Phase 3: PR (requires user confirmation)

1. Summarize what was verified and what was fixed
2. Ask user if they want to create a PR
3. If confirmed: stage changes, commit, push, create PR with summary

## Output Format

```
## Verification Report

### Tests
- Status: PASSED / FAILED
- Results: X passed, Y failed, Z skipped
- Fixes applied: [list or "none"]

### Code Review
- Issues found: [count]
- Fixes applied: [list or "none"]

### PR
- Ready: yes/no
- Action: [created PR #N / awaiting confirmation / blocked by failures]
```
