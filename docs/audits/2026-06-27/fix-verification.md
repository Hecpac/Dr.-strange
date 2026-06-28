# Fix verification — acceptance gate

**Question:** of the audit/investigation findings confirmed at `610bfea`, which were actually **remediated** by the 8 commits landed since?
**Baseline:** `610bfea` (findings confirmed present) → **HEAD `10219e7`** (8 commits later).
**Method:** 9 read-only `Explore` agents; the H1 fix verified empirically by running the 3 tests (isolated temp DB); adversarial (a "harden"/"add checks" commit message is not accepted as proof).

## Tally: 22 findings checked → **FIXED 3 · PARTIAL 3 · NOT_FIXED 16**

> **Scope of this gate (read first):** this verifies **finding-resolution status** — "is each audit finding resolved at HEAD?" — plus the 3 H1 tests and `ruff --check`. It is **NOT a regression/correctness pass on the 8 commits.** The full suite was not run (live-daemon-restart risk; would need an isolated checkout), and the commits' changes *beyond* each finding were not reviewed (`7696c59` also touched `f2_recovery.py`/`task_ledger.py`; `9390bc4` added an `agent_jobs` migration; `8858671` changed parallel dispatch ordering). So "FIXED" = the finding is gone, not "the commit is correct and regression-free." The `REGRESSED` verdict was available but never exercised against the diffs.

---

## ✅ FIXED (3)

| ID | Finding | Fix commit | Evidence |
|---|---|---|---|
| **H1** | 3 `test_bot.py` tests red (coordinator.run not called) | `7696c59` | **All 3 now pass (ran them).** Added `_configured_f2_durability_store()` (`task_handler.py:95-104`) + new test. ⚠️ **caveat below.** |
| **I_toolcontract** | Browser* tools had no `success_condition` | `4ee49c2` | All 3 now have `success_condition`; tier-3 (Click/Type) also got `preflight`; tests assert no `ToolContractWarning` |
| **V2** | 15 files needed `ruff format` | `7696c59`+`8858671`+`4ee49c2`+`b41bd68` | `ruff format --check` now reports **0** files; cleaned incrementally |

**⚠️ H1 caveat — the fix is the opposite of what was recommended.** The remediation special-cases test doubles in **production** code: `if store_type.__module__ == "unittest.mock": return None` (`task_handler.py:102-103`). It makes the 3 tests green and adds a regression test, with no other-test regressions. But production logic now branches on whether an injected dependency is a mock — a code smell (the recommended fix was to configure `f2_durability_store=None` in the test fixtures, keeping production unaware of test doubles). Functionally resolved; design choice worth a second look. **Concrete consequence:** because the guard treats *any* `unittest.mock` store as "no store," every mock-coordinator test now bypasses the F2 recovery path — so the fail-closed logic added in `a89096e` loses its mock-based test coverage. **Note:** "main fully green" is OPERATIONAL, not verified — the full suite was not re-run here (daemon-restart risk).

---

## 🟡 PARTIAL (3)

| ID | Finding | Fix commit | Why PARTIAL |
|---|---|---|---|
| **F3** | No formal per-task lease | `9390bc4` | Lease mechanism **fully built + CAS-tested** (`jobs.py` lease_owner/expires/generation, heartbeat/release/acquire), wired into `claim_next`. But **behind default-OFF flag** (`formal_job_leases_enabled=False`) and commit warns "must remain disabled until runners propagate lease end-to-end" → built, not active. |
| **F6** | Dynamic fan-out not built | `8858671` | Commit titled "Implement deterministic F6 fan-out" actually adds **shadow metadata / observability only** — no planner node, no dynamic task generation; coordinator still fixed-phase; behind `langgraph_shadow_enabled=False`. The message oversells; functionally F6 is still not built. |
| **HF1** | operator in `allowed_contexts` for Browser click/type | none | Security is actually **fine**: `requires_human=true` gate provably blocks operator self-approval (needs a human token; tests confirm). Only the architecture is inelegant (operator still listed). Downgrade from "hole" to "cosmetic". |

---

## 🔴 NOT_FIXED (16) — still open at HEAD

Doc hygiene: **M5** (AUDIT_CLOSURE still "OPEN — 3 blockers"), **M2** (#11 default-off undocumented, no order test), **L_xfail** (§5.1 still cites removed xfail), **L_runbook** (still "F2 design-only"), **M3** (4 invariants still no `enforced_by`), **L_tripwire5** (5 symbols still unguarded), **M6** (PRD bot.py target stale, no system-arch doc).

Security/runtime tail: **L_subprocess** (kairos.py keychain still raw `subprocess.run`), **L_codex_redact** (`_format_cli_detail` still no `redact_sensitive`), **D2** (`_last_sent` still no Lock), **L_reconcile** (`_reconcile_orphaned_jobs` still no rate-limit), **L_backoff** (still no inter-retry sleep), **L_acks_purge** (still purge-on-write only).

Capability/verification: **D3** (notebook verification still key-presence only — no judge lane), **D4** (morning brief still no re-entrancy guard; `b41bd68` only changed wording), **M1** (still zero `DELETE FROM agent_jobs/agent_tasks`; `9390bc4` added a lease-expiry index but no prune — and F4-B1 keeps feeding this table).

---

## Read

- The 8 commits resolved the **highest-severity actionable** (H1, main-red) and two contract/hygiene items (browser contracts, ruff), and built solid lease + shadow-metadata infrastructure that isn't switched on yet (F3, F6 — both default-OFF).
- The **entire long tail** (security latent, retention, doc hygiene, D3/D4) is untouched. None are Critical, but L_subprocess (API key → stdout) and M1 (unbounded growth, now fed by F4-B1) are the two worth scheduling.
- One judgment call to revisit: the H1 fix teaches production code about `unittest.mock` — green but arguably the wrong layer.

*Raw verdicts (with file:line + quotes): regenerable via `./workflows/verify-audit-fixes.js`.*
