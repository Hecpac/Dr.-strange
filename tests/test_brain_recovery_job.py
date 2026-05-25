"""Tests for brain.py no-lost-task recovery (P0 hotfix B).

When the brain fails on an actionable request after retries, it must persist
a recovery job (status=pending_recovery) and reply with a recovery message
instead of the bare INTERNAL_TOOL_TRACE_FALLBACK.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.adapters.base import AdapterError
from claw_v2.brain import (
    BrainService,
    INTERNAL_TOOL_TRACE_FALLBACK,
    _request_looks_actionable,
)
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse


_IMAGE_ERROR = (
    "API Error: an image in the conversation could not be processed and was removed."
)


def _actionable_image_prompt(text: str = "arregla el deployment de producción ahora") -> list[dict]:
    return [
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "FAKE"},
        },
    ]


class RequestLooksActionableTests(unittest.TestCase):
    def test_action_verbs_match(self) -> None:
        self.assertTrue(_request_looks_actionable("arregla el bug del login"))
        self.assertTrue(_request_looks_actionable("crea un agente que postee en X"))
        self.assertTrue(_request_looks_actionable("ejecuta el deploy ahora"))

    def test_short_or_smalltalk_does_not_match(self) -> None:
        self.assertFalse(_request_looks_actionable(""))
        self.assertFalse(_request_looks_actionable("hola"))
        self.assertFalse(_request_looks_actionable("ok"))
        self.assertFalse(_request_looks_actionable("status"))

    def test_common_spanish_imperatives_match(self) -> None:
        """Regression: prior allowlist missed verbs Hector actually types,
        causing recovery to drop the task on provider error."""
        for phrase in (
            "parchea el bug del login que se rompió ayer",
            "finaliza el deploy a producción",
            "termina la migración de la tabla users",
            "continúa con la tarea del onboarding",
            "continua con eso por favor",
            "sigue con la auditoría de los webhooks",
            "dale al pipeline de QTS",
            "aplica el patch en main",
            "borra el branch viejo de feature/x",
            "elimina las filas duplicadas en orders",
            "envía el reporte de fallos al admin",
            "cierra el ticket de stripe",
        ):
            self.assertTrue(
                _request_looks_actionable(phrase),
                f"expected actionable: {phrase!r}",
            )


class _BrainRecoveryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.memory = MemoryStore(self.db_path)
        self.router = MagicMock()
        self.router.config.max_budget_usd = 1.0
        self.observe = ObserveStream(self.db_path)
        self.brain = BrainService(
            router=self.router,
            memory=self.memory,
            system_prompt="You are Claw.",
            observe=self.observe,
        )


class FailedBrainTurnCreatesRecoveryJobTests(_BrainRecoveryHarness):
    def test_failed_brain_turn_creates_recovery_job(self) -> None:
        self.router.ask.side_effect = [
            AdapterError(_IMAGE_ERROR),
            AdapterError("still broken after sanitization"),
        ]

        result = self.brain.handle_message("s1", _actionable_image_prompt())

        self.assertIsNotNone(result)
        jobs = self.memory.list_pending_recovery_jobs("s1")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["status"], "pending_recovery")
        self.assertEqual(job["failure_reason"], "provider_context_media_poison")
        self.assertIn("arregla el deployment", job["original_request_sanitized"])
        self.assertTrue(job["turn_id"])

    def test_non_actionable_request_still_raises_and_does_not_create_job(self) -> None:
        self.router.ask.side_effect = [AdapterError("provider crashed")]

        with self.assertRaises(AdapterError):
            self.brain.handle_message("s1", "hola")

        jobs = self.memory.list_pending_recovery_jobs("s1")
        self.assertEqual(jobs, [])


class GenericApologyNotUsedWhenRecoveryJobCreatedTests(_BrainRecoveryHarness):
    def test_generic_apology_not_used_when_recovery_job_created(self) -> None:
        self.router.ask.side_effect = [
            AdapterError(_IMAGE_ERROR),
            AdapterError("still broken after sanitization"),
        ]

        result = self.brain.handle_message("s1", _actionable_image_prompt())

        self.assertNotIn(INTERNAL_TOOL_TRACE_FALLBACK, result.content)
        lowered = result.content.lower()
        self.assertTrue(
            "recovery" in lowered or "cola" in lowered or "queued" in lowered,
            f"recovery wording missing in response: {result.content!r}",
        )
        jobs = self.memory.list_pending_recovery_jobs("s1")
        self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()
