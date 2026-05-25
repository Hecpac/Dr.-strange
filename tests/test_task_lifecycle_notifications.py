from __future__ import annotations

import unittest

from claw_v2.lifecycle import (
    format_task_ledger_terminal_message,
    should_notify_task_ledger_terminal,
)


class TaskLifecycleNotificationTests(unittest.TestCase):
    def test_notifies_non_coordinator_telegram_terminal_task(self) -> None:
        notified: set[str] = set()
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "brain_fallback",
            "status": "succeeded",
            "verification_status": "needs_verification",
            "summary": "brain tool-use produced a useful summary",
        }

        self.assertTrue(should_notify_task_ledger_terminal(payload, notified))
        message = format_task_ledger_terminal_message(payload)
        self.assertIn("Registré resultado", message)
        self.assertIn("pendiente de verificacion", message)
        self.assertIn("ejecucion con herramientas", message)
        self.assertNotIn("needs_verification", message)
        self.assertNotIn("task-1", message)

    def test_skips_synthetic_brain_tooluse_task(self) -> None:
        payload = {
            "task_id": "brain-tooluse:tg-123:999",
            "session_id": "tg-123",
            "runtime": "telegram",
            "status": "succeeded",
            "verification_status": "needs_verification",
            "summary": "brain tool-use turn: 8 tool calls (unverified)",
            "metadata": {
                "brain_tool_use": True,
                "created_by": "brain_tool_use_ledger",
            },
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, set()))

    def test_skips_inline_preflight_blocker_task(self) -> None:
        payload = {
            "task_id": "tg-123:blocked:999",
            "session_id": "tg-123",
            "runtime": "telegram_preflight",
            "status": "failed",
            "verification_status": "blocked",
            "notify_policy": "none",
            "summary": "Task blocked during capability preflight",
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, set()))

    def test_skips_regular_coordinator_terminal_task_to_avoid_duplicate(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "coordinator",
            "status": "succeeded",
            "verification_status": "passed",
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, set()))

    def test_notifies_lost_coordinator_task_from_watchdog(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "coordinator",
            "status": "lost",
            "verification_status": "failed",
            "error": "runtime lost authoritative backing state",
        }

        self.assertTrue(should_notify_task_ledger_terminal(payload, set()))
        message = format_task_ledger_terminal_message(payload)
        self.assertIn("No pude cerrar", message)
        self.assertIn("sin estado ejecutable", message)
        self.assertNotIn("runtime lost authoritative backing state", message)
        self.assertNotIn("task-1", message)

    def test_dedupes_already_notified_task(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "telegram_imperative",
            "status": "failed",
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, {"task-1"}))


if __name__ == "__main__":
    unittest.main()
