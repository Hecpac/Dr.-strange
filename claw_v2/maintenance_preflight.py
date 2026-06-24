from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from claw_v2.daemon import PendingVerificationReconciliationJobRunner
from claw_v2.jobs import JobService
from claw_v2.maintenance import (
    CLAW_MAINTENANCE_MODE,
    CLAW_NO_JOB_CLAIM,
    MAINTENANCE_MODE_REASON,
    drain_apply_block_reason,
    flag_enabled,
    job_claim_block_reason,
    scheduler_work_block_reason,
)
from claw_v2.scheduled_background_jobs import (
    APPROVAL_SWEEP_JOB_KIND,
    APPROVAL_SWEEP_RESUME_KEY,
    PIPELINE_POLL_MERGES_JOB_KIND,
    PIPELINE_POLL_MERGES_RESUME_KEY,
    enqueue_scheduled_background_job,
)

PASS = "PASS"
FAIL = "FAIL"

CLAW_F2_DURABILITY_ENABLED = "CLAW_F2_DURABILITY_ENABLED"
F2_DURABILITY_ENABLED = "F2_DURABILITY_ENABLED"


@dataclass(frozen=True, slots=True)
class PathCheck:
    status: str
    reasons: tuple[str, ...]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


@dataclass(slots=True)
class _PreflightJob:
    job_id: str
    payload: dict[str, Any]


class _PreflightLedger:
    def __init__(self) -> None:
        self.list_calls = 0
        self.drain_apply_calls = 0
        self.failure_review_apply_calls = 0

    def list(self, *, statuses: Iterable[str] | None = None, limit: int = 20) -> list[Any]:
        self.list_calls += 1
        return []

    def drain_reconcilable_unverified(self, *, apply: bool, **kwargs: Any) -> dict[str, Any]:
        if apply:
            self.drain_apply_calls += 1
            raise AssertionError("maintenance preflight detected reconcilable drain apply")
        return {"applied": 0}

    def reconcile_failed_unverified(self, *, apply: bool, **kwargs: Any) -> dict[str, Any]:
        if apply:
            self.failure_review_apply_calls += 1
            raise AssertionError("maintenance preflight detected failed-unverified apply")
        return {"reconciled_count": 0}


def collect_maintenance_preflight(
    *,
    db_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed no-work preflight report.

    The live DB, when supplied, is opened read-only/immutable for a liveness
    check only. Work-path proof uses temp state and fakes so the preflight never
    claims live jobs, runs live scheduler handlers, or applies live drains.
    """
    effective_env = dict(os.environ if env is None else env)
    maintenance_mode_active = flag_enabled(CLAW_MAINTENANCE_MODE, effective_env)
    no_job_claim_active = flag_enabled(CLAW_NO_JOB_CLAIM, effective_env)
    f2_enabled = flag_enabled(
        CLAW_F2_DURABILITY_ENABLED,
        effective_env,
    ) or flag_enabled(F2_DURABILITY_ENABLED, effective_env)

    reasons: list[str] = []
    db_path_checked = "temp"
    db_check = _check_db_read_only(db_path) if db_path is not None else None
    if db_check is not None:
        db_path_checked = str(Path(db_path).expanduser())
        if db_check.status == FAIL:
            reasons.extend(f"db:{reason}" for reason in db_check.reasons)

    if not maintenance_mode_active:
        reasons.append("maintenance_mode_inactive")

    claim = _check_claim_path(effective_env)
    scheduler = _check_scheduler_path(effective_env)
    drain = _check_drain_path(effective_env)

    for name, check in (
        ("claim_path", claim),
        ("scheduler_path", scheduler),
        ("drain_path", drain),
    ):
        if check.status == FAIL:
            reasons.extend(f"{name}:{reason}" for reason in check.reasons)

    overall_status = (
        PASS
        if maintenance_mode_active
        and claim.status == PASS
        and scheduler.status == PASS
        and drain.status == PASS
        and (db_check is None or db_check.status == PASS)
        else FAIL
    )
    if overall_status == PASS:
        reasons.append("maintenance_preflight_passed")

    return {
        "overall_status": overall_status,
        "claim_path": claim.status,
        "scheduler_path": scheduler.status,
        "drain_path": drain.status,
        "maintenance_mode_active": maintenance_mode_active,
        "no_job_claim_active": no_job_claim_active,
        "f2_enabled": f2_enabled,
        "db_path_checked": db_path_checked,
        "reasons": reasons,
        "checks": {
            "claim_path": claim.to_dict(),
            "scheduler_path": scheduler.to_dict(),
            "drain_path": drain.to_dict(),
            **({"db_path": db_check.to_dict()} if db_check is not None else {}),
        },
    }


def _check_claim_path(env: Mapping[str, str]) -> PathCheck:
    expected_reason = job_claim_block_reason(env)
    reasons: list[str] = []
    details: dict[str, Any] = {
        "claim_block_reason": expected_reason,
        "claim_result": None,
        "claim_next_result": None,
        "queued_status_after": None,
        "retrying_status_after": None,
        "running_count_after": None,
    }
    if expected_reason is None:
        reasons.append("claim_gate_inactive")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "preflight-claims.db")
            queued = service.enqueue(kind="preflight.claim")
            retrying = service.enqueue(kind="preflight.claim_next")
            with _patched_env(
                {
                    CLAW_MAINTENANCE_MODE: "0",
                    CLAW_NO_JOB_CLAIM: "0",
                }
            ):
                setup_claim = service.claim(retrying.job_id, worker_id="preflight-setup")
                if setup_claim is not None:
                    service.fail(
                        retrying.job_id,
                        error="preflight_retry_setup",
                        retry=True,
                        retry_delay_seconds=0,
                    )
            with _patched_env(env):
                claim_result = service.claim(queued.job_id, worker_id="preflight-worker")
                claim_next_result = service.claim_next(
                    worker_id="preflight-worker",
                    kinds=("preflight.claim_next",),
                    now=time.time() + 1,
                )
            queued_after = service.get(queued.job_id)
            retrying_after = service.get(retrying.job_id)
            running = service.list(statuses=("running",), limit=10)
            details.update(
                {
                    "claim_result": getattr(claim_result, "job_id", None),
                    "claim_next_result": getattr(claim_next_result, "job_id", None),
                    "queued_status_after": getattr(queued_after, "status", None),
                    "retrying_status_after": getattr(retrying_after, "status", None),
                    "running_count_after": len(running),
                }
            )
            if claim_result is not None:
                reasons.append("claim_transitioned_to_running")
            if claim_next_result is not None:
                reasons.append("claim_next_transitioned_to_running")
            if getattr(queued_after, "status", None) != "queued":
                reasons.append("queued_job_status_changed")
            if getattr(retrying_after, "status", None) != "retrying":
                reasons.append("retrying_job_status_changed")
            if running:
                reasons.append("running_jobs_present_after_claim_check")
    except Exception as exc:
        reasons.append(f"claim_check_exception:{exc.__class__.__name__}")
        details["exception"] = str(exc)

    if not reasons:
        reasons.append("claim_and_claim_next_blocked_before_running_transition")
    return PathCheck(
        status=PASS
        if reasons == ["claim_and_claim_next_blocked_before_running_transition"]
        else FAIL,
        reasons=tuple(reasons),
        details=details,
    )


def _check_scheduler_path(env: Mapping[str, str]) -> PathCheck:
    block_reason = scheduler_work_block_reason(env)
    reasons: list[str] = []
    details: dict[str, Any] = {
        "scheduler_block_reason": block_reason,
        "approval_sweep_enqueued": False,
        "pipeline_poll_merges_enqueued": False,
        "queued_count_after": 0,
    }
    if block_reason is None:
        reasons.append("scheduler_gate_inactive")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "preflight-scheduler.db")
            enqueued: list[str | None] = []
            for job_name, job_kind, resume_key in (
                ("approval_sweep", APPROVAL_SWEEP_JOB_KIND, APPROVAL_SWEEP_RESUME_KEY),
                (
                    "pipeline_poll_merges",
                    PIPELINE_POLL_MERGES_JOB_KIND,
                    PIPELINE_POLL_MERGES_RESUME_KEY,
                ),
            ):
                if block_reason:
                    continue
                enqueued.append(
                    enqueue_scheduled_background_job(
                        job_name=job_name,
                        job_kind=job_kind,
                        resume_key=resume_key,
                        job_service=service,
                    )
                )
            queued = service.list(limit=10)
            details.update(
                {
                    "approval_sweep_enqueued": any(
                        job.kind == APPROVAL_SWEEP_JOB_KIND for job in queued
                    ),
                    "pipeline_poll_merges_enqueued": any(
                        job.kind == PIPELINE_POLL_MERGES_JOB_KIND for job in queued
                    ),
                    "queued_count_after": len(queued),
                    "enqueue_results": [job_id for job_id in enqueued if job_id],
                }
            )
            if queued:
                reasons.append("scheduled_work_would_enqueue")
    except Exception as exc:
        reasons.append(f"scheduler_check_exception:{exc.__class__.__name__}")
        details["exception"] = str(exc)

    if not reasons:
        reasons.append("approval_sweep_and_pipeline_poll_merges_blocked_before_enqueue")
    return PathCheck(
        status=PASS
        if reasons == ["approval_sweep_and_pipeline_poll_merges_blocked_before_enqueue"]
        else FAIL,
        reasons=tuple(reasons),
        details=details,
    )


def _check_drain_path(env: Mapping[str, str]) -> PathCheck:
    block_reason = drain_apply_block_reason(env)
    reasons: list[str] = []
    details: dict[str, Any] = {
        "drain_apply_block_reason": block_reason,
        "drain_apply_requested": True,
        "observe_only_reconciliation_ran": False,
        "drain_apply_calls": 0,
        "failure_review_apply_calls": 0,
        "drain_skip_reason": None,
    }
    if block_reason is None:
        reasons.append("drain_apply_gate_inactive")

    ledger = _PreflightLedger()
    runner = PendingVerificationReconciliationJobRunner(
        job_service=None,
        task_ledger=ledger,
        observe=None,
    )
    job = _PreflightJob(
        job_id="job:maintenance-preflight-drain",
        payload={
            "drain_apply": True,
            "drain_max_scan": 500,
            "drain_max_apply": 10,
        },
    )
    try:
        with _patched_env(env):
            result = runner._execute(job)
        details.update(
            {
                "observe_only_reconciliation_ran": ledger.list_calls > 0,
                "drain_apply_calls": ledger.drain_apply_calls,
                "failure_review_apply_calls": ledger.failure_review_apply_calls,
                "drain_apply_result": result.get("drain_apply"),
                "drain_skip_reason": result.get("drain_skip_reason"),
            }
        )
        if ledger.drain_apply_calls:
            reasons.append("drain_reconcilable_unverified_apply_called")
        if ledger.failure_review_apply_calls:
            reasons.append("reconcile_failed_unverified_apply_called")
        if result.get("drain_apply") is not False:
            reasons.append("drain_apply_not_disabled")
        if result.get("drain_skip_reason") != MAINTENANCE_MODE_REASON:
            reasons.append("drain_skip_reason_missing_or_unexpected")
        if ledger.list_calls < 1:
            reasons.append("observe_only_reconciliation_not_run")
    except Exception as exc:
        reasons.append(f"drain_check_exception:{exc.__class__.__name__}")
        details.update(
            {
                "exception": str(exc),
                "observe_only_reconciliation_ran": ledger.list_calls > 0,
                "drain_apply_calls": ledger.drain_apply_calls,
                "failure_review_apply_calls": ledger.failure_review_apply_calls,
            }
        )

    if not reasons:
        reasons.append("drain_apply_blocked_after_observe_only_reconciliation")
    return PathCheck(
        status=PASS
        if reasons == ["drain_apply_blocked_after_observe_only_reconciliation"]
        else FAIL,
        reasons=tuple(reasons),
        details=details,
    )


def _check_db_read_only(db_path: Path | str) -> PathCheck:
    path = Path(db_path).expanduser().resolve()
    details: dict[str, Any] = {"path": str(path), "opened_read_only_immutable": False}
    reasons: list[str] = []
    if not path.exists():
        reasons.append("db_path_missing")
        return PathCheck(status=FAIL, reasons=tuple(reasons), details=details)
    conn: sqlite3.Connection | None = None
    try:
        uri = f"{path.as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        details["opened_read_only_immutable"] = True
    except Exception as exc:
        reasons.append(f"db_read_only_open_failed:{exc.__class__.__name__}")
        details["exception"] = str(exc)
    finally:
        if conn is not None:
            conn.close()

    if not reasons:
        reasons.append("db_opened_read_only_immutable")
    return PathCheck(
        status=PASS if reasons == ["db_opened_read_only_immutable"] else FAIL,
        reasons=tuple(reasons),
        details=details,
    )


@contextmanager
def _patched_env(env: Mapping[str, str]):
    keys = {
        CLAW_MAINTENANCE_MODE,
        CLAW_NO_JOB_CLAIM,
        CLAW_F2_DURABILITY_ENABLED,
        F2_DURABILITY_ENABLED,
        "CLAW_PENDING_VERIFICATION_DRAIN_APPLY",
    }
    keys.update(env.keys())
    old_values = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            if key in env:
                os.environ[key] = str(env[key])
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _env_with_cli_overrides(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    _apply_bool_option(env, CLAW_MAINTENANCE_MODE, args.maintenance_mode)
    _apply_bool_option(env, CLAW_NO_JOB_CLAIM, args.no_job_claim)
    _apply_bool_option(env, CLAW_F2_DURABILITY_ENABLED, args.f2)
    if args.f2 != "env":
        env[F2_DURABILITY_ENABLED] = env[CLAW_F2_DURABILITY_ENABLED]
    return env


def _apply_bool_option(env: dict[str, str], name: str, option: str) -> None:
    if option == "env":
        return
    env[name] = "1" if option == "on" else "0"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only maintenance no-work preflight for F2 canaries.",
    )
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--maintenance-mode",
        choices=("on", "off", "env"),
        default="env",
        help="Maintenance posture to prove. Use 'on' for the Stage 2C2 canary gate.",
    )
    parser.add_argument(
        "--no-job-claim",
        choices=("on", "off", "env"),
        default="env",
        help="Optional claim-only gate override.",
    )
    parser.add_argument(
        "--f2",
        choices=("on", "off", "env"),
        default="env",
        help="F2 durability posture reported by the preflight.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON only.")
    return parser


def _format_human(report: Mapping[str, Any]) -> str:
    lines = [
        f"overall_status: {report['overall_status']}",
        f"claim_path: {report['claim_path']}",
        f"scheduler_path: {report['scheduler_path']}",
        f"drain_path: {report['drain_path']}",
        f"maintenance_mode_active: {str(report['maintenance_mode_active']).lower()}",
        f"no_job_claim_active: {str(report['no_job_claim_active']).lower()}",
        f"f2_enabled: {str(report['f2_enabled']).lower()}",
        f"db_path_checked: {report['db_path_checked']}",
        "reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in report["reasons"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        report = collect_maintenance_preflight(
            db_path=args.db_path,
            env=_env_with_cli_overrides(args),
        )
    except Exception as exc:
        report = {
            "overall_status": FAIL,
            "claim_path": FAIL,
            "scheduler_path": FAIL,
            "drain_path": FAIL,
            "maintenance_mode_active": False,
            "no_job_claim_active": False,
            "f2_enabled": False,
            "db_path_checked": str(args.db_path) if args.db_path is not None else "temp",
            "reasons": [f"unexpected_exception:{exc.__class__.__name__}"],
            "checks": {},
        }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    return 0 if report["overall_status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
