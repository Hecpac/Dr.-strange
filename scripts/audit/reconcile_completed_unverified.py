#!/usr/bin/env python3
"""Dry-run reconciliation queue for `completed_unverified` ledger rows.

Reads `data/claw.db` in read-only mode, never mutates the runtime DB,
and writes a sanitized JSON report listing every pending row with the
metadata reviewers need (channel, tools, verification status, summary,
recommended action, deadline). Emits a
``pending_verification_reconciliation`` observe event recording the
review.

Usage::

    python scripts/audit/reconcile_completed_unverified.py \
        --db data/claw.db \
        --output reports/behavior_audit/reconciliation_<runid>.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claw_v2.behavior_audit_io import generate_run_id
from claw_v2.observe import ObserveStream
from claw_v2.reconciliation import (
    DEFAULT_RECONCILIATION_DEADLINE_SECONDS,
    write_reconciliation_report,
)
from claw_v2.task_ledger import TaskLedger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "claw.db")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "reports" / "behavior_audit",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=DEFAULT_RECONCILIATION_DEADLINE_SECONDS,
        help="How many seconds after mark_terminal to expect verifier signal.",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_id = generate_run_id()
    out_path = args.output_dir / f"reconciliation_{run_id}.json"

    # Open the ledger; the constructor only reads schema and applies the
    # additive migration for completed_unverified (no row mutation).
    ledger = TaskLedger(args.db)
    observe = ObserveStream(args.db)
    written = write_reconciliation_report(
        ledger,
        out_path,
        deadline_seconds=args.deadline_seconds,
        observe=observe,
    )
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
