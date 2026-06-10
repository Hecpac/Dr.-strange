from __future__ import annotations

import unittest

from claw_v2.lifecycle import (
    format_autonomous_task_terminal_message,
    format_task_ledger_terminal_message,
    should_notify_task_ledger_terminal,
)


class TaskLifecycleNotificationTests(unittest.TestCase):
    def test_skips_successful_telegram_terminal_task_to_avoid_system_completion_message(self) -> None:
        notified: set[str] = set()
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "brain_fallback",
            "status": "succeeded",
            "verification_status": "needs_verification",
            "summary": "brain tool-use produced a useful summary",
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, notified))

    def test_skips_inline_handler_result_that_normal_turn_already_returns(self) -> None:
        payload = {
            "task_id": "nlm-123",
            "session_id": "tg-123",
            "runtime": "nlm_natural_language",
            "status": "succeeded",
            "verification_status": "passed",
            "summary": "Notebook creado: 123 — tema",
            "artifacts": {"handler_result": "Notebook creado: 123 — tema"},
            "metadata": {"intent": "create_notebook", "turn_id": "turn-1"},
        }

        self.assertFalse(should_notify_task_ledger_terminal(payload, set()))

    def test_terminal_message_formatter_keeps_internal_status_out_of_successful_egress_path(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "runtime": "brain_fallback",
            "status": "succeeded",
            "verification_status": "needs_verification",
            "summary": "brain tool-use produced a useful summary",
        }

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

    def test_autonomous_completion_message_uses_response_without_system_header(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "response": "Notebook creado: 123 — tema",
            "verification_status": "passed",
        }

        message = format_autonomous_task_terminal_message(payload)

        self.assertEqual(message, "Notebook creado: 123 — tema")
        self.assertNotIn("Cerré", message)
        self.assertNotIn("Verificación", message)
        self.assertNotIn("task-1", message)

    def test_autonomous_failure_message_uses_error_without_task_id_or_verification(self) -> None:
        payload = {
            "task_id": "task-1",
            "session_id": "tg-123",
            "error": "provider timed out",
            "verification_status": "failed",
        }

        message = format_autonomous_task_terminal_message(payload, failed=True)

        self.assertIn("No pude completar eso.", message)
        self.assertIn("provider timed out", message)
        self.assertNotIn("No pude cerrar", message)
        self.assertNotIn("Verificación", message)
        self.assertNotIn("task-1", message)


if __name__ == "__main__":
    unittest.main()
