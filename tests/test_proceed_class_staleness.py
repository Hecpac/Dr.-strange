"""Regression tests for proceed-class continuation staleness.

State_handler resolves "proceed/dale/sí" by selecting the next pending task
from `task_queue`. Stale pending entries should not be selected just because
they appear earlier in iteration order.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from claw_v2.bot_helpers import _select_next_task_queue_item


def _stale_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _fresh_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class ProceedClassStalenessTests(unittest.TestCase):
    def test_select_next_skips_stale_entries_older_than_threshold(self) -> None:
        # 7 days old; should be considered abandoned in any reasonable design.
        task_queue = [
            {
                "task_id": "old",
                "status": "pending",
                "summary": "vieja propuesta abandonada",
                "mode": "operator",
                "created_at": _stale_iso(7 * 86400),
            },
        ]
        result = _select_next_task_queue_item(task_queue, preferred_mode="operator")
        self.assertIsNone(
            result,
            "Stale pending entry should be filtered out; today it is returned because no TTL is enforced.",
        )

    def test_select_next_prefers_fresh_entry_over_stale(self) -> None:
        task_queue = [
            {
                "task_id": "stale",
                "status": "pending",
                "summary": "vieja",
                "mode": "operator",
                "created_at": _stale_iso(7 * 86400),
            },
            {
                "task_id": "fresh",
                "status": "pending",
                "summary": "fresca",
                "mode": "operator",
                "created_at": _fresh_iso(60),
            },
        ]
        result = _select_next_task_queue_item(task_queue, preferred_mode="operator")
        self.assertIsNotNone(result)
        self.assertEqual(
            result.get("task_id"),
            "fresh",
            "Fresh entry should be preferred over stale; today the iteration order alone decides.",
        )


if __name__ == "__main__":
    unittest.main()
