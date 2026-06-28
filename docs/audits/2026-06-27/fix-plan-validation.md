# Fix-plan validation — long-tail remediation (working tree)

**Question:** does the uncommitted 2026-06-27 plan actually close the 5 findings `fix-verification.md` marked NOT_FIXED at HEAD, and do the tests *prove* it?
**Baseline:** HEAD `136097c` (= pre-fix; the plan's changes are all in the working tree, uncommitted).
**Method:** 13-agent workflow (`workflows/verify-audit-fix-plan.js`) — per checkpoint: static review → focal-gate run → adversarial + logical-differential verify; plus cross-cutting gates + a plan-claims honesty audit; then synthesis. Read-only (no commit/staging); focal gates only (full suite forbidden — live-daemon restart risk).

## Verdict: 5/5 VERIFIED · gates PASS · claims honest → **SHIP, no blockers**

| CP | Finding | Verdict | Conf | Proof the test would fail pre-fix |
|----|---------|---------|------|-----------------------------------|
| **C1** | M1 durable retention | ✅ VERIFIED | high | `git show HEAD` has no `prune_terminal` / `durable_retention` job; tests assert delete-old + survive-recent + status-filter + `max_rows` cap (not empty→0) |
| **C2** | L_subprocess (kairos) | ✅ VERIFIED | high | AST contract reproduced: pre-fix offenders `[1132,1139,1186,1326,1468]` (FAIL) → working tree `[]` (PASS); no residual `subprocess` token |
| **C3** | L_codex_redact | ✅ VERIFIED | high | pre-fix emits raw `sk-…`; both new tests assert secret ABSENT + `[REDACTED]` PRESENT → fail pre-fix by construction |
| **C4** | D2 dedupe race | ✅ VERIFIED | high | event-barrier 2-thread differential: pre-fix lockless path fails `<=1` **5/5**; post-fix locked path passes **5/5** (deterministic, not racy) |
| **C5** | L_reconcile | ✅ VERIFIED | high | per-step `jobs.list.call_count = 1,1,2,2`; pre-fix unguarded code calls 3× → test's `==2` FAILs; reconciler body byte-identical |

**Focal gates (all green, working tree):** test_jobs 40 · test_task_ledger 15 · test_latency_audit_group3 11 · test_kairos 57 · test_kairos_health_check 8 · test_subprocess_runner 10 · test_codex_adapter 19 · test_operational_alerts 11 · test_daemon 41 · **test_architecture_invariants 39 (+22 subtests)** · test_lifecycle 16.

## The one flagged risk — resolved (C3)

The pre-review concern: old `result.stderr.strip()[:200]` → new `str(redact_sensitive(..., limit=200))`; if `limit=` didn't truncate, `artifacts["stderr"]` would regress from bounded to **unbounded** (no outer cap, unlike `_format_cli_detail`'s `detail[:500]`).

**Empirically false.** `redact_text` (redaction.py:96-102) truncates after substitution: probe `redact_sensitive('A'*1000+' sk-…', limit=200)` → **len 212** (= limit + 12-char `…[truncated]` marker), token absent. Position-independence probes confirm real substitution over the full string (token at offset 1000 still removed), not a slice artifact. No exposure regression.

## Architecture-invariant check (C1's new on-tick handler)

The new **synchronous** `durable_retention_prune` scheduler handler runs inline in `daemon.tick`. The tripwire `test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick` sweeps every registered job under router.ask / auto_research / sub_agents / subprocess.run sentinels — the new handler is in that set and trips none. **Correct, not vacuous:** it only does env-int parsing + two bounded SQLite DELETEs (`prune_terminal` holds `self._lock`, commits, no subprocess/LLM) — same sanctioned class as the existing inline `observe_prune`. Core Invariant 1 forbids *heavy* work on-tick, not bounded local DB writes.

## Plan-claims honesty audit — all true

- `git diff --check` clean (exit 0).
- No commit, no staging: 15 files ` M` (unstaged), staging empty, HEAD still `136097c`.
- Changed-file set matches the plan 1:1 (7 sources + 8 tests); untracked artifacts (`.patch-backups/`, `docs/audits/2026-06-27/*`, `memory/2026-06-*.md`) intact.
- The plan's *aggregate* counts (257/267) were **not** independently re-run (full suite forbidden) — the plan disclosed this; focal per-file counts all re-verified and matched.

## Non-blocking findings (nits / minor)

- **C2 (minor):** AST contract catches `subprocess.run` only — not `Popen`/`call`/`check_output` or aliased imports. Matches the plan's literal claim; broader `test_architecture_invariants` covers Popen/shell/os.system across all of `claw_v2/`. Optional: widen the kairos contract.
- **C3 (nit):** `artifacts["stderr"]` bound is now 212 (was hard 200) — the 12 extra chars are the truncation marker. Redaction is pattern-threshold dependent (`sk-` needs ≥20 trailing chars; tests use a valid 29-char token).
- **C4 (nit):** the `==now` rollback-clobber edge is reachable only at `cooldown_seconds==0`; no shipped rule uses 0 (min 60) → unreachable.
- **C1 (nit):** handler re-reads env each tick (intended: env changes take effect without restart). `DURABLE_RETENTION_DAYS=0` is a config footgun (cutoff=now; still `max_rows`-capped); default 30 is safe.

## Recommended next steps (when ready to land)

1. Commit the 15 files on a **non-protected branch** (`main` is git-commit-blocked per `136097c`); do **not** stage the untracked audit/workflow artifacts.
2. Same commit: update `describes_commit` / `last_verified` in `claw_v2/INTERNAL_WIRING.md` for the `daemon.tick` / `_reconcile_orphaned_jobs` change (C5) and the new on-tick `durable_retention_prune` handler (CLAUDE.md same-commit rule).
3. Optional hardening: broaden the kairos AST contract (Popen/call/check_output/aliased); doc note on the `DURABLE_RETENTION_DAYS=0` footgun.

*Regenerable via `./workflows/verify-audit-fix-plan.js`. Evidence class: per-finding correctness re-verified empirically against the working tree; OPERATIONAL/live-runtime not exercised (no daemon restart, no Telegram).*
