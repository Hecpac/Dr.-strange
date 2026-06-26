# F4-B1 Recoverable Delivery State Machine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make F4-B1 deterministic delegation survive process crashes — one Telegram delivery yields exactly one logical autonomous task with recoverable, crash-consistent state, replacing the inert `f4b.delegation_reservation` token.

**Architecture:** The Telegram gate enqueues one real durable `f4b.delegation` job keyed by `resume_key = f4b-delegation:{session_id}:{message_id}` and returns a status-aware truthful ack. A registered, maintenance-aware `F4DelegationJobRunner` claims that job through JobService (so crash recovery = JobService claim/retry/stale-recovery) and calls a NEW additive `TaskHandler.ensure_autonomous_task_enqueued(...)` that idempotently materialises one `agent_tasks` row (deterministic `task_id`, `record_task_started` is already `ON CONFLICT(task_id) DO UPDATE`) plus one `coordinator.autonomous_task` job (`reserve(resume_key="coordinator:{task_id}")`). The existing `coordinator.autonomous_task` BackgroundJobRunner executes it. `start_autonomous_task` is NOT changed for other callers; no inline thread runs from intake; no rows are deleted (failures terminalize).

**Tech Stack:** Python 3.13, SQLite via RuntimeDb single-writer, `JobService` (`claw_v2/jobs.py`), `TaskHandler`/`TaskLedger`, daemon `BackgroundJobRunner` pattern (`claw_v2/daemon.py`), pytest + unittest.

**Guarantee delivered:** exactly one logical task/bootstrap identity per delivery, with crash recovery. NOT exactly-once browser/external-effect execution (that is F5 / the execution track).

---

## Verified preconditions (do not re-litigate; cited for the implementer)
- `agent_tasks.task_id` is `PRIMARY KEY`; `TaskLedger.record_task_started` is `INSERT ... ON CONFLICT(task_id) DO UPDATE` → idempotent on a deterministic `task_id` (`claw_v2/task_ledger.py:~241`).
- `JobService.reserve(resume_key, kind) -> (record, created)` elects one creator via the `resume_key` unique index (`claw_v2/jobs.py`).
- `JobService.claim_next(worker_id, kinds=...)`, `checkpoint(job_id, dict)`, `complete(job_id, result=)`, `fail(job_id, error=)` exist (`claw_v2/jobs.py:300+`).
- Runner pattern: `PendingVerificationReconciliationJobRunner` + `_run_background_job_runner_loop(BackgroundJobRunner(name, handler, interval))` (`claw_v2/daemon.py`).
- Crash recovery allowlist: `AUTONOMY_STALE_RUNNING_JOB_KINDS` (`claw_v2/main.py:137`); add `f4b.delegation`.
- The `coordinator.autonomous_task` BackgroundJobRunner (`daemon.py:187`) resumes from the `agent_tasks` record via `task_handler._resume_autonomous_record`; the bootstrap must leave a resumable record + the coordinator job.

## File Structure
- **Create** `claw_v2/f4_delegation.py` — `F4DelegationJobRunner` (claim → bootstrap → checkpoint linkage → complete/terminal-fail) + `F4_DELEGATION_JOB_KIND = "f4b.delegation"` + `f4b_delivery_task_id(delivery_key)` deterministic id helper.
- **Modify** `claw_v2/task_handler.py` — add additive `ensure_autonomous_task_enqueued(...) -> AutonomousTaskBootstrapResult` (dataclass). Do NOT change `start_autonomous_task`.
- **Modify** `claw_v2/bot.py` — gate enqueues the `f4b.delegation` job (replace `reserve`/`start_autonomous_task`/`delete`); status-aware truthful acks; remove `_f4_start_delegated_task` string parsing.
- **Modify** `claw_v2/jobs.py` — REMOVE `JobService.delete` (quarantine, not delete); the delivery job terminalizes instead.
- **Modify** `claw_v2/daemon.py` + `claw_v2/main.py` — register the runner; add `f4b.delegation` to the stale-recovery allowlist; ensure no generic/unfiltered runner claims it.
- **Modify** `claw_v2/INTERNAL_WIRING.md` — invariant rewrite (delivery job → runner → idempotent bootstrap; crash table; no-delete) + §5.1 row 8b.
- **Test** `tests/test_f4b_deterministic_delegation.py` (gate/intake/concurrency/truthful acks), `tests/test_f4_delegation.py` (NEW: bootstrap idempotency, runner, crash boundaries, maintenance).

---

### Task 1: Idempotent autonomous-task bootstrap API

**Files:** Modify `claw_v2/task_handler.py`; Test `tests/test_f4_delegation.py` (create).

- [ ] **Step 1: Failing test — deterministic id + bootstrap creates exactly one task + one coordinator job, idempotent on retry.**
```python
# tests/test_f4_delegation.py
import tempfile, unittest
from pathlib import Path
from tests.helpers import make_config
# build a TaskHandler with real TaskLedger + JobService on a temp DB (mirror tests/test_task_handler.py setup)
class BootstrapIdempotencyTests(unittest.TestCase):
    def test_bootstrap_is_idempotent_on_deterministic_task_id(self):
        th = self._handler()  # helper builds TaskHandler w/ real ledger+job_service+coordinator stub
        tid = "f4bdeliv:tg-1:111"
        r1 = th.ensure_autonomous_task_enqueued(task_id=tid, session_id="tg-1",
            objective="Revisa el feed de X", mode="chat", task_kind="authenticated_browse",
            source_text="Haz un repaso por X", delegation_metadata={"source": "f4_deterministic_delegation"})
        r2 = th.ensure_autonomous_task_enqueued(task_id=tid, session_id="tg-1",
            objective="Revisa el feed de X", mode="chat", task_kind="authenticated_browse",
            source_text="Haz un repaso por X", delegation_metadata={"source": "f4_deterministic_delegation"})
        self.assertEqual(r1.task_id, tid)
        self.assertEqual(r1.coordinator_job_id, r2.coordinator_job_id)  # same job
        self.assertTrue(r1.task_created); self.assertFalse(r2.task_created)
        self.assertTrue(r1.job_created); self.assertFalse(r2.job_created)
        self.assertEqual(r1.status, "started")
        self.assertIsNotNone(th.task_ledger.get(tid))                    # one agent_tasks row
        self.assertEqual(len(th.job_service.list(kinds=["coordinator.autonomous_task"], limit=50)), 1)
```
- [ ] **Step 2:** Run → FAIL (`ensure_autonomous_task_enqueued` undefined). `pytest tests/test_f4_delegation.py::BootstrapIdempotencyTests -v`
- [ ] **Step 3: Implement** the dataclass + method in `task_handler.py`. The method reuses `record_task_started` (idempotent upsert) and `reserve`; it does NOT mint a fresh `task_id`, does NOT start an inline thread, returns structured result.
```python
@dataclass(slots=True)
class AutonomousTaskBootstrapResult:
    task_id: str
    coordinator_job_id: str | None
    task_created: bool
    job_created: bool
    status: str   # "started" | "coordinator_unavailable" | "failed"
    reason: str

def ensure_autonomous_task_enqueued(self, *, task_id, session_id, objective, mode,
        task_kind, source_text, delegation_metadata):
    if self.coordinator is None:
        return AutonomousTaskBootstrapResult(task_id, None, False, False, "coordinator_unavailable", "coordinator unavailable")
    if self.job_service is None:
        return AutonomousTaskBootstrapResult(task_id, None, False, False, "failed", "job_service unavailable")
    existed_task = self.task_ledger.get(task_id) is not None
    # idempotent agent_tasks row (record_task_started is ON CONFLICT(task_id) DO UPDATE)
    self._record_ledger_task_started(task_id=task_id, session_id=session_id, objective=objective,
        mode=mode, route={}, goal_id=None,
        task_contract={"goal": objective, "source_message": source_text[:500],
            "task_kind": task_kind, "current_step": "task_started",
            "verification_requirement": "task ledger records terminal or pending state with evidence",
            "blockers": [], "plan": list(planned_phases_for_mode(mode))},
        verify=None)
    provider, model = self._provider_model_for_mode(session_id, mode)
    job, job_created = self.job_service.reserve(
        resume_key=f"coordinator:{task_id}", kind="coordinator.autonomous_task",
        payload={"task_id": task_id, "session_id": session_id, "objective": objective, "mode": mode},
        metadata={"runtime": "coordinator", "provider": provider, "model": model,
            "reason": "f4b_delegation_bootstrap", "delegation_metadata": dict(delegation_metadata or {})})
    return AutonomousTaskBootstrapResult(task_id, job.job_id, not existed_task, job_created, "started", "ok")
```
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Concurrent bootstrap test** (barrier, two threads, same task_id) → one task row, one coordinator job, both return same `coordinator_job_id`; loop 25×.
- [ ] **Step 6: Commit** `feat(f4b): additive idempotent ensure_autonomous_task_enqueued bootstrap`.

> Implementer note: confirm `_resume_autonomous_record` can resume from the row `record_task_started` writes (read `task_handler.py:1916`). If it requires goal/lifecycle artifacts absent here, add them idempotently INSIDE `ensure_autonomous_task_enqueued` keyed on `task_id` (upsert/if-absent). **If any such write cannot be made idempotent or reconciled, STOP and report** (per the operator's §8).

### Task 2: Durable `f4b.delegation` job + runner (claim → bootstrap → terminalize)

**Files:** Create `claw_v2/f4_delegation.py`; Test `tests/test_f4_delegation.py`.

- [ ] **Step 1: Failing test** — runner claims one queued `f4b.delegation` job, bootstraps exactly one task+coordinator job, links them in checkpoint, completes the delegation job. Re-run after marking the delegation job `retrying` (simulated reclaim) → no second task/coordinator job.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** `F4_DELEGATION_JOB_KIND = "f4b.delegation"`, `f4b_delivery_task_id(delivery_key)` (e.g. `"f4bdeliv:" + sha1(delivery_key)[:16]` — deterministic, stable), and `F4DelegationJobRunner` mirroring `PendingVerificationReconciliationJobRunner`: `run_available(limit=1)` → `reclaim_stale_running` (uses `job_service.recover_stale_running` or `claim_next` lease) → `claim_next(worker_id="f4b_delegation", kinds=(F4_DELEGATION_JOB_KIND,))` → read payload → `result = task_handler.ensure_autonomous_task_enqueued(...)` → on `status=="started"`: `job_service.checkpoint(job_id, {"task_id": result.task_id, "coordinator_job_id": result.coordinator_job_id})` then `job_service.complete(job_id, result=...)`; on `coordinator_unavailable`/`failed`: `job_service.fail(job_id, error=reason)` (terminal after max_attempts — NEVER delete).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Maintenance test** — with `CLAW_MAINTENANCE_MODE=1`, `claim_next` returns None → the delegation job stays `queued`/`retrying`, runner does nothing (assert no task created). (Reuse the JobService maintenance gate already exercised in `tests/test_jobs.py`.)
- [ ] **Step 6: Commit** `feat(f4b): durable f4b.delegation job + runner`.

### Task 3: Gate enqueues the delivery job + truthful status-aware acks

**Files:** Modify `claw_v2/bot.py`; Test `tests/test_f4b_deterministic_delegation.py`.

- [ ] **Step 1: Failing tests** — gate: (a) match → enqueue one `f4b.delegation` job (`reserve`, `created=True`) → ack "Registré tu pedido… lo proceso en una tarea de fondo" (queued, NOT "en marcha"); (b) duplicate while queued → `created=False` → read job status → "Esta misma solicitud ya fue aceptada; no generé otra."; (c) duplicate after the linked task is `running`/`completed`/`failed` → status-neutral truthful ack derived from the linked task state (never "ya está en marcha" unless the linked task is actually running); (d) flag OFF → None; (e) no message_id → None.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** the gate rewrite: `reserve(resume_key=delivery_key, kind=F4_DELEGATION_JOB_KIND, payload={session_id, message_id, task_id=f4b_delivery_task_id(delivery_key), objective, channel})`; `created` → "accepted/queued" ack; not created → inspect `existing` job status + (if linked) the `agent_tasks` status via `task_handler.task_ledger.get(existing.payload["task_id"])` to produce a truthful ack; remove `_f4_start_delegated_task`, the `start_autonomous_task` call, and the `job_service.delete` path. No inline thread.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Concurrency** — keep the barrier test; assert one `f4b.delegation` job, both callers truthful, exactly one is the dedup ack; loop 25×.
- [ ] **Step 6: Commit** `feat(f4b): gate enqueues durable delivery job with truthful acks`.

### Task 4: Crash-boundary tests (reopen DB / re-run runner)

**Files:** Test `tests/test_f4_delegation.py`.

For each window, persist state, drop & reopen `JobService`/`TaskHandler` on the SAME temp DB (simulating a process crash), then run the runner / redeliver. Prove: one delivery job, one `agent_tasks` row, one coordinator job, no duplicate scheduling, eventual terminal delegation state.
- [ ] before delivery-job commit → redelivery enqueues normally (one job).
- [ ] after delivery commit, before claim → reopen, run runner → claims, bootstraps once.
- [ ] after claim, before bootstrap → reopen, reclaim stale-running → bootstraps once.
- [ ] after bootstrap, before checkpoint/complete → reopen, reclaim → `ensure_*` re-run reads same `task_id`/coordinator job (idempotent), links, completes; no second of either.
- [ ] after delivery completion → redelivery finds terminal delivery job → no new task.
- [ ] **Run each crash test 25×.** **Commit** `test(f4b): crash-boundary recovery matrix`.

### Task 5: Remove inert reservation + destructive delete (quarantine, not delete)

**Files:** Modify `claw_v2/jobs.py`, `claw_v2/bot.py`, `tests/test_f4b_deterministic_delegation.py`.
- [ ] Remove `JobService.delete` and its tests (no broad unaudited delete). Remove the `f4b.delegation_reservation` kind/usages and the reservation/delete tests. Failures now terminalize the `f4b.delegation` job (audit preserved). Run the suite. **Commit** `refactor(f4b): drop inert reservation + delete; failures terminalize`.

### Task 6: Operational safety — runner registration + no generic pickup + recovery allowlist

**Files:** Modify `claw_v2/daemon.py`, `claw_v2/main.py`; Test `tests/test_architecture_invariants.py` or `tests/test_daemon.py`.
- [ ] Register exactly one `F4DelegationJobRunner` in the daemon loop (mirror the pending-verification runner wiring). Add `f4b.delegation` to `AUTONOMY_STALE_RUNNING_JOB_KINDS`. Add a test that **no other runner's `kinds` include `f4b.delegation`** and that a generic/unfiltered `claim_next` (no kinds filter) is not used by any registered runner for this kind. **Commit** `feat(f4b): register delegation runner; stale-recovery allowlist; exclusivity test`.

### Task 7: Docs + validation + fresh review

**Files:** Modify `claw_v2/INTERNAL_WIRING.md`; PR body.
- [ ] Rewrite the invariant `high_confidence_delegation_intents_do_not_depend_on_model_tool_choice`: delivery job → runner → idempotent bootstrap; deterministic `task_id`; crash-window table; truthful status-aware acks; failures terminalize (no delete); §5.1 row 8b. Remove all `idempotency_key` / inert-reservation / response-string-parsing references. Update the PR body to the final architecture.
- [ ] Run: `pytest tests/test_f4b_deterministic_delegation.py tests/test_f4_delegation.py tests/test_telegram.py tests/test_task_handler.py tests/test_jobs.py tests/test_config.py tests/test_brain_tooluse_ledger.py tests/test_brain_tooluse_verify.py tests/test_architecture_invariants.py -q`; loop the crash + concurrency nodes 25×; `ruff check`/`format --check`; `git diff --check origin/main...HEAD`.
- [ ] Dispatch a fresh `code-reviewer` agent focused on: transaction/idempotency boundaries, cross-process creator election, runner retry, deterministic task ids, duplicate session-state writes, orphan jobs, truthful acks, maintenance gating, unknown-worker pickup. Fix substantive findings with code+tests (no dismissals). **Commit** `docs(f4b): final recoverable-delivery contract`.

---

## Self-Review
- **Spec coverage:** intake job (T3) · runner (T2) · deterministic task_id (T1/T2) · bootstrap API (T1) · idempotent persistence via `ON CONFLICT(task_id)` + `reserve` (T1) · no inline thread (T3) · crash recovery (T4) · failure terminalize/no-delete (T5) · maintenance + exclusivity (T6) · truthful acks queued/dup/running/completed/failed (T3) · docs (T7) · fresh review (T7). All mandatory test groups mapped.
- **Placeholder scan:** the one open verification (does `_resume_autonomous_record` need more than the `record_task_started` row?) is called out in Task 1 with a STOP-and-report instruction — not a silent TODO.
- **Type consistency:** `AutonomousTaskBootstrapResult` fields (task_id, coordinator_job_id, task_created, job_created, status, reason) used identically in T1/T2/T3; `F4_DELEGATION_JOB_KIND="f4b.delegation"`, `resume_key` delivery=`f4b-delegation:{session}:{msgid}`, coordinator=`coordinator:{task_id}` used consistently.
