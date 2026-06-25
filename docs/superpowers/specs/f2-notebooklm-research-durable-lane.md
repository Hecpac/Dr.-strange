# F2 — NotebookLM Research Durable Lane (Design Spec)

Status: Design only, 2026-06-24. **Spec only — does NOT authorize implementation,
deploy, daemon work, primary-DB writes, Stage 2C2, or Stage 3 execution.** See
the **Gates** section at the end.

Scope owner context: builds on the already-merged F2.0 (durability schema) and
F2.1 (`F2DurabilityStore`). Aligns with `docs/architecture/f2_durability_design.md`
(the F2 design) and the §1 invariants in `claw_v2/INTERNAL_WIRING.md`. Assumes
F2 durability stays OFF in production until a separate Gate B / Stage 2C2
authorization.

---

## 1. Problem

NotebookLM research/artifact jobs execute their work in an ephemeral,
request-spawned `threading.Thread(daemon=True)` (`claw_v2/notebooklm.py`
`start_research`). The `agent_jobs` row is durable, but the executor is not:
a daemon restart (watchdog, deploy) or a CDP/external-backend hang mid-research
kills the thread before its `try/except/finally` can complete or fail the job,
orphaning the row in `running` forever. The generic stale-running recovery
deliberately excludes notebooklm kinds (no durable consumer), so orphans
accumulated indefinitely (observed: 3 rows stuck 16–19 days across multiple
restarts).

A **stopgap** is already shipped (commit `6ecd2ce`,
`notebooklm_stale_running_job_reconcile`): with F2 OFF, orphaned `notebooklm.*`
running rows are retired to terminal `failed` (never retried). The stopgap stops
the accumulation but does **not** make the work resumable, and re-running a
research that may have partially imported sources risks duplicate sources.

This spec designs the **durable lane**: notebooklm research that, on restart,
resumes safely **without duplicating** the external effect (imported sources),
using the F2.3 external-effect facility.

## 2. Goals / Non-goals

**Goals**
- A daemon restart mid-research never silently loses the research and never
  duplicates imported sources.
- On restart, the work resumes through a durable consumer, classifying the prior
  external effect as applied / absent / needs-review.
- A reusable, generic external-effect executor (F2.3) with notebooklm as its
  first consumer.
- Built behind feature flags; with the flags OFF, current behavior + the
  deployed stopgap remain unchanged.

**Non-goals**
- Enabling F2 durability in production (that is Gate B / Stage 2C2 — separate).
- Other `notebooklm.*` artifact kinds (podcast, audio, …) — they stay on the
  thread path + stopgap this round.
- Other external effects (GitHub push, social publish, …) — the executor is
  designed generic, but only notebooklm research is wired now.
- Coordinator F2.4 recovery-cursor flow — notebooklm research is a standalone
  job, not a multi-phase Coordinator task (see §6).

## 3. Success criteria

- **Never duplicate**: a crash in any window between intent and result never
  causes a second source-importing run without positive evidence the first
  did not apply.
- **Never silently lose**: an unresolved/ambiguous effect surfaces visibly
  (terminal-flagged job + observe event + Telegram notify), never a silent drop.
- **Flag-OFF parity**: with either flag OFF, `start_research` behaves exactly as
  today (thread path) and the stopgap remains the safety net.
- **Maintenance-aware**: the durable consumer claims through `JobService`, so the
  A2 maintenance gates (`CLAW_MAINTENANCE_MODE`) block pickup during a no-work
  window with no special-casing.

## 4. Components

Each unit has one purpose, a defined interface, and is testable in isolation.

### 4.1 `F2ExternalEffectExecutor` (generic)

`execute(effect_spec, adapter, verifier) -> EffectOutcome`

Owns the external-effect state machine on top of `F2DurabilityStore`.
Responsibilities:
- compute the `idempotency_key` from the `effect_spec`;
- look up any existing `external_effect_records` row for that key (dedup);
- record the **intent** row (committed) **before** calling the adapter;
- enforce the status-transition policy (§5);
- call the **adapter** only when the state machine says it is safe to apply;
- record the **result** row after the adapter returns;
- on recovery / ambiguous prior states, call the **verifier**;
- **never auto-replay** a `verified_applied` effect;
- **fail closed** to `blocked_manual_review` on any unresolved ambiguity.

The executor does not know anything notebooklm-specific. It is reusable for
future effects.

### 4.2 `NotebookLMResearchEffectSpec`

Provides the effect identity and request payload:
- `effect_kind = "notebooklm_research"`
- `target = notebook_id`
- `content_hash = sha256(query | mode | normalized request fields)`
- `request_json` includes: `notebook_id`, `query`, `mode`,
  `pre_intent_source_count`, and (if available) a status snapshot hash.
- `(task_id, run_id, phase, job_id)` derived from the job by the runner (§4.4,
  §6).

`pre_intent_source_count` is captured at intent time (from `status(notebook_id)`)
and is the verifier's baseline.

### 4.3 NotebookLM verifier

Pure classifier; no side effects beyond reading `status(notebook_id)`.
- Inputs: original request, `pre_intent_source_count`, adapter's returned
  `imported_count` (if a result was recorded), current `status(notebook_id)`.
- Outputs: `verified_applied` | `verified_absent` | `blocked_manual_review`.
- Policy: see §5 (conservative / fail-closed).

### 4.4 Durable runner

- Job kind: `notebooklm.research`.
- Daemon-registered background runner (same pattern as the wiki/perf runners),
  claims via `JobService.claim` / `claim_next` → **A2 maintenance gates apply**.
- Runs the research through `F2ExternalEffectExecutor`.
- Completes the `agent_jobs` job only after a safe terminal effect state
  (`applied` / `verified_applied`).
- On `blocked_manual_review`, transitions the job per §7 (terminal-flagged +
  notify), never retried, never silently dropped.

### 4.5 `start_research` routing

```
if not F2_DURABILITY or not CLAW_NOTEBOOKLM_RESEARCH_DURABLE:
    # current behavior — unchanged
    enqueue job + spawn thread (existing path); orphans handled by the stopgap.

if F2_DURABILITY and CLAW_NOTEBOOKLM_RESEARCH_DURABLE:
    enqueue job only (no thread); the durable runner handles the effect
    through the executor.
```

Two independent flags: the global F2 durability flag **and** a dedicated
`CLAW_NOTEBOOKLM_RESEARCH_DURABLE`. Both must be ON for the durable path. The
dedicated flag allows an independent canary of the notebooklm lane even after
F2 is globally enabled.

## 5. Verifier policy (conservative, fail-closed)

The `status()` source-count delta is inherently fuzzy: other operations can move
the count between intent and verification. Automatic decisions are made **only**
on a clear signal; everything else goes to a human.

- `verified_applied` (complete the job, do **not** re-run) ⟺ a `result_json` was
  recorded (the adapter returned before the crash) with `imported_count > 0`.
- `verified_absent` (safe to retry, reuse the same idempotency key) ⟺ current
  source count **==** `pre_intent_source_count` **and** no `result_json` recorded
  (clean no-op: nothing was imported).
- `blocked_manual_review` ⟺ everything else, including: the count changed but no
  result was recorded; a partial/ambiguous snapshot; `status()` unavailable.

Never auto-skip on a count delta alone (the increase may be from elsewhere).
Never auto-retry when anything moved. This minimizes both duplication and silent
loss, at the cost of more manual-review cases — an acceptable trade for an
irreversible external effect.

## 6. Data model & identity

Reuses the existing F2.0 `external_effect_records` table (no new tables). Key
columns used: `external_effect_id` (PK), `idempotency_key` (UNIQUE), `task_id`,
`run_id`, `job_id`, `phase`, `effect_kind`, `target`, `content_hash`,
`request_json`, `request_sha256`, `status`, `attempt_count`, `verifier_kind`,
`verification_json`, `result_json`, `result_sha256`, `error`, `schema_version`,
`created_at`, `updated_at`.

`idempotency_key = sha256(task_id || run_id || phase || effect_kind || target ||
content_hash || schema_version)` (per the F2 design).

**Standalone identity.** NotebookLM research is a standalone `agent_jobs` job,
not a Coordinator multi-phase task. The runner derives:
- `run_id = job_id` (so a re-claim of the **same** job after restart reuses the
  same idempotency key — the retry must not double-apply);
- `task_id` = the job's originating task metadata if present, else `job_id`;
- `phase = "research"` (an identity label, not a Coordinator phase).

It uses `external_effect_records` **only** — no `phase_checkpoints` and no
`phase_recovery_cursors` (those are Coordinator F2.2/F2.4 surfaces). Recovery for
this lane is driven by JobService re-claim + the effect-record lookup, which is
simpler than the cursor-driven Coordinator flow.

**Legitimate re-runs are not blocked.** A user re-requesting research on the same
notebook+query later creates a **new job** → new `run_id` → **different key** →
not deduped. Only the same job's crash-retry reuses the key.

## 7. Effect statuses & `blocked_manual_review` handling

Effect statuses (subset of the F2 design set, in use here): `intent_recorded` →
`apply_in_progress` → `applied` / `failed` → `verification_required` →
`verified_applied` / `verified_absent` / `blocked_manual_review`.

Job ↔ effect coupling:
- Effect `applied` / `verified_applied` → job `completed`.
- Effect `verified_absent` → job retried (same key; bounded by `max_attempts`).
- Effect `blocked_manual_review` → job to a **terminal flagged** state:
  `failed` with a distinct `error = "effect_blocked_manual_review"`, plus an
  `observe.emit("notebooklm_research_effect_blocked_manual_review", …)` event and
  a Telegram notification ("research for X is in review; it was not re-run").
  Never retried, never silently dropped.

## 8. Data flow (happy path + crash windows)

Mirrors the F2 design's external-effect transaction boundaries.

Happy path:
1. Runner claims the job (→ running). Reads `pre_intent_source_count` via
   `status()`.
2. Executor computes the key; records **intent** (`intent_recorded`); commit.
3. Executor calls the adapter (`deep_research`).
4. Executor records **result** (`applied`, `result_json` incl. `imported_count`);
   commit.
5. Runner completes the job. (No verifier on the happy path.)

Crash windows (resume = runner re-claims the job, executor loads the effect row
by key):
- **Crash before intent commit** → no durable effect row; the re-claimed job
  starts a fresh intent. Safe (nothing applied).
- **Crash after intent, before adapter** → state `intent_recorded`, no result →
  verifier (count unchanged, no result) → `verified_absent` → retry same key.
- **Crash after adapter, before result commit** → state `intent_recorded` /
  `apply_in_progress`, no result → verifier: count moved → `blocked_manual_review`
  (we cannot prove applied vs absent) → §7 handling.
- **Crash after result commit, before job complete** → state `applied` with
  result → verifier → `verified_applied` → complete the job, no re-run.

## 9. Flag gating & migration

| F2 durability | `CLAW_NOTEBOOKLM_RESEARCH_DURABLE` | Behavior |
| --- | --- | --- |
| OFF | (any) | Current thread path + stopgap (unchanged). |
| ON | OFF | Current thread path + stopgap (notebooklm not yet migrated). |
| ON | ON | Durable lane (enqueue-only + runner + executor). |

Migration is a flag flip, reversible. The stopgap remains the safety net in every
row where the durable lane is not active.

## 10. Maintenance-gate interaction (A2)

The durable runner claims through `JobService`, so with `CLAW_MAINTENANCE_MODE`
ON the claim gate blocks pickup — queued research jobs wait, consistent with the
no-work posture proven by the A4 preflight. No special-casing; the maintenance
invariant from this session's A2 work covers the new runner for free.

## 11. Testing strategy (TDD)

All work is test-first.
- **Unit — executor**: idempotency key stability; dedup of an existing key;
  intent-before-adapter ordering; never-auto-replay of `verified_applied`;
  fail-closed to `blocked_manual_review`.
- **Unit — verifier**: each branch of §5 (applied / absent / manual_review),
  including the ambiguous count-moved-without-result case.
- **Integration — runner**: claim → run → complete on the happy path; A2
  maintenance gate blocks pickup; `blocked_manual_review` → terminal-flagged job
  + notify.
- **Crash-resume synthetics**: one per §8 window, asserting both the resumed
  behavior and the absence of a duplicate external effect (mirrors the existing
  `tests/test_f2_external_effect_synthetics.py` style).
- **Flag gating**: with either flag OFF, `start_research` takes the thread path
  unchanged and no `external_effect_records` row is written.

## 12. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Verifier false "applied" → research silently skipped | Conservative policy (§5): only `result_json` proves applied; count delta alone never auto-skips. |
| Verifier false "absent" → duplicate import | Only count-unchanged + no-result is "absent"; any movement → manual review. |
| Executor becomes a second DB writer | All effect writes go through `F2DurabilityStore` (RuntimeDb-owned); enforced by the architecture-invariant suite. |
| Durable runner re-runs a stale request on enablement | Bounded by `max_attempts`; effect recovery verifies before any retry; manual-review on ambiguity. |
| Hidden coupling to Coordinator F2.4 | This lane uses `external_effect_records` only, not cursors/phase checkpoints (§6). |

## 13. Open questions (resolve during planning)

- Exact `request_json` "status snapshot hash" contents (optional; only if
  `status()` exposes a stable shape).
- Whether `verified_absent` retries should carry a small backoff or run on the
  next runner tick.
- Whether the Telegram notify in §7 should be rate-limited/coalesced if multiple
  effects block in one window.

---

## Gates

**This spec does not authorize implementation nor exposure to real traffic.**

- **Implementation requires a separate explicit gate.** No runtime code, daemon
  work, or primary-DB writes follow from this document.
- **Stage 2C2 (F2 durability enablement / canary) is out of scope** and remains
  gated separately; the durable lane stays inert until both flags are ON.
- **Stage 3 requires explicit, separate authorization.**

Until those gates are granted, the shipped stopgap (`6ecd2ce`) is the only active
behavior for orphaned notebooklm jobs, and F2 durability stays OFF in production.
