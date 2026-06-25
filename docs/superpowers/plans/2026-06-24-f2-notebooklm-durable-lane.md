# F2 NotebookLM Research Durable Lane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make notebooklm research durable and dedup-safe across daemon restarts, built behind the `F2` + `CLAW_NOTEBOOKLM_RESEARCH_DURABLE` flags, inert until both are ON.

**Architecture:** A generic `F2ExternalEffectExecutor` orchestrates the
`external_effect_records` state machine on `F2DurabilityStore` (intent →
apply_in_progress → applied/blocked, verifier-driven recovery). NotebookLM
research is its first consumer via an effect spec + adapter wrapper + verifier. A
daemon-registered durable runner claims `notebooklm.research` jobs (so A2
maintenance gates apply) and drives the executor. `start_research` routes to the
durable path only when both flags are ON; otherwise the current thread path +
the shipped stopgap (`6ecd2ce`) remain.

**Tech Stack:** Python 3.13, `F2DurabilityStore` (RuntimeDb-owned), `JobService`,
`ClawDaemon.register_background_job_runner`, `pytest`/`unittest`.

**Source spec:** `docs/superpowers/specs/f2-notebooklm-research-durable-lane.md`
(commits `a86c382` + `6eb6ab9`). Read it before starting.

**Gate:** Implementation is authorized. Enabling F2 (Stage 2C2 / Gate B) and
Stage 3 remain separately gated; ship with both flags OFF.

---

## File Structure

- Create: `claw_v2/external_effect_executor.py` — generic `F2ExternalEffectExecutor`,
  `EffectSpec`, `AdapterResult`, `VerifierVerdict`, `EffectOutcome`. One purpose:
  own the external-effect state machine; no notebooklm knowledge.
- Create: `claw_v2/notebooklm_research_effect.py` — `build_research_effect_spec`,
  `notebooklm_research_adapter` (wraps `deep_research`, classifies imported_count),
  `notebooklm_research_verifier` (recovery classifier via `status()` delta).
- Create: `claw_v2/notebooklm_research_runner.py` — `NotebookLMResearchRunner`:
  claims `notebooklm.research`, builds the spec from the job, runs the executor,
  maps `EffectOutcome` → JobService completion/retry/fail.
- Modify: `claw_v2/config.py` — add `notebooklm_research_durable` flag
  (`CLAW_NOTEBOOKLM_RESEARCH_DURABLE`, default False).
- Modify: `claw_v2/notebooklm.py:395` `start_research` — two-flag routing
  (enqueue-only vs current thread path).
- Modify: `claw_v2/main.py` — register `notebooklm_research_runner` near the
  existing stale-recovery runners, gated by `config.f2_durability_enabled and
  config.notebooklm_research_durable`.
- Test: `tests/test_external_effect_executor.py`
- Test: `tests/test_notebooklm_research_effect.py`
- Test: `tests/test_notebooklm_research_runner.py`

## Shared contracts (define once in `external_effect_executor.py`)

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from claw_v2.f2_durability_store import ExternalEffectRecord, F2DurabilityStore

@dataclass(frozen=True, slots=True)
class EffectSpec:
    task_id: str
    run_id: str
    phase: str
    effect_kind: str
    target: str
    request: dict[str, Any]          # persisted as request_json (notebooklm: incl. pre_intent_source_count)
    content_hash: str
    job_id: str | None = None
    verifier_kind: str | None = None
    max_attempts: int = 3            # effect-level apply attempts before fail-closed

@dataclass(frozen=True, slots=True)
class AdapterResult:
    applied: bool                    # True -> effect applied; False -> needs review (e.g. zero imports)
    result: dict[str, Any]           # notebooklm: {"imported_count": int}
    reason: str | None = None

@dataclass(frozen=True, slots=True)
class VerifierVerdict:
    classification: str              # "verified_applied" | "verified_absent" | "blocked_manual_review"
    verification: dict[str, Any]
    reason: str

@dataclass(frozen=True, slots=True)
class EffectOutcome:
    status: str                      # applied | verified_applied | verified_absent | blocked_manual_review
    record: ExternalEffectRecord
    should_retry: bool               # True only for verified_absent within attempt budget

# adapter: (EffectSpec) -> AdapterResult, may raise
Adapter = Callable[[EffectSpec], AdapterResult]
# verifier: (EffectSpec, ExternalEffectRecord) -> VerifierVerdict
Verifier = Callable[[EffectSpec, ExternalEffectRecord], VerifierVerdict]
```

**Executor decision table** (entry status of the existing/just-recorded record):
- fresh `intent_recorded`, `attempt_count == 0` → **APPLY**.
- `verified_absent` → **APPLY** (controlled retry: transition
  `apply_in_progress` + `increment_attempt_count`, then adapter — per spec patch 1).
- `intent_recorded`/`apply_in_progress` with `attempt_count > 0` and no
  `result_json` (interrupted attempt) → **RECOVER** (verifier).
- `applied` → outcome `applied` (already terminal-safe; complete the job).
- `verified_applied` → outcome `applied` (idempotent re-entry; never auto-replay).
- `blocked_manual_review` → outcome `blocked_manual_review`.
- any state with `attempt_count >= max_attempts` entering APPLY → `blocked_manual_review`.

**APPLY** = `update_external_effect_status(eid, status="apply_in_progress", increment_attempt_count=True)`;
call `adapter`; on `AdapterResult.applied` → `status="applied"` (store result) →
outcome `applied`; on `applied is False` → `status="blocked_manual_review"` →
outcome blocked; on adapter **raise** → record `error` (status stays
`apply_in_progress`) and re-raise (the runner's next job attempt re-enters →
RECOVER).

**RECOVER** = call `verifier` → `update_external_effect_status` to the verdict's
classification (store `verification`, `verifier_kind`); `verified_applied` →
outcome applied; `verified_absent` → loop to APPLY; `blocked_manual_review` →
outcome blocked.

---

## Phase 1 — Generic executor

### Task 1.1: Contracts + executor skeleton

**Files:**
- Create: `claw_v2/external_effect_executor.py`
- Test: `tests/test_external_effect_executor.py`

- [ ] **Step 1: Write the failing test (happy path → applied)**

```python
import tempfile
from pathlib import Path
import unittest
from claw_v2.sqlite_runtime import RuntimeDb
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.external_effect_executor import (
    F2ExternalEffectExecutor, EffectSpec, AdapterResult, VerifierVerdict,
)

def _store(tmp):
    db = RuntimeDb(Path(tmp) / "claw.db")
    return F2DurabilityStore(db), db

def _spec(**kw):
    base = dict(task_id="t1", run_id="r1", phase="research",
                effect_kind="demo_effect", target="nb1",
                request={"q": "x"}, content_hash="ch1", job_id="job:1",
                verifier_kind="demo")
    base.update(kw)
    return EffectSpec(**base)

class ExecutorHappyPathTests(unittest.TestCase):
    def test_happy_path_records_intent_then_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            calls = []
            def adapter(spec):
                calls.append(spec.run_id)
                return AdapterResult(applied=True, result={"imported_count": 3})
            def verifier(spec, record):
                raise AssertionError("verifier must not run on happy path")
            outcome = ex.execute(_spec(), adapter, verifier)
            self.assertEqual(outcome.status, "applied")
            self.assertEqual(calls, ["r1"])
            self.assertEqual(outcome.record.status, "applied")
            self.assertEqual(outcome.record.attempt_count, 1)
            self.assertFalse(outcome.should_retry)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/hector/Projects/Dr.-strange/.venv/bin/python -m pytest tests/test_external_effect_executor.py -q`
Expected: FAIL — `ModuleNotFoundError: claw_v2.external_effect_executor` (feature missing).

- [ ] **Step 3: Write minimal implementation (contracts + APPLY path)**

Create `claw_v2/external_effect_executor.py` with the dataclasses from "Shared
contracts" above and:

```python
class F2ExternalEffectExecutor:
    def __init__(self, store: F2DurabilityStore) -> None:
        self._store = store

    def execute(self, spec: EffectSpec, adapter, verifier) -> EffectOutcome:
        record = self._store.record_external_effect(
            task_id=spec.task_id, run_id=spec.run_id, phase=spec.phase,
            effect_kind=spec.effect_kind, target=spec.target, request=spec.request,
            content_hash=spec.content_hash, job_id=spec.job_id,
            verifier_kind=spec.verifier_kind, status="intent_recorded",
        )
        return self._drive(spec, record, adapter, verifier)

    def _drive(self, spec, record, adapter, verifier) -> EffectOutcome:
        status = record.status
        if status in ("applied", "verified_applied"):
            return EffectOutcome("applied", record, should_retry=False)
        if status == "blocked_manual_review":
            return EffectOutcome("blocked_manual_review", record, should_retry=False)
        if status == "verified_absent" or (status == "intent_recorded" and record.attempt_count == 0):
            return self._apply(spec, record, adapter, verifier)
        # interrupted attempt (intent_recorded/apply_in_progress with attempt>0, no result) -> recover
        return self._recover(spec, record, adapter, verifier)

    def _apply(self, spec, record, adapter, verifier) -> EffectOutcome:
        if record.attempt_count >= spec.max_attempts:
            blocked = self._store.update_external_effect_status(
                record.external_effect_id, status="blocked_manual_review",
                error="max_attempts_exhausted")
            return EffectOutcome("blocked_manual_review", blocked, should_retry=False)
        record = self._store.update_external_effect_status(
            record.external_effect_id, status="apply_in_progress",
            increment_attempt_count=True)
        try:
            ar = adapter(spec)
        except Exception as exc:  # ambiguous; leave apply_in_progress + error, let next attempt recover
            self._store.update_external_effect_status(
                record.external_effect_id, status="apply_in_progress", error=str(exc))
            raise
        if ar.applied:
            applied = self._store.update_external_effect_status(
                record.external_effect_id, status="applied", result=ar.result)
            return EffectOutcome("applied", applied, should_retry=False)
        blocked = self._store.update_external_effect_status(
            record.external_effect_id, status="blocked_manual_review",
            result=ar.result, error=ar.reason or "adapter_not_applied")
        return EffectOutcome("blocked_manual_review", blocked, should_retry=False)

    def _recover(self, spec, record, adapter, verifier) -> EffectOutcome:
        verdict = verifier(spec, record)
        updated = self._store.update_external_effect_status(
            record.external_effect_id, status=verdict.classification,
            verification=verdict.verification, verifier_kind=spec.verifier_kind)
        if verdict.classification == "verified_applied":
            return EffectOutcome("applied", updated, should_retry=False)
        if verdict.classification == "verified_absent":
            return self._apply(spec, updated, adapter, verifier)
        return EffectOutcome("blocked_manual_review", updated, should_retry=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/hector/Projects/Dr.-strange/.venv/bin/python -m pytest tests/test_external_effect_executor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claw_v2/external_effect_executor.py tests/test_external_effect_executor.py
git commit -m "feat(f2): F2ExternalEffectExecutor happy path"
```

### Task 1.2: Dedup — re-entry on a terminal record never re-runs the adapter

- [ ] **Step 1: Failing test**

```python
class ExecutorDedupTests(unittest.TestCase):
    def test_reentry_on_applied_does_not_recall_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            n = {"calls": 0}
            def adapter(spec):
                n["calls"] += 1
                return AdapterResult(applied=True, result={"imported_count": 2})
            def verifier(spec, record):
                raise AssertionError("no verifier")
            spec = _spec()
            ex.execute(spec, adapter, verifier)          # first run -> applied
            outcome = ex.execute(spec, adapter, verifier) # same key -> dedup
            self.assertEqual(n["calls"], 1)
            self.assertEqual(outcome.status, "applied")
```

- [ ] **Step 2: Run — Expected: PASS already** (the ON CONFLICT dedup + the
  `applied`/`verified_applied` branch in `_drive` cover this). If it fails,
  inspect `_drive`'s terminal-status short-circuit.

- [ ] **Step 3:** No new code if green. If red, fix `_drive` per the decision table.

- [ ] **Step 4: Commit**

```bash
git add tests/test_external_effect_executor.py
git commit -m "test(f2): executor dedup re-entry never re-runs adapter"
```

### Task 1.3: Recovery — interrupted intent with no result → verifier

- [ ] **Step 1: Failing test** (simulate a crash after intent by manually
  recording an interrupted record, then execute)

```python
class ExecutorRecoveryTests(unittest.TestCase):
    def test_interrupted_intent_runs_verifier_verified_absent_then_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            # simulate prior interrupted attempt: intent recorded, apply_in_progress, attempt=1, no result
            rec = store.record_external_effect(
                task_id=spec.task_id, run_id=spec.run_id, phase=spec.phase,
                effect_kind=spec.effect_kind, target=spec.target, request=spec.request,
                content_hash=spec.content_hash, job_id=spec.job_id, status="apply_in_progress",
                attempt_count=1)
            n = {"calls": 0}
            def adapter(spec):
                n["calls"] += 1
                return AdapterResult(applied=True, result={"imported_count": 4})
            def verifier(spec, record):
                return VerifierVerdict("verified_absent", {"reason": "no-op"}, "count_unchanged")
            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "applied")   # verified_absent -> retry -> applied
            self.assertEqual(n["calls"], 1)
```

- [ ] **Step 2: Run — Expected: PASS** (covered by `_recover` → `_apply`). If red,
  check the interrupted-attempt branch of `_drive`.
- [ ] **Step 3:** Fix only if red.
- [ ] **Step 4: Commit** `test(f2): executor recovery verified_absent retries`

### Task 1.4: verified_applied recovery never re-runs; blocked is terminal

- [ ] **Step 1: Failing tests** (two): an `apply_in_progress` record whose
  verifier returns `verified_applied` → outcome applied, adapter NOT called; a
  verifier returning `blocked_manual_review` → outcome blocked, adapter NOT called.

```python
    def test_recovery_verified_applied_completes_without_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            store.record_external_effect(
                task_id=spec.task_id, run_id=spec.run_id, phase=spec.phase,
                effect_kind=spec.effect_kind, target=spec.target, request=spec.request,
                content_hash=spec.content_hash, status="apply_in_progress", attempt_count=1)
            def adapter(spec):
                raise AssertionError("adapter must not run")
            def verifier(spec, record):
                return VerifierVerdict("verified_applied", {"imported_count": 5}, "result_present")
            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "applied")

    def test_recovery_blocked_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec()
            store.record_external_effect(
                task_id=spec.task_id, run_id=spec.run_id, phase=spec.phase,
                effect_kind=spec.effect_kind, target=spec.target, request=spec.request,
                content_hash=spec.content_hash, status="apply_in_progress", attempt_count=1)
            def adapter(spec):
                raise AssertionError("adapter must not run")
            def verifier(spec, record):
                return VerifierVerdict("blocked_manual_review", {}, "ambiguous")
            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "blocked_manual_review")
```

- [ ] **Step 2: Run — Expected: PASS** (covered by `_recover`). Fix only if red.
- [ ] **Step 3:** —
- [ ] **Step 4: Commit** `test(f2): executor recovery verified_applied/blocked`

### Task 1.5: Attempt budget — APPLY past max → blocked_manual_review

- [ ] **Step 1: Failing test** — a record at `attempt_count == max_attempts`
  entering via `verified_absent` → outcome blocked, adapter NOT called.

```python
    def test_apply_past_attempt_budget_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = _store(tmp)
            ex = F2ExternalEffectExecutor(store)
            spec = _spec(max_attempts=2)
            store.record_external_effect(
                task_id=spec.task_id, run_id=spec.run_id, phase=spec.phase,
                effect_kind=spec.effect_kind, target=spec.target, request=spec.request,
                content_hash=spec.content_hash, status="verified_absent", attempt_count=2)
            def adapter(spec):
                raise AssertionError("must not run past budget")
            def verifier(spec, record):
                raise AssertionError("no verifier")
            outcome = ex.execute(spec, adapter, verifier)
            self.assertEqual(outcome.status, "blocked_manual_review")
```

- [ ] **Step 2: Run — Expected: PASS** (the budget guard in `_apply`). Fix if red.
- [ ] **Step 3:** —
- [ ] **Step 4: Commit** `test(f2): executor attempt-budget fail-closed`

---

## Phase 2 — NotebookLM effect, adapter, verifier

### Task 2.1: `build_research_effect_spec`

**Files:**
- Create: `claw_v2/notebooklm_research_effect.py`
- Test: `tests/test_notebooklm_research_effect.py`

- [ ] **Step 1: Failing test**

```python
import hashlib, unittest
from claw_v2.notebooklm_research_effect import build_research_effect_spec

class BuildSpecTests(unittest.TestCase):
    def test_spec_identity_and_request(self):
        spec = build_research_effect_spec(
            job_id="job:abc", notebook_id="nb-1", query="climate", mode="deep",
            pre_intent_source_count=2)
        self.assertEqual(spec.effect_kind, "notebooklm_research")
        self.assertEqual(spec.target, "nb-1")
        self.assertEqual(spec.run_id, "job:abc")   # same job retry reuses the key
        self.assertEqual(spec.phase, "research")
        self.assertEqual(spec.verifier_kind, "notebooklm_research")
        self.assertEqual(spec.request["pre_intent_source_count"], 2)
        expected = hashlib.sha256("climate|deep".encode()).hexdigest()
        self.assertEqual(spec.content_hash, expected)
```

- [ ] **Step 2: Run — Expected: FAIL** (module missing).
- [ ] **Step 3: Implement**

```python
import hashlib
from claw_v2.external_effect_executor import EffectSpec

def build_research_effect_spec(*, job_id, notebook_id, query, mode,
                               pre_intent_source_count, task_id=None):
    content_hash = hashlib.sha256(f"{query}|{mode}".encode()).hexdigest()
    return EffectSpec(
        task_id=task_id or job_id, run_id=job_id, phase="research",
        effect_kind="notebooklm_research", target=notebook_id,
        request={"notebook_id": notebook_id, "query": query, "mode": mode,
                 "pre_intent_source_count": pre_intent_source_count},
        content_hash=content_hash, job_id=job_id, verifier_kind="notebooklm_research")
```

- [ ] **Step 4: Run — Expected: PASS**
- [ ] **Step 5: Commit** `feat(f2): notebooklm research effect spec`

### Task 2.2: `notebooklm_research_adapter` (classifies imported_count — spec patch 2)

- [ ] **Step 1: Failing tests** — `imported_count > 0` → `AdapterResult.applied`;
  `imported_count == 0` → `applied is False` (blocked).

```python
from claw_v2.notebooklm_research_effect import notebooklm_research_adapter

class AdapterTests(unittest.TestCase):
    def test_positive_import_is_applied(self):
        def deep_research(nb, q): return 3
        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertTrue(ar.applied)
        self.assertEqual(ar.result["imported_count"], 3)

    def test_zero_import_is_not_applied(self):
        def deep_research(nb, q): return 0
        ar = notebooklm_research_adapter(deep_research)(_nb_spec())
        self.assertFalse(ar.applied)
        self.assertEqual(ar.result["imported_count"], 0)
```

(`_nb_spec()` = `build_research_effect_spec(job_id="job:1", notebook_id="nb-1",
query="q", mode="deep", pre_intent_source_count=0)`.)

- [ ] **Step 2: Run — Expected: FAIL**
- [ ] **Step 3: Implement**

```python
from claw_v2.external_effect_executor import AdapterResult

def notebooklm_research_adapter(deep_research_fn):
    def adapter(spec):
        notebook_id = spec.request["notebook_id"]
        query = spec.request["query"]
        imported = int(deep_research_fn(notebook_id, query) or 0)
        return AdapterResult(applied=imported > 0, result={"imported_count": imported},
                             reason=None if imported > 0 else "zero_imports")
    return adapter
```

- [ ] **Step 4: Run — Expected: PASS**
- [ ] **Step 5: Commit** `feat(f2): notebooklm research adapter classifies imports`

### Task 2.3: `notebooklm_research_verifier` (recovery policy — spec §5)

- [ ] **Step 1: Failing tests** — three branches:
  result present → `verified_applied`; count unchanged + no result →
  `verified_absent`; count moved + no result → `blocked_manual_review`.

```python
from claw_v2.notebooklm_research_effect import notebooklm_research_verifier

def _rec(status="apply_in_progress", attempt_count=1, result=None):
    # build a minimal ExternalEffectRecord-like object via the store in a tmp db
    ...

class VerifierTests(unittest.TestCase):
    def test_result_present_is_verified_applied(self):
        v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 5})
        verdict = v(_nb_spec(), _record_with_result(imported_count=2))
        self.assertEqual(verdict.classification, "verified_applied")

    def test_count_unchanged_no_result_is_verified_absent(self):
        v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 0})
        verdict = v(_nb_spec_pre(0), _record_no_result())
        self.assertEqual(verdict.classification, "verified_absent")

    def test_count_moved_no_result_is_blocked(self):
        v = notebooklm_research_verifier(status_fn=lambda nb: {"source_count": 3})
        verdict = v(_nb_spec_pre(0), _record_no_result())
        self.assertEqual(verdict.classification, "blocked_manual_review")
```

Build the record fixtures from a tmp `F2DurabilityStore` (record with/without
result). `status_fn(notebook_id) -> {"source_count": int}` is injected (the real
caller passes a closure over `notebooklm.status`). On `status_fn` raising or
returning no count → `blocked_manual_review`.

- [ ] **Step 2: Run — Expected: FAIL**
- [ ] **Step 3: Implement**

```python
from claw_v2.external_effect_executor import VerifierVerdict

def notebooklm_research_verifier(status_fn):
    def verify(spec, record):
        if record.result_json:  # adapter returned before crash
            return VerifierVerdict("verified_applied", {"source": "result_json"}, "result_present")
        pre = int(spec.request.get("pre_intent_source_count", -1))
        try:
            current = int((status_fn(spec.target) or {}).get("source_count", -1))
        except Exception as exc:
            return VerifierVerdict("blocked_manual_review", {"error": str(exc)}, "status_unavailable")
        if pre >= 0 and current == pre:
            return VerifierVerdict("verified_absent", {"pre": pre, "current": current}, "count_unchanged")
        return VerifierVerdict("blocked_manual_review", {"pre": pre, "current": current}, "count_moved_or_unknown")
    return verify
```

- [ ] **Step 4: Run — Expected: PASS**
- [ ] **Step 5: Commit** `feat(f2): notebooklm research recovery verifier`

---

## Phase 3 — Durable runner, config flag, routing, wiring

### Task 3.1: config flag

**Files:** Modify `claw_v2/config.py`; Test `tests/test_config.py`.

- [ ] **Step 1: Failing test** (mirror an existing AppConfig flag test):
  `CLAW_NOTEBOOKLM_RESEARCH_DURABLE` defaults False, parses truthy.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — add `notebooklm_research_durable: bool` read from
  `CLAW_NOTEBOOKLM_RESEARCH_DURABLE` using the existing truthy-flag helper in
  `config.py` (match the `f2_durability_enabled` pattern).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(config): CLAW_NOTEBOOKLM_RESEARCH_DURABLE flag`

### Task 3.2: `NotebookLMResearchRunner` (claims job → executor → job outcome)

**Files:** Create `claw_v2/notebooklm_research_runner.py`; Test
`tests/test_notebooklm_research_runner.py`.

Runner contract: `run_once()` claims one `notebooklm.research` job via
`JobService.claim_next(worker_id="notebooklm-research", kinds=("notebooklm.research",))`
(so A2 gates apply), reads `pre_intent_source_count` via the injected status_fn,
builds the spec, runs the executor, then maps `EffectOutcome` → job state:
- `applied` → `job_service.complete(job_id, result=...)`.
- `verified_absent` (should_retry) → `job_service.fail(job_id, error="effect_verified_absent_retry", retry=True)`.
- `blocked_manual_review` → `job_service.fail(job_id, error="effect_blocked_manual_review", retry=False)`
  with metadata (`external_effect_id`, `idempotency_key`, `notebook_id`,
  `effect_kind`, `verifier_reason`) + `observe.emit(
  "notebooklm_research_effect_blocked_manual_review", payload=...)` + Telegram
  notify (per spec §7, patch 3).
- adapter raise propagates → `job_service.fail(job_id, error=str(exc), retry=True)`.

- [ ] **Step 1: Failing test** — happy path: enqueue a `notebooklm.research` job;
  `run_once()` with a fake `deep_research` returning 3 → job `completed`, one
  `external_effect_records` row `applied`.

```python
class RunnerHappyTests(unittest.TestCase):
    def test_run_once_completes_job_and_records_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RuntimeDb(Path(tmp) / "claw.db")
            jobs = JobService(Path(tmp) / "claw.db")  # see note: share the RuntimeDb path
            store = F2DurabilityStore(db)
            job = jobs.enqueue(kind="notebooklm.research",
                               payload={"notebook_id": "nb-1", "query": "q", "mode": "deep"})
            runner = NotebookLMResearchRunner(
                job_service=jobs, store=store, observe=None, notifier=None,
                deep_research_fn=lambda nb, q: 3,
                status_fn=lambda nb: {"source_count": 0})
            ran = runner.run_once()
            self.assertTrue(ran)
            self.assertEqual(jobs.get(job.job_id).status, "completed")
            effects = store.list_external_effects()
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].status, "applied")
```

(Implementation note: `JobService` and `F2DurabilityStore` must point at the same
DB. Follow how `build_runtime` wires them — `JobService` over the same path /
RuntimeDb. Confirm the wiring in `main.py` `_setup_*` and mirror it in the test.)

- [ ] **Step 2: Run — FAIL** (module missing).
- [ ] **Step 3: Implement** `NotebookLMResearchRunner` per the contract above,
  using `build_research_effect_spec`, `notebooklm_research_adapter`,
  `notebooklm_research_verifier`, and `F2ExternalEffectExecutor`.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(f2): NotebookLMResearchRunner happy path`

### Task 3.3: Runner — blocked_manual_review path

- [ ] **Step 1: Failing test** — `deep_research` returns 0 → job `failed` with
  `error == "effect_blocked_manual_review"`, retry NOT scheduled, observe event
  emitted (assert via a fake observe), effect row `blocked_manual_review`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3:** Implement the blocked branch (fail retry=False + observe +
  notify) if not already covered.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(f2): runner blocked_manual_review fail-closed`

### Task 3.4: `start_research` routing (two flags)

**Files:** Modify `claw_v2/notebooklm.py` `start_research` (~line 395); Test
`tests/test_notebooklm_research_runner.py` (routing section).

- [ ] **Step 1: Failing tests** — (a) both flags ON → `start_research` enqueues a
  `notebooklm.research` job and does **not** start a thread (assert
  `self._running` stays empty / no thread); (b) either flag OFF → current thread
  path (existing behavior unchanged; assert a job enqueued + thread path as today).

  Inject the flags into the NotebookLM service (constructor param
  `research_durable: bool` resolved from `config.f2_durability_enabled and
  config.notebooklm_research_durable`, passed at construction in `main.py`), so
  the test sets it directly without env.

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the branch at the top of `start_research`:

```python
if self._research_durable:
    job = self._job_service.enqueue(kind="notebooklm.research", payload={...}, metadata={...})
    self._emit("nlm_research_started", notebook_id=full_id, query=query, mode=mode, job_id=job.job_id)
    return f"Deep Research encolado para '{query}' en notebook {title}..."
# else: existing thread path (unchanged)
```

- [ ] **Step 4: Run — PASS.** Also run `tests/test_notebooklm*.py` to confirm the
  thread path is unchanged when the flag is OFF.
- [ ] **Step 5: Commit** `feat(f2): start_research durable routing behind flags`

### Task 3.5: Wire runner in `main.py` (gated)

**Files:** Modify `claw_v2/main.py` (near the stale-recovery runner registration);
Test `tests/test_scheduled_background_jobs.py`.

- [ ] **Step 1: Failing test** — with both flags ON in the env, `build_runtime`
  registers a `notebooklm_research` background runner (assert by name in
  `runtime.daemon._background_job_runners`); with the dedicated flag OFF, it is
  NOT registered. (Mirror `test_autonomy_stale_recovery_runner_*`.)
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — construct `NotebookLMResearchRunner` (wiring
  `job_service`, `store=f2_durability_store`, `observe`, the Telegram notifier,
  the real `deep_research`/`status` callables) and
  `daemon.register_background_job_runner(name="notebooklm_research", handler=runner.run_once, interval=...)`,
  guarded by `if config.f2_durability_enabled and config.notebooklm_research_durable`.
  Note: `f2_durability_store` is only constructed when `config.f2_durability_enabled`
  (`main.py:2184`), so guard accordingly.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(f2): register notebooklm_research durable runner (gated)`

---

## Phase 4 — Crash-resume synthetics

**Files:** Test `tests/test_notebooklm_research_durable_synthetics.py` (mirror
`tests/test_f2_external_effect_synthetics.py`).

One test per spec §8 window, each asserting resumed behavior **and** exactly one
external effect (no duplicate adapter apply). Use a shared store + an adapter
whose call count is asserted.

- [ ] **Task 4.1 — crash before intent commit:** no effect row exists; a fresh
  `execute` records intent and applies once. Assert one `applied` row, one adapter
  call. Commit `test(f2): synthetic crash-before-intent`.
- [ ] **Task 4.2 — crash after intent, before adapter:** pre-seed
  `apply_in_progress` attempt=1 no result; verifier `verified_absent`; `execute`
  retries and applies once. Assert one adapter call total. Commit.
- [ ] **Task 4.3 — crash after adapter, before result commit:** pre-seed
  `apply_in_progress` attempt=1 no result; `status_fn` shows a moved count;
  verifier `blocked_manual_review`; `execute` → blocked, adapter NOT called.
  Assert zero new adapter calls + blocked. Commit.
- [ ] **Task 4.4 — crash after result, before job complete:** pre-seed `applied`
  with result; `execute` → outcome applied, adapter NOT called. Assert no
  duplicate. Commit.

---

## Verification (end of implementation, in the worktree)

- [ ] `uvx ruff check claw_v2 tests` — clean.
- [ ] `uvx ruff format --check` on changed files (format if needed).
- [ ] Targeted suites green: `test_external_effect_executor.py`,
  `test_notebooklm_research_effect.py`, `test_notebooklm_research_runner.py`,
  `test_notebooklm_research_durable_synthetics.py`, `test_scheduled_background_jobs.py`,
  `test_architecture_invariants.py`, `test_config.py`, `tests/test_notebooklm*.py`.
- [ ] Confirm flags-OFF parity: `start_research` thread path + stopgap unchanged.
- [ ] Do **not** run the full suite from the live checkout (daemon-restart hazard);
  the worktree is isolated but some tests touch global resources — run targeted.

## Deploy (separately gated)

Ship with both flags OFF (inert). Merge + `scripts/restart.sh` is a normal,
inert code deploy (no behavior change until Gate B + the dedicated flag). The
deploy itself is a prod restart — confirm before running it. Enabling F2
(Stage 2C2 / Gate B) stays a separate explicit gate.

## Self-review notes

- **Spec coverage:** executor + state machine (spec §4.1, §7) → Phase 1; effect
  spec + verifier policy (§4.2, §4.3, §5, patch 2) → Phase 2; durable runner +
  routing + flags + A2 gate (§4.4, §4.5, §9, §10) → Phase 3; crash windows (§8) →
  Phase 4; blocked_manual_review job handling (§7, patch 3) → Task 3.3; verified_absent
  transition (patch 1) → executor `_recover`→`_apply` + Task 1.3.
- **Open items deferred to execution:** exact `JobService`↔`F2DurabilityStore`
  shared-DB wiring (mirror `build_runtime`); the runner interval (default 300s,
  align with the other runners); whether `claim_next` vs draining a batch per tick.
