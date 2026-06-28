# Audit verification — Dr.-strange / Claw v2

**Method:** 15 read-only `Explore` agents (no Edit/Write capability) + one operator-run isolated test gate.
**Audit baseline:** `6eb6ab9`. **Verified against:** working tree at `610bfea` (**59 commits ahead** of the audit + uncommitted mods).
**Coverage:** all 31 audit claim IDs received a verdict (D5 split into D5a–D5e + L_D6, all CONFIRMED). Read-only honored: only `ruff --check`, 2 targeted test files, and `git` reads were executed; nothing written to the repo.

The real question this answers is not "true/false" but **"is each finding still actionable at HEAD `610bfea`?"** — so each verdict carries a drift attribution.

---

## Headline: the audit's overall verdict holds, with 5 corrections

The audit's top-line is **CONFIRMED, with the evidence class labeled**:
- **Independently re-verified this pass (code-cited):** the 3 April blockers D.1 / D.3 / D.5 are genuinely closed in live code; `main` is red.
- **Covered transitively:** the AST-enforced subprocess / no-`shell=True` invariants — via the passing tripwire (V3, 39/22).
- **Audit-asserted, NOT independently re-checked here:** triple-AND tool gating, redaction-on-every-observe-event, anti-injection quarantine, sandbox. These were not in this pass's 15 groups. Note M3 shows `triple_and_gating` and `audit_trail` have *no* tripwire guard (code-review-only defense) — so "intact" for these rests on the audit's read, not a fresh check. A follow-up pass that exercises each gate (each factor failing alone) would be needed to call them verified.

Five items where the audit is **wrong, overstated, or already fixed** (do not action as written):

| # | Audit said | Reality at HEAD | Disposition |
|---|---|---|---|
| **H1 root-cause** | "could be a real regression — lost tasks no longer restart coordinator" OR stale tests | **Stale tests / mock artifact, NOT a regression.** `MagicMock` coordinator auto-creates a truthy `f2_durability_store`, entering the F2 no-run path; production has it `None` → `coordinator.run()` is called (`task_handler.py:870-872, 917-918`). | Fix the test fixtures, not the resume path. Main-red severity still stands. |
| **M2** | §5.1 stale "because refactored to route-table"; nlm #16 default-OFF | Route-table migration is **incomplete** (`_build_pre_brain_routes` has only 2 routes) → §5.1's table is actually accurate. nlm #16 is **default-ON** (`CLAW_DISABLE_NLM_NATURAL_LANGUAGE="0"`), not OFF. | Audit substantially wrong. Real gap is narrower (#11 default-off undocumented; no order test). |
| **L_describes_commit** | INTERNAL_WIRING anchors stale (~5 behind) | **FIXED since audit** — doc_version 2.41, `last_verified` 2026-06-26, describes_commit = F4-B1, ~1 commit behind HEAD. | Not actionable (REFUTED). |
| **M5** | AUDIT_CLOSURE "broken anchors (bot.py 440-450)" | Lines 440-450 **exist and reference valid code**; the anchor isn't broken. Core staleness (dated 2026-04-26, status OPEN for now-closed blockers) is true. | Anchor sub-claim overstated; core finding stands. |
| **V4** | "1 of the 3 fails because verification doesn't complete in the test timeout" | All 3 fail **fast (~2.6s)** — not a timeout; failure is `coordinator.run` called 0 times. "3625 passed" total is unverifiable read-only (full suite forbidden — daemon restart risk). | Timeout speculation wrong; counts drifted. |

---

## Full verdict table (31 findings)

### Validation
| ID | Verdict | Drift | Note |
|---|---|---|---|
| V1 ruff check passes | CONFIRMED | STILL_TRUE | "All checks passed" |
| V2 ruff format: 15 files | CONFIRMED | STILL_TRUE | Exact same 6 runtime + 9 test files |
| V3 tripwire 39/22 | CONFIRMED | STILL_TRUE | 39 passed / 22 subtests, identical |
| V4 full suite 3F/3625P | PARTIAL | NUMBERS_DRIFTED | 3 failures confirmed; total unverifiable read-only; timeout guess wrong |

### April blockers
| ID | Verdict | Drift | Note |
|---|---|---|---|
| D1 acks TOCTOU closed | CONFIRMED | STILL_TRUE | fcntl.flock + mkstemp→fsync→os.replace + busy_timeout=5000 + purge-on-write all present; file unchanged since audit |
| D2 alerts dedupe race (open) | CONFIRMED | STILL_TRUE | `_last_sent` read/write unguarded (lines 120/128), no Lock |
| D3 notebook bypass closed (nuance) | CONFIRMED | STILL_TRUE | Task recording works; **verification is pure key-presence** — a well-formed but false review passes. No judge lane. |
| D4 brief cron overlap (was unverified) | CONFIRMED | STILL_TRUE | **Now closed:** only guard is stamp-file + scheduler interval; no lock/single-flight. Low risk under sync tick, real if path goes async/multi-instance. |
| D5 daemon liveness closed | CONFIRMED ×5 | STILL_TRUE | /health (web_transport:139), atomic sink (liveness.py), heartbeat (lifecycle.py), diagnostics consumes + flips critical, tripwire present |
| L5 compaction closed | CONFIRMED | STILL_TRUE | `USE_COMPACTION` default True; auto-triggered via store_message |
| L6 coordinator retry bounded | CONFIRMED | STILL_TRUE | 2 attempts + circuit breaker |

### High
| ID | Verdict | Drift | Note |
|---|---|---|---|
| H1 3 tests red on main | CONFIRMED | STILL_TRUE | **Root cause = stale tests (mock artifact), not regression** — see headline |

### Medium
| ID | Verdict | Drift | Note |
|---|---|---|---|
| M1 no agent_jobs/agent_tasks retention | CONFIRMED | STILL_TRUE | Zero DELETE FROM at HEAD (a delete() was added 135f819 then removed cf9478e post-audit) |
| M2 §5.1 dispatch stale | PARTIAL | NUMBERS_DRIFTED | See headline — substantially wrong |
| M3 4 invariants no enforced_by | CONFIRMED | STILL_TRUE | triple_and_gating, audit_trail, no_silent_degrade, kairos_external_mutation_gated all lack it |
| M4 bot.py god-module | CONFIRMED | NUMBERS_DRIFTED | Now 11,575 / 3,802 / 3,306 (was 11,399 / 3,771 / 3,164) |
| M5 AUDIT_CLOSURE stale | PARTIAL | STILL_TRUE | Core stale; "broken anchor" overstated |
| M6 BMad/PRD gap | CONFIRMED | STILL_TRUE | PRD targets bot.py ~200 lines vs 11,575; docs/architecture/ has only f2_durability_design.md; ADR log = 1 entry |

### Low
| ID | Verdict | Drift | Note |
|---|---|---|---|
| L_D6 web_transport.stop no escalate | CONFIRMED | STILL_TRUE | join(timeout=5)+warning; daemon=True thread, acceptable |
| L_backoff no inter-retry sleep | CONFIRMED | STILL_TRUE | `continue` with no time.sleep (bounded by cap) |
| L_deadcode RetryStuckPolicy | CONFIRMED | STILL_TRUE | Defined, test-only, no runtime instantiation |
| L_subprocess 5 raw subprocess.run | CONFIRMED | STILL_TRUE | **kairos.py:1326 reads HEYGEN_API_KEY into stdout** via raw subprocess.run; bypasses bounded runner |
| L_codex_redact | CONFIRMED | STILL_TRUE | `_format_cli_detail` (codex.py:278) surfaces stderr/stdout, no redact_sensitive; latent |
| L_acks_purge | CONFIRMED | STILL_TRUE | Purge-on-write-only; expired persist if no new acks |
| L_reconcile N+1/tick | CONFIRMED | STILL_TRUE | `_reconcile_orphaned_jobs` ~101 ops/tick, no rate-limit (vs `_reconcile_stale_tasks` which is gated 300s) |
| L_xfail doc ref | CONFIRMED | STILL_TRUE | §5.1 still says "xfail strict"; 0 xfails in test_dispatch_routing.py |
| L_tripwire5 | CONFIRMED | STILL_TRUE | NON_TOOL_LANES, CRITICAL_TASK_KINDS, DAEMON_AUTO_APPROVE, SECRET_PATH_PATTERNS, _DAEMON_REASON exist but tripwire references none |
| L_describes_commit | REFUTED | FIXED_SINCE | See headline — already current |
| L_runbook | CONFIRMED | (code fixed) | OPERATIONS_RUNBOOK still says "F2 remains design-only" (contradicts merged F2.0/F2.1); stale code-version examples. Doc fix still actionable. |

### Info
| ID | Verdict | Drift | Note |
|---|---|---|---|
| I_toolcontract Browser* no success_condition | CONFIRMED | STILL_TRUE | Warnings fired live during the H1 test run; "hard error in F4" |
| I_claudemd diff additive | CONFIRMED | STILL_TRUE | +20/-0; new §7 reinforces persona-vs-dev; no safety rule removed |

---

## Prioritized, still-actionable at HEAD `610bfea`

1. **Green `main`** (H1) — the 3 `test_bot.py` tests assert the **pre-F2 contract** (coordinator always reruns on resume). Production behavior is correct; the tests are stale. Updating them is the fix — decision for Hector: **(a)** force `coordinator.f2_durability_store = None` on the mock so the resume path matches production-with-F2-off, or **(b)** assert the new F2-recovery-first contract directly. Either way it's a test-only change; no production code. A CI smoke on these 3 would stop silent re-reddening.
2. **`docs/AUDIT_CLOSURE.md`** (M5) — re-emit or banner: D.1/D.3/D.5 are closed; it currently reads "OPEN — 3 blockers," which is actively misleading.
3. **Security latent** (L_subprocess) — migrate the kairos.py keychain read (HEYGEN_API_KEY → stdout) to the bounded runner; same for the other 4 raw `subprocess.run` callsites.
4. **D3 verification depth** — key-presence-only profile lets a false notebook review close as `passed`; consider a judge lane for the review profile.
5. **Retention** (M1) — add an off-tick prune for terminal `agent_jobs`/`agent_tasks` mirroring `observe_prune`, so daily VACUUM can reclaim.
6. **Doc hygiene** — fix M2 (correctly: document #11 default-off + add an order test; the route-table claim is wrong), L_xfail, L_runbook, M3 enforced_by, L_tripwire5, M6 PRD refresh.
7. **Chore** — `uvx ruff format` to clear the 15-file baseline (V2).

Not actionable: L_describes_commit (already current).
