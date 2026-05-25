"""Tests for brain.py context/media poison sanitizer (P0 hotfix A).

Covers the failure mode that killed the 2026-05-24 22:50 turn:
provider rejects the conversation because of an unprocessable image,
retry reuses the same image and fails again with internal_trace_repeated.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.adapters.base import AdapterError
from claw_v2.brain import (
    BrainService,
    _QUARANTINED_IMAGE_PLACEHOLDER,
    _is_image_processing_error,
    _quarantine_image_blocks,
)
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse


_IMAGE_ERROR_MESSAGE = (
    "API Error: an image in the conversation could not be processed "
    "and was removed. Re-read the file with a different approach if you still need it."
)


def _image_prompt(text: str = "qué ves en esta foto?") -> list[dict]:
    return [
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "FAKE_BASE64"},
        },
    ]


def _ok_response(content: str = "<response>sin imagen, ok</response>") -> LLMResponse:
    return LLMResponse(
        content=content,
        lane="brain",
        provider="anthropic",
        model="claude-opus-4-7",
    )


class ImageProcessingErrorDetectionTests(unittest.TestCase):
    def test_detects_anthropic_image_phrase(self) -> None:
        self.assertTrue(_is_image_processing_error(AdapterError(_IMAGE_ERROR_MESSAGE)))

    def test_detects_could_not_process_image_phrase(self) -> None:
        self.assertTrue(
            _is_image_processing_error(AdapterError("Could not process image attachment"))
        )

    def test_unrelated_adapter_errors_are_not_image_poison(self) -> None:
        self.assertFalse(_is_image_processing_error(AdapterError("rate limit exceeded")))
        self.assertFalse(_is_image_processing_error(AdapterError("session corrupt")))


class QuarantineImageBlocksTests(unittest.TestCase):
    def test_string_prompt_is_passed_through(self) -> None:
        out, count = _quarantine_image_blocks("hola")
        self.assertEqual(out, "hola")
        self.assertEqual(count, 0)

    def test_image_blocks_become_placeholder_text(self) -> None:
        prompt = _image_prompt()
        sanitized, count = _quarantine_image_blocks(prompt)
        self.assertEqual(count, 1)
        self.assertIsInstance(sanitized, list)
        types = [b.get("type") for b in sanitized]
        self.assertNotIn("image", types)
        placeholders = [b for b in sanitized if b.get("text") == _QUARANTINED_IMAGE_PLACEHOLDER]
        self.assertEqual(len(placeholders), 1)

    def test_text_blocks_are_left_untouched(self) -> None:
        prompt = [{"type": "text", "text": "primera"}, {"type": "text", "text": "segunda"}]
        sanitized, count = _quarantine_image_blocks(prompt)
        self.assertEqual(count, 0)
        self.assertEqual(sanitized, prompt)


class _ImagePoisonHarness(unittest.TestCase):
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

    def _emitted_event_types(self) -> list[str]:
        rows = self.observe.recent_events(limit=100)
        return [row["event_type"] for row in rows]


class BrokenImageContextNotReusedTests(_ImagePoisonHarness):
    def test_broken_image_context_is_quarantined_and_not_reused(self) -> None:
        prompt = _image_prompt()
        self.router.ask.side_effect = [AdapterError(_IMAGE_ERROR_MESSAGE), _ok_response()]

        result = self.brain.handle_message("s1", prompt)

        self.assertEqual(result.content, "sin imagen, ok")
        self.assertEqual(self.router.ask.call_count, 2)
        retry_prompt = self.router.ask.call_args_list[1].args[0]
        self.assertIsInstance(retry_prompt, list)
        for block in retry_prompt:
            self.assertNotEqual(block.get("type"), "image")
        retry_texts = " ".join(b.get("text", "") for b in retry_prompt if b.get("type") == "text")
        self.assertIn(_QUARANTINED_IMAGE_PLACEHOLDER, retry_texts)


class ProviderSessionClearedOnImagePoisonTests(_ImagePoisonHarness):
    def test_provider_session_cleared_after_image_processing_failure(self) -> None:
        self.memory.link_provider_session("s1", "anthropic", "stale-sdk-session")
        prompt = _image_prompt()
        self.router.ask.side_effect = [AdapterError(_IMAGE_ERROR_MESSAGE), _ok_response()]

        self.brain.handle_message("s1", prompt)

        self.assertIsNone(self.memory.get_provider_session("s1", "anthropic"))
        retry_session = self.router.ask.call_args_list[1].kwargs.get("session_id")
        self.assertIsNone(retry_session)


class BrainRetryTextOnlyEventTests(_ImagePoisonHarness):
    def test_brain_retry_text_only_after_image_poison(self) -> None:
        prompt = _image_prompt()
        self.router.ask.side_effect = [AdapterError(_IMAGE_ERROR_MESSAGE), _ok_response()]

        self.brain.handle_message("s1", prompt)

        emitted = self._emitted_event_types()
        self.assertIn("media_context_quarantined", emitted)
        self.assertIn("brain_retry_text_only", emitted)
        self.assertNotIn("brain_retry_text_only_failed", emitted)

    def test_brain_retry_text_only_failed_event_when_retry_fails(self) -> None:
        prompt = _image_prompt()
        self.router.ask.side_effect = [
            AdapterError(_IMAGE_ERROR_MESSAGE),
            AdapterError("still broken after sanitization"),
        ]

        with self.assertRaises(AdapterError):
            self.brain.handle_message("s1", prompt)

        emitted = self._emitted_event_types()
        self.assertIn("media_context_quarantined", emitted)
        self.assertIn("brain_retry_text_only", emitted)
        self.assertIn("brain_retry_text_only_failed", emitted)


if __name__ == "__main__":
    unittest.main()
