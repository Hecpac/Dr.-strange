---
name: claw-lease-safety-review
description: Use when reviewing or changing Dr. Strange formal JobService leases, job lifecycle mutations, runner lease token propagation, heartbeat, or lease-related RuntimeDb safety.
---

# Claw Lease Safety Review

Use this before touching `JobService`, job lifecycle mutations, runners that
claim jobs, heartbeat, or formal lease runtime activation.

## Invariants

- `formal_job_leases_enabled` and `CLAW_FORMAL_JOB_LEASES_ENABLED` stay
  default-off.
- `lease_generation` is the CAS token.
- `worker_id` alone is not sufficient ownership proof.
- Formal `heartbeat_lease`, `release_lease`, `checkpoint`,
  `wait_for_approval`, `complete`, `fail`, and `reschedule` require owner plus
  generation.
- Do not add new RuntimeDb writers or new RuntimeDb connections.
- Do not activate formal leases in real runtime until runners propagate
  `lease_owner` + `lease_generation` and heartbeat works end-to-end.

## Required Review Scenarios

Simulate and map tests for:

- worker crash after claim;
- worker crash during heartbeat;
- double release;
- heartbeat after release;
- expired lease reclaim;
- concurrent reclaim;
- same `worker_id` with stale generation;
- stale heartbeat, release, complete, fail, reschedule, approval, and checkpoint
  after another worker claimed a newer generation;
- flag off after passive migration.

## Commands

```bash
.venv/bin/python -m py_compile claw_v2/jobs.py claw_v2/config.py claw_v2/main.py tests/test_jobs.py tests/test_config.py tests/test_architecture_invariants.py
uvx ruff check claw_v2/config.py claw_v2/jobs.py claw_v2/main.py tests/test_architecture_invariants.py tests/test_config.py tests/test_jobs.py
uvx ruff format --check claw_v2/config.py claw_v2/jobs.py claw_v2/main.py tests/test_architecture_invariants.py tests/test_config.py tests/test_jobs.py
.venv/bin/python -m pytest tests/test_jobs.py tests/test_config.py tests/test_architecture_invariants.py tests/test_task_handler.py tests/test_sqlite_runtime.py tests/test_observe_subscribe.py tests/test_runtimedb_wiring.py -q
```

## Output

Report:

- GO / GO WITH RISKS / NO-GO;
- P0/P1/P2 findings;
- invariant-to-test mapping;
- whether formal leases remain default-off;
- whether any runner still lacks token propagation or heartbeat.
