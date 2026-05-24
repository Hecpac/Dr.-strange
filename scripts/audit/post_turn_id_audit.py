#!/usr/bin/env python3
"""Post-merge audit: turn_id coverage and behavioral deltas.

Designed to run 24-48h after the turn_id correlator went live
(2026-05-24 restart). Produces a sanitized JSON report comparing
post-merge metrics to the 2026-05-23 baseline that motivated the work.

Outputs:
  reports/behavior_audit/post_turn_id_audit_<run_id>.json
  reports/behavior_audit/BEHAVIOR_AUDIT_REPORT_<run_id>.md
  reports/behavior_audit/behavior_cases_sample_<run_id>.jsonl

No DB mutation — read-only against ``data/claw.db?mode=ro``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claw_v2.behavior_audit_io import build_frontmatter, generate_run_id

BASELINE = {
    # 2026-05-23 audit (524 cases canonical + 100 complement).
    "channel_null_brain_fallback_pct": 0.99,
    "task_outcomes_success_with_unverified_status_pct": "high",
    "completed_unverified_count": 91,
    "task_outcomes_success_count": 144,
    "turn_id_coverage_pct": 0.0,
    "soul_update_suggestion_duplicate_count": 6,
    "promote_perf_optimizer_pending_count": 17,
}


def _connect_readonly(db: Path) -> sqlite3.Connection:
    uri = f"{db.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _metric(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def compute_post_metrics(db: Path, since: str) -> dict:
    conn = _connect_readonly(db)
    try:
        total_events_post = _metric(
            conn,
            "SELECT COUNT(*) FROM observe_stream WHERE datetime(timestamp) >= datetime(?)",
            since,
        )
        events_with_turn_id_post = _metric(
            conn,
            """
            SELECT COUNT(*) FROM observe_stream
            WHERE datetime(timestamp) >= datetime(?)
              AND json_extract(payload, '$.turn_id') IS NOT NULL
            """,
            since,
        )
        unique_turn_ids_post = _metric(
            conn,
            """
            SELECT COUNT(DISTINCT json_extract(payload, '$.turn_id')) FROM observe_stream
            WHERE datetime(timestamp) >= datetime(?)
              AND json_extract(payload, '$.turn_id') IS NOT NULL
            """,
            since,
        )
        turn_id_missing_post = _metric(
            conn,
            """
            SELECT COUNT(*) FROM observe_stream
            WHERE event_type='turn_id_missing' AND datetime(timestamp) >= datetime(?)
            """,
            since,
        )
        # Channel NULL on brain-fallback tg-* tasks (the D fix target).
        brain_fallback_post = _metric(
            conn,
            """
            SELECT COUNT(*) FROM agent_tasks
            WHERE mode='brain_fallback' AND session_id LIKE 'tg-%'
              AND created_at >= strftime('%s', ?)
            """,
            since,
        )
        brain_fallback_null_channel_post = _metric(
            conn,
            """
            SELECT COUNT(*) FROM agent_tasks
            WHERE mode='brain_fallback' AND session_id LIKE 'tg-%'
              AND created_at >= strftime('%s', ?)
              AND (channel IS NULL OR channel='')
            """,
            since,
        )
        # task_outcomes alignment (E fix).
        outcomes_by_kind_post = {
            row[0]: int(row[1])
            for row in conn.execute(
                """
                SELECT outcome, COUNT(*) FROM task_outcomes
                WHERE datetime(created_at) >= datetime(?)
                GROUP BY outcome
                """,
                (since,),
            ).fetchall()
        }
        # completed_unverified remaining (C fix surfaces them).
        completed_unverified_total = _metric(
            conn,
            "SELECT COUNT(*) FROM agent_tasks WHERE status='completed_unverified'",
        )
        # soul_update_suggestion dedup (F fix).
        soul_suggestion_facts = _metric(
            conn,
            "SELECT COUNT(*) FROM facts WHERE key LIKE 'soul_update_suggestion.%'",
        )
        # Critical events with turn_id (proves wrap is active).
        critical_with_turn_id = _metric(
            conn,
            """
            SELECT COUNT(*) FROM observe_stream
            WHERE datetime(timestamp) >= datetime(?)
              AND event_type IN ('dispatch_decision','brain_turn_start','brain_turn_complete')
              AND json_extract(payload, '$.turn_id') IS NOT NULL
            """,
            since,
        )
        critical_total = _metric(
            conn,
            """
            SELECT COUNT(*) FROM observe_stream
            WHERE datetime(timestamp) >= datetime(?)
              AND event_type IN ('dispatch_decision','brain_turn_start','brain_turn_complete')
            """,
            since,
        )
    finally:
        conn.close()

    coverage_pct = (
        events_with_turn_id_post / total_events_post if total_events_post else 0.0
    )
    critical_coverage_pct = (
        critical_with_turn_id / critical_total if critical_total else 0.0
    )
    channel_null_pct = (
        brain_fallback_null_channel_post / brain_fallback_post
        if brain_fallback_post
        else 0.0
    )
    return {
        "since": since,
        "total_events": total_events_post,
        "events_with_turn_id": events_with_turn_id_post,
        "turn_id_coverage_pct": round(coverage_pct, 4),
        "unique_turn_ids": unique_turn_ids_post,
        "turn_id_missing_count": turn_id_missing_post,
        "critical_events_total": critical_total,
        "critical_events_with_turn_id": critical_with_turn_id,
        "critical_coverage_pct": round(critical_coverage_pct, 4),
        "brain_fallback_tg_total": brain_fallback_post,
        "brain_fallback_tg_channel_null": brain_fallback_null_channel_post,
        "brain_fallback_tg_channel_null_pct": round(channel_null_pct, 4),
        "task_outcomes_by_kind": outcomes_by_kind_post,
        "completed_unverified_total": completed_unverified_total,
        "soul_suggestion_facts_total": soul_suggestion_facts,
    }


def diff_against_baseline(post: dict) -> dict:
    return {
        "channel_null_brain_fallback": {
            "baseline_pct": BASELINE["channel_null_brain_fallback_pct"],
            "post_pct": post["brain_fallback_tg_channel_null_pct"],
            "improved": post["brain_fallback_tg_channel_null_pct"] < BASELINE["channel_null_brain_fallback_pct"],
        },
        "turn_id_coverage": {
            "baseline_pct": BASELINE["turn_id_coverage_pct"],
            "post_pct": post["turn_id_coverage_pct"],
            "critical_post_pct": post["critical_coverage_pct"],
            "improved": post["turn_id_coverage_pct"] > BASELINE["turn_id_coverage_pct"],
        },
        "completed_unverified": {
            "baseline": BASELINE["completed_unverified_count"],
            "post_total": post["completed_unverified_total"],
        },
        "outcomes_alignment": {
            "baseline_success_count": BASELINE["task_outcomes_success_count"],
            "post_by_kind": post["task_outcomes_by_kind"],
            "has_usable_reply_unverified": "usable_reply_unverified" in post["task_outcomes_by_kind"],
        },
        "soul_suggestion_dedup": {
            "baseline_dup_count": BASELINE["soul_update_suggestion_duplicate_count"],
            "post_facts_total": post["soul_suggestion_facts_total"],
        },
        "turn_id_missing": {
            "post_count": post["turn_id_missing_count"],
            "comment": "non-zero means a critical event fired outside a turn_id_context (Kairos / heartbeat / recovery paths still need wiring)",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "claw.db")
    parser.add_argument(
        "--since",
        default="2026-05-24 03:32:26",
        help="UTC datetime of the restart that activated handle_text turn_id wrap",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "reports" / "behavior_audit",
    )
    parser.add_argument(
        "--run-extractor",
        action="store_true",
        help="Also run reports/behavior_audit/extract_behavior_audit.py to refresh the canonical-style report",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_id = generate_run_id()
    out_path = args.output_dir / f"post_turn_id_audit_{run_id}.json"

    post = compute_post_metrics(args.db, args.since)
    diff = diff_against_baseline(post)

    report = {
        "run_id": run_id,
        "since": args.since,
        "baseline": BASELINE,
        "post": post,
        "diff": diff,
    }
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    extractor_out = None
    if args.run_extractor:
        extractor = REPO_ROOT / "reports" / "behavior_audit" / "extract_behavior_audit.py"
        result = subprocess.run(
            [sys.executable, str(extractor), "--db", str(args.db), "--output-dir", str(args.output_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        extractor_out = (result.stdout or result.stderr).strip()

    print(
        json.dumps(
            {
                "run_id": run_id,
                "report": str(out_path),
                "since": args.since,
                "key_findings": {
                    "channel_null_post_pct": post["brain_fallback_tg_channel_null_pct"],
                    "turn_id_coverage_pct": post["turn_id_coverage_pct"],
                    "critical_coverage_pct": post["critical_coverage_pct"],
                    "turn_id_missing_count": post["turn_id_missing_count"],
                    "outcomes_by_kind": post["task_outcomes_by_kind"],
                    "completed_unverified_total": post["completed_unverified_total"],
                },
                "extractor_output": extractor_out,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
