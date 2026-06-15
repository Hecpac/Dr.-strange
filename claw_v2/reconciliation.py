"""P0-C: dry-run reconciliation queue for completed_unverified ledger rows.

Behavioral audit (2026-05-23) showed 91 ``agent_tasks`` rows stuck in
``status='completed_unverified'`` / ``verification_status='needs_verification'``
with no SLA, no human signal, no automated closure. This module produces
a sanitized, dry-run JSON report that lists each pending row with the
metadata reviewers need (channel, tools, summary, recommended action,
deadline) and emits a ``pending_verification_reconciliation`` event so
the agent has a durable trace of when the queue was last reviewed.

The module never writes to ``agent_tasks``. Any actual reconciliation
action — auto-closing, re-verifying, escalating — is taken by a separate
human-in-the-loop step.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_RECONCILIATION_DEADLINE_SECONDS = 24 * 60 * 60  # 24 hours
"""How long after ``mark_terminal`` we expect a verifier-or-human signal."""

AUTO_CLOSED_UNVERIFIED_LOOKUP = "auto_closed_unverified_lookup"
"""Terminal ``verification_status`` stamped on read-only, no-error rows drained
past the reconciliation deadline (PR2 Checkpoint C). The row transitions to the
existing terminal ``status='cancelled'`` (reuse-states; no schema migration),
matching the established prod convention, so it leaves the active queue (which
lists only ``completed_unverified`` rows)."""

RECONCILIATION_SCAN_LIMIT = 100
"""Max ``completed_unverified`` rows examined per reconciliation/drain call.
This is the real cap (``TaskLedger.list`` clamps to 100), so drain telemetry
reports it honestly. Checkpoint D must page/lift this before a daemon consumes
the backlog, so old rows are not hidden behind the first page."""

_MUTATING_TOOLS = frozenset(
    {
        "Bash",
        "Write",
        "Edit",
        "Task",
        "social.publish",
        "deploy.production",
        "pipeline.merge",
        "git.force_push",
    }
)
_READONLY_TOOLS = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
    }
)


def recommend_reconciliation_action(*, tools: Iterable[str], error: str | None) -> str:
    """Suggest what to do with an unverified row.

    The recommendations are conservative: any mutating tool keeps the
    row in the human-review lane; presence of an error escalates to
    ``investigate_failure``; rows with read-only tools only are
    candidates for auto-close (still a human decision in practice).
    """
    tools_set = {str(tool) for tool in tools or ()}
    if (error or "").strip():
        return "investigate_failure"
    if tools_set & _MUTATING_TOOLS:
        return "require_human_verification"
    if tools_set and tools_set <= _READONLY_TOOLS:
        return "auto_close_as_unverified_lookup"
    return "needs_evidence_review"


def _tools_from_record(record: Any) -> list[str]:
    artifacts = getattr(record, "artifacts", {}) or {}
    if not isinstance(artifacts, dict):
        return []
    manifest = artifacts.get("evidence_manifest")
    if isinstance(manifest, dict):
        tools = manifest.get("tools_run") or manifest.get("tools_used") or []
    else:
        tools = artifacts.get("tools_run") or []
    return [str(tool) for tool in tools if tool]


def build_reconciliation_report(
    task_ledger: Any,
    *,
    deadline_seconds: int = DEFAULT_RECONCILIATION_DEADLINE_SECONDS,
    observe: Any | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable reconciliation report for unverified rows.

    Pure read; the underlying ``TaskLedger`` is not mutated. When
    ``observe`` is provided, emits ``pending_verification_reconciliation``
    with summary counts so the daemon trace records every review.
    """
    rows = task_ledger.list(statuses=("completed_unverified",), limit=RECONCILIATION_SCAN_LIMIT)
    now = time.time()
    cases: list[dict[str, Any]] = []
    for record in rows:
        verification = str(getattr(record, "verification_status", "") or "unknown")
        completed_at = float(getattr(record, "completed_at", 0.0) or 0.0)
        deadline_epoch = (completed_at or now) + float(deadline_seconds)
        tools = _tools_from_record(record)
        cases.append(
            {
                "task_id": getattr(record, "task_id", ""),
                "channel": getattr(record, "channel", None),
                "external_session_id": getattr(record, "external_session_id", None),
                "session_id": getattr(record, "session_id", ""),
                "tools": tools,
                "verification_status": verification,
                "summary": getattr(record, "summary", ""),
                "error": getattr(record, "error", ""),
                "completed_at_epoch": completed_at,
                "deadline_at_epoch": deadline_epoch,
                "deadline_at": datetime.fromtimestamp(deadline_epoch, tz=timezone.utc).isoformat(),
                "recommended_action": recommend_reconciliation_action(
                    tools=tools, error=str(getattr(record, "error", "") or "")
                ),
            }
        )
    by_action: dict[str, int] = {}
    for case in cases:
        action = case["recommended_action"]
        by_action[action] = by_action.get(action, 0) + 1
    overdue = sum(1 for case in cases if case["deadline_at_epoch"] < now)
    report = {
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "deadline_seconds": int(deadline_seconds),
        "unverified_count": len(cases),
        "overdue_count": overdue,
        "by_recommended_action": by_action,
        "cases": cases,
    }
    if observe is not None:
        try:
            observe.emit(
                "pending_verification_reconciliation",
                payload={
                    "unverified_count": len(cases),
                    "overdue_count": overdue,
                    "by_recommended_action": by_action,
                },
            )
        except Exception:
            # Observability failure must never break the reconciliation
            # read. Surface as debug so silent degrades are still
            # diagnosable (INTERNAL_WIRING §1 no_silent_degrade).
            logger.debug(
                "pending_verification_reconciliation emit failed",
                exc_info=True,
            )
    return report


def write_reconciliation_report(
    task_ledger: Any,
    out_path: Path | str,
    *,
    deadline_seconds: int = DEFAULT_RECONCILIATION_DEADLINE_SECONDS,
    observe: Any | None = None,
) -> Path:
    """Persist the report as pretty JSON. Returns the written path."""
    report = build_reconciliation_report(
        task_ledger, deadline_seconds=deadline_seconds, observe=observe
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
