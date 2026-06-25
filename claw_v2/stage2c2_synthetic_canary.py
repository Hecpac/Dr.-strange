"""Isolated Stage 2C2 synthetic F2 canary.

Exercises the F2 durability store and the recovery planner against *synthetic*
state on an isolated temp DB, so Stage 2C2 prep can prove the F2 logic without
ever touching the primary live DB.

What it proves: the F2 store/planner *logic* — phase checkpoints, ordered
checkpoint writes, external-effect records (incl. idempotency), and the
recovery-planner classifications (COMPLETE / RETRYABLE / BLOCKED /
MANUAL_REVIEW_REQUIRED, verified_applied/absent handling, no auto-replay).

What it does NOT prove: the live daemon's F2 path against the primary DB. That
exercise remains unbuilt and still requires injecting synthetic state through
the daemon single-writer path (or a quiesced daemon). A PASS here must never be
read as "Stage 2C2 is safe to enable live"; the live gap that BLOCKED the live
canary persists.

Usage:

    python -m claw_v2.stage2c2_synthetic_canary --temp-db --json

The harness only ever writes to a temp DB it creates. A supplied ``--db-path``
is refused before anything is opened.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.f2_durability_schema import F2_DURABILITY_SCHEMA_VERSION
from claw_v2.f2_durability_store import (
    F2DurabilityStore,
    compute_external_effect_idempotency_key,
)
from claw_v2.f2_recovery import F2RecoveryStatus, plan_f2_recovery
from claw_v2.sqlite_runtime import RuntimeDb

PASS = "PASS"
FAIL = "FAIL"
SYNTHETIC_PREFIX = "stage2c2-"
_PHASE = "implementation"
_F2_TABLES = (
    "phase_checkpoints",
    "phase_checkpoint_writes",
    "external_effect_records",
    "phase_recovery_cursors",
)
_DOES_NOT_PROVE = (
    "Isolated synthetic validation of F2 store + recovery-planner logic only. "
    "Does NOT prove the live daemon's F2 path against the primary DB: that "
    "exercise remains unbuilt and still requires injecting synthetic state "
    "through the daemon single-writer path or a quiesced daemon. A PASS here is "
    "NOT a signal that enabling F2 live (Gate B / Stage 2C2) is safe."
)


@dataclass(slots=True)
class _PathResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Synthetic seeding helpers (stage2c2-* IDs only)                             #
# --------------------------------------------------------------------------- #
def _seed_phase(
    store: F2DurabilityStore,
    *,
    task_id: str,
    phase: str = _PHASE,
    terminal_status: str | None,
) -> None:
    """Seed a phase: a ``started`` write+checkpoint, then an optional terminal
    write+checkpoint. ``terminal_status=None`` leaves the phase started-only."""
    start_write = store.append_checkpoint_write(
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        write_kind="phase_started",
        payload={"event": "phase_started", "phase": phase},
    )
    store.create_phase_checkpoint(
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        phase_version=1,
        status="started",
        last_write_order=start_write.write_order,
        payload={"event": "phase_started", "phase": phase},
    )
    if terminal_status is None:
        return
    finish_write = store.append_checkpoint_write(
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        write_kind="phase_return" if terminal_status == "succeeded" else "phase_error",
        payload={"event": f"phase_{terminal_status}", "phase": phase},
    )
    store.create_phase_checkpoint(
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        phase_version=2,
        status=terminal_status,
        last_write_order=finish_write.write_order,
        payload={"event": f"phase_{terminal_status}", "phase": phase},
    )


def _seed_write_without_checkpoint(
    store: F2DurabilityStore, *, task_id: str, phase: str = _PHASE
) -> None:
    """A checkpoint write with no phase checkpoint → BLOCKED (writes ahead of a
    durable checkpoint)."""
    store.append_checkpoint_write(
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        write_kind="phase_started",
        payload={"event": "write_without_checkpoint", "phase": phase},
    )


def _seed_effect(
    store: F2DurabilityStore,
    *,
    task_id: str,
    phase: str = _PHASE,
    status: str,
    linked: bool,
):
    """Record a synthetic external effect, optionally linked via a checkpoint
    write. An *unlinked* effect is an orphan; an unsafe linked status is a
    blocker — both drive MANUAL_REVIEW_REQUIRED."""
    effect = store.record_external_effect(
        external_effect_id=f"{task_id}-effect",
        task_id=task_id,
        run_id=task_id,
        phase=phase,
        effect_kind="synthetic_external_effect",
        target=f"synthetic://stage2c2/{task_id}",
        content_hash=f"sha256:{task_id}",
        request={"action": "synthetic_apply"},
    )
    if status != "intent_recorded":
        updated = store.update_external_effect_status(
            effect.external_effect_id,
            status=status,
            result={"provider_effect_id": "synthetic"},
            verification={"status": status, "source": "synthetic"},
            verifier_kind="synthetic",
        )
        if updated is not None:
            effect = updated
    if linked:
        store.append_checkpoint_write(
            task_id=task_id,
            run_id=task_id,
            phase=phase,
            write_kind="external_effect_intent",
            write_key=f"external-effect:{effect.external_effect_id}",
            payload={"external_effect_id": effect.external_effect_id},
            external_effect_id=effect.external_effect_id,
        )
    return effect


def _plan(store: F2DurabilityStore, task_id: str):
    # Single-phase isolation so each synthetic task classifies on its own
    # seeded phase (matching the existing F2 synthetic tests).
    return plan_f2_recovery(store, task_id=task_id, run_id=task_id, phases=(_PHASE,))


# --------------------------------------------------------------------------- #
# Path checks                                                                 #
# --------------------------------------------------------------------------- #
def _check_phase_checkpoint_path(
    store: F2DurabilityStore,
) -> tuple[_PathResult, list[str]]:
    reasons: list[str] = []
    task_id = f"{SYNTHETIC_PREFIX}phase-complete"
    _seed_phase(store, task_id=task_id, terminal_status="succeeded")
    checkpoints = store.list_phase_checkpoints(
        task_id=task_id, run_id=task_id, phase=_PHASE, order="phase_version_asc"
    )
    writes = store.list_checkpoint_writes(
        task_id=task_id, run_id=task_id, phase=_PHASE, order="write_order_asc"
    )
    statuses = [c.status for c in checkpoints]
    orders = [w.write_order for w in writes]

    if statuses != ["started", "succeeded"]:
        reasons.append(f"checkpoint_status_sequence_unexpected:{statuses}")
    if orders != list(range(1, len(orders) + 1)):
        reasons.append(f"checkpoint_writes_not_contiguously_ordered:{orders}")
    if len(writes) != 2:
        reasons.append(f"expected_2_writes_got_{len(writes)}")
    write_orders = set(orders)
    for checkpoint in checkpoints:
        if checkpoint.last_write_order not in write_orders:
            reasons.append(f"checkpoint_last_write_order_unlinked:{checkpoint.last_write_order}")
    if any(not c.payload_sha256 for c in checkpoints) or any(not w.payload_sha256 for w in writes):
        reasons.append("payload_hash_missing")
    if any(c.schema_version != F2_DURABILITY_SCHEMA_VERSION for c in checkpoints):
        reasons.append("checkpoint_schema_version_invalid")
    if any(w.schema_version != F2_DURABILITY_SCHEMA_VERSION for w in writes):
        reasons.append("write_schema_version_invalid")

    details = {
        "checkpoint_statuses": statuses,
        "write_orders": orders,
        "schema_version": F2_DURABILITY_SCHEMA_VERSION,
    }
    status = PASS if not reasons else FAIL
    if status == PASS:
        reasons.append("phase_checkpoint_structure_verified")
    return _PathResult(status=status, reasons=reasons, details=details), [task_id]


def _check_recovery_planner_path(
    store: F2DurabilityStore,
) -> tuple[_PathResult, list[str]]:
    reasons: list[str] = []
    ids: list[str] = []
    classifications: dict[str, str] = {}

    def _expect(label: str, task_id: str, expected: F2RecoveryStatus):
        plan = _plan(store, task_id)
        classifications[label] = plan.status.value
        if plan.status is not expected:
            reasons.append(f"{label}_misclassified:{plan.status.value}")
        # Regression guard: the planner hardcodes will_replay_external_effects
        # to False (f2_recovery.py) — it must never auto-replay external effects.
        if plan.will_replay_external_effects:
            reasons.append(f"{label}_will_replay_true")
        return plan

    # COMPLETE: terminal succeeded checkpoint.
    task_id = f"{SYNTHETIC_PREFIX}rec-complete"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status="succeeded")
    _expect("complete", task_id, F2RecoveryStatus.COMPLETE)

    # RETRYABLE: started-only checkpoint, no terminal.
    task_id = f"{SYNTHETIC_PREFIX}rec-retryable"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    _expect("retryable", task_id, F2RecoveryStatus.RETRYABLE)

    # BLOCKED: writes ahead of any checkpoint.
    task_id = f"{SYNTHETIC_PREFIX}rec-blocked"
    ids.append(task_id)
    _seed_write_without_checkpoint(store, task_id=task_id)
    _expect("blocked", task_id, F2RecoveryStatus.BLOCKED)

    # MANUAL_REVIEW_REQUIRED: orphaned (unlinked) external effect.
    task_id = f"{SYNTHETIC_PREFIX}rec-manual"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    _seed_effect(store, task_id=task_id, status="verified_applied", linked=False)
    manual_plan = _expect("manual_review", task_id, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
    if not manual_plan.external_effect_blockers:
        reasons.append("manual_review_missing_blocker")

    # verified_applied (linked) → recorded, no replay, no future execution.
    task_id = f"{SYNTHETIC_PREFIX}rec-applied"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    applied_effect = _seed_effect(store, task_id=task_id, status="verified_applied", linked=True)
    applied_plan = _plan(store, task_id)
    classifications["verified_applied"] = applied_plan.status.value
    applied_decision = applied_plan.phase_decisions[0]
    if applied_effect.external_effect_id not in applied_decision.verified_applied_effect_ids:
        reasons.append("verified_applied_not_recorded")
    if (
        applied_effect.external_effect_id
        in applied_plan.external_effects_requiring_future_execution
    ):
        reasons.append("verified_applied_should_not_require_future_execution")
    if applied_plan.will_replay_external_effects:
        reasons.append("verified_applied_will_replay_true")
    if applied_plan.external_effect_blockers:
        reasons.append("verified_applied_unexpected_blocker")

    # verified_absent (linked) → future execution required, no replay.
    task_id = f"{SYNTHETIC_PREFIX}rec-absent"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    absent_effect = _seed_effect(store, task_id=task_id, status="verified_absent", linked=True)
    absent_plan = _plan(store, task_id)
    classifications["verified_absent"] = absent_plan.status.value
    if (
        absent_effect.external_effect_id
        not in absent_plan.external_effects_requiring_future_execution
    ):
        reasons.append("verified_absent_not_future_execution")
    if absent_plan.will_replay_external_effects:
        reasons.append("verified_absent_will_replay_true")

    details = {"classifications": classifications}
    status = PASS if not reasons else FAIL
    if status == PASS:
        reasons.append("recovery_classifications_verified")
    return _PathResult(status=status, reasons=reasons, details=details), ids


def _check_external_effect_path(
    store: F2DurabilityStore,
) -> tuple[_PathResult, list[str]]:
    reasons: list[str] = []
    ids: list[str] = []

    # Idempotency: same key + different requested id returns the FIRST row.
    task_id = f"{SYNTHETIC_PREFIX}eff-idem"
    ids.append(task_id)
    key = compute_external_effect_idempotency_key(
        task_id=task_id,
        run_id=task_id,
        phase=_PHASE,
        effect_kind="synthetic_external_effect",
        target=f"synthetic://stage2c2/{task_id}",
        content_hash=f"sha256:{task_id}",
    )
    common = dict(
        idempotency_key=key,
        task_id=task_id,
        run_id=task_id,
        phase=_PHASE,
        effect_kind="synthetic_external_effect",
        target=f"synthetic://stage2c2/{task_id}",
        content_hash=f"sha256:{task_id}",
    )
    first = store.record_external_effect(
        external_effect_id=f"{task_id}-first", request={"v": 1}, **common
    )
    second = store.record_external_effect(
        external_effect_id=f"{task_id}-second", request={"v": 2}, **common
    )
    rows = store.list_external_effects(task_id=task_id)
    if len(rows) != 1:
        reasons.append(f"idempotency_row_count:{len(rows)}")
    if second.external_effect_id != first.external_effect_id:
        reasons.append("idempotency_returned_new_row")
    if second.external_effect_id != f"{task_id}-first":
        reasons.append("idempotency_not_first_row")

    # Orphaned effect → blocker reason orphaned_external_effect → MANUAL_REVIEW.
    task_id = f"{SYNTHETIC_PREFIX}eff-orphan"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    _seed_effect(store, task_id=task_id, status="verified_applied", linked=False)
    orphan_plan = _plan(store, task_id)
    if orphan_plan.status is not F2RecoveryStatus.MANUAL_REVIEW_REQUIRED:
        reasons.append(f"orphan_not_manual_review:{orphan_plan.status.value}")
    orphan_reasons = {b.reason for b in orphan_plan.external_effect_blockers}
    if "orphaned_external_effect" not in orphan_reasons:
        reasons.append(f"orphan_blocker_reason_unexpected:{sorted(orphan_reasons)}")

    # Unsafe linked status → blocker → MANUAL_REVIEW (no replay).
    task_id = f"{SYNTHETIC_PREFIX}eff-unsafe"
    ids.append(task_id)
    _seed_phase(store, task_id=task_id, terminal_status=None)
    _seed_effect(store, task_id=task_id, status="apply_in_progress", linked=True)
    unsafe_plan = _plan(store, task_id)
    if unsafe_plan.status is not F2RecoveryStatus.MANUAL_REVIEW_REQUIRED:
        reasons.append(f"unsafe_not_manual_review:{unsafe_plan.status.value}")
    if unsafe_plan.will_replay_external_effects:
        reasons.append("unsafe_will_replay_true")

    details = {"idempotency_row_count": len(rows)}
    status = PASS if not reasons else FAIL
    if status == PASS:
        reasons.append("external_effect_behaviour_verified")
    return _PathResult(status=status, reasons=reasons, details=details), ids


# --------------------------------------------------------------------------- #
# Isolation + report assembly                                                 #
# --------------------------------------------------------------------------- #
def _f2_counts(db: RuntimeDb) -> dict[str, int]:
    counts: dict[str, int] = {}
    with db.cursor() as cur:
        for table in _F2_TABLES:
            counts[table] = int(cur.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    return counts


def _non_synthetic_row_count(db: RuntimeDb) -> int:
    total = 0
    with db.cursor() as cur:
        for table in _F2_TABLES:
            row = cur.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE task_id NOT LIKE ?",
                (f"{SYNTHETIC_PREFIX}%",),
            ).fetchone()
            total += int(row["c"])
    return total


def _refused_report(db_path: str) -> dict[str, Any]:
    """Pure-path refusal: a supplied non-temp DB path is rejected before any DB
    is opened, so the primary DB is never touched."""
    return {
        "overall_status": FAIL,
        "db_path_checked": str(db_path),
        "temp_db_only": False,
        "primary_db_touched": False,
        "synthetic_prefix": SYNTHETIC_PREFIX,
        "phase_checkpoint_path": FAIL,
        "recovery_planner_path": FAIL,
        "external_effect_path": FAIL,
        "non_synthetic_records_created": False,
        "real_external_effects_executed": False,
        "counts_before": {table: None for table in _F2_TABLES},
        "counts_after": {table: None for table in _F2_TABLES},
        "synthetic_ids": [],
        "reasons": ["non_temp_db_path_refused_requires_future_operator_authorization"],
        "checks": {},
        "does_not_prove": _DOES_NOT_PROVE,
    }


def _exception_report(exc: BaseException) -> dict[str, Any]:
    return {
        "overall_status": FAIL,
        "db_path_checked": "temp",
        "temp_db_only": True,
        "primary_db_touched": False,
        "synthetic_prefix": SYNTHETIC_PREFIX,
        "phase_checkpoint_path": FAIL,
        "recovery_planner_path": FAIL,
        "external_effect_path": FAIL,
        "non_synthetic_records_created": False,
        "real_external_effects_executed": False,
        "counts_before": {table: None for table in _F2_TABLES},
        "counts_after": {table: None for table in _F2_TABLES},
        "synthetic_ids": [],
        "reasons": [f"unexpected_exception:{exc.__class__.__name__}", str(exc)],
        "checks": {},
        "does_not_prove": _DOES_NOT_PROVE,
    }


def run_stage2c2_synthetic_canary(*, db_path: str | None = None) -> dict[str, Any]:
    """Run the isolated Stage 2C2 synthetic F2 canary and return a structured,
    fail-closed report. Writes only to a fresh temp DB; a supplied ``db_path``
    is refused before anything is opened."""
    if db_path is not None:
        return _refused_report(db_path)

    try:
        with tempfile.TemporaryDirectory(prefix="stage2c2-canary-") as tmpdir:
            temp_path = Path(tmpdir) / "claw.db"
            db = RuntimeDb(temp_path)
            try:
                store = F2DurabilityStore(db)
                counts_before = _f2_counts(db)
                phase_result, phase_ids = _check_phase_checkpoint_path(store)
                recovery_result, recovery_ids = _check_recovery_planner_path(store)
                effect_result, effect_ids = _check_external_effect_path(store)
                counts_after = _f2_counts(db)
                non_synthetic = _non_synthetic_row_count(db)
                synthetic_ids = sorted(set(phase_ids + recovery_ids + effect_ids))
            finally:
                # Close before the TemporaryDirectory unlinks (WAL -wal/-shm).
                db.close()
    except Exception as exc:  # fail closed on any harness error
        return _exception_report(exc)

    isolation_clean = non_synthetic == 0 and all(v == 0 for v in counts_before.values())
    paths_pass = (
        phase_result.status == PASS
        and recovery_result.status == PASS
        and effect_result.status == PASS
    )
    overall = PASS if (paths_pass and isolation_clean) else FAIL

    reasons: list[str] = []
    reasons.extend(f"phase:{r}" for r in phase_result.reasons)
    reasons.extend(f"recovery:{r}" for r in recovery_result.reasons)
    reasons.extend(f"effect:{r}" for r in effect_result.reasons)
    if non_synthetic:
        reasons.append(f"non_synthetic_records_present:{non_synthetic}")
    if not all(v == 0 for v in counts_before.values()):
        reasons.append("counts_before_nonzero")
    if overall == PASS:
        reasons.append("stage2c2_synthetic_canary_passed")

    return {
        "overall_status": overall,
        "db_path_checked": str(temp_path),
        "temp_db_only": True,
        "primary_db_touched": False,
        "synthetic_prefix": SYNTHETIC_PREFIX,
        "phase_checkpoint_path": phase_result.status,
        "recovery_planner_path": recovery_result.status,
        "external_effect_path": effect_result.status,
        "non_synthetic_records_created": non_synthetic > 0,
        "real_external_effects_executed": False,
        "counts_before": counts_before,
        "counts_after": counts_after,
        "synthetic_ids": synthetic_ids,
        "reasons": reasons,
        "checks": {
            "phase_checkpoint_path": phase_result.details,
            "recovery_planner_path": recovery_result.details,
            "external_effect_path": effect_result.details,
        },
        "does_not_prove": _DOES_NOT_PROVE,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated Stage 2C2 synthetic F2 canary. Writes only to a temp DB; "
            "proves F2 store/planner logic, not the live primary-DB path."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--temp-db",
        action="store_true",
        help="Run against an isolated temp DB (default behavior).",
    )
    group.add_argument(
        "--db-path",
        default=None,
        help=(
            "Refused. Reserved for a future operator-authorized read-only check; "
            "the harness never writes to a supplied DB path."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON only.")
    return parser


def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"overall_status: {report['overall_status']}",
        f"db_path_checked: {report['db_path_checked']}",
        f"temp_db_only: {str(report['temp_db_only']).lower()}",
        f"primary_db_touched: {str(report['primary_db_touched']).lower()}",
        f"synthetic_prefix: {report['synthetic_prefix']}",
        f"phase_checkpoint_path: {report['phase_checkpoint_path']}",
        f"recovery_planner_path: {report['recovery_planner_path']}",
        f"external_effect_path: {report['external_effect_path']}",
        f"non_synthetic_records_created: {str(report['non_synthetic_records_created']).lower()}",
        f"real_external_effects_executed: {str(report['real_external_effects_executed']).lower()}",
        f"counts_before: {report['counts_before']}",
        f"counts_after: {report['counts_after']}",
        f"synthetic_ids: {report['synthetic_ids']}",
        "reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in report["reasons"])
    lines.append(f"does_not_prove: {report['does_not_prove']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_stage2c2_synthetic_canary(db_path=args.db_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    return 0 if report["overall_status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
