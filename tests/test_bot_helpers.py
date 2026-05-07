from __future__ import annotations

import unittest

from claw_v2.bot_helpers import (
    _chat_response_has_internal_leak,
    _extract_option_reference,
    _extract_ratio_context_from_text,
    _looks_like_ratio_reference_request,
    _sanitize_chat_response,
)


class BotHelperRegressionTests(unittest.TestCase):
    def test_dame_los_2_ratios_is_not_option_2(self) -> None:
        self.assertIsNone(_extract_option_reference("Dame los 2 ratios"))
        self.assertTrue(_looks_like_ratio_reference_request("Dame los 2 ratios"))

    def test_vamos_con_la_2_selects_option_2(self) -> None:
        self.assertEqual(_extract_option_reference("Vamos con la 2"), 2)
        self.assertEqual(_extract_option_reference("vamos con opción 2"), 2)

    def test_ratio_context_extracts_both_pending_ratios(self) -> None:
        text = "Te tiro los otros 2 ratios: 9:16 vertical para Reels y 1:1 cuadrado para feed."
        self.assertEqual(
            _extract_ratio_context_from_text(text),
            ["9:16 vertical", "1:1 cuadrado"],
        )

    def test_visible_chat_response_suppresses_internal_runtime_details(self) -> None:
        text = (
            "Message ID 123 a tu chat tg-abc. localhost:8765 terminal bridge "
            "message_id 99 chat_id 456 nlm-abc blocked model response /Users/hector/private/file.txt"
        )
        sanitized = _sanitize_chat_response(text)

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("Message ID", sanitized)
        self.assertNotIn("message_id", sanitized)
        self.assertNotIn("chat_id", sanitized)
        self.assertNotIn("tg-", sanitized)
        self.assertNotIn("nlm-", sanitized)
        self.assertNotIn("localhost", sanitized)
        self.assertNotIn("terminal bridge", sanitized.lower())
        self.assertNotIn("/Users/hector", sanitized)

    def test_visible_chat_response_replaces_internal_trace_fallback_text(self) -> None:
        text = (
            "La salida del modelo contenía trazas internas de herramientas y la oculté. "
            "Repite la instrucción y la ejecuto limpio."
        )
        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("salida del modelo", lowered)
        self.assertNotIn("trazas internas", lowered)
        self.assertNotIn("herramientas internas", lowered)
        self.assertNotIn("la oculté", lowered)
        self.assertNotIn("respuesta bloqueada", lowered)
        self.assertNotIn("sanitizer", lowered)
        self.assertNotIn("blocked model response", lowered)
        self.assertNotIn("repite la instrucción", lowered)

    def test_visible_chat_response_replaces_raw_tool_trace(self) -> None:
        sanitized = _sanitize_chat_response('to=functions.exec_command {"cmd":"pwd"}')

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("to=functions", sanitized)

    def test_visible_chat_response_replaces_prompt_echo_and_role_echo(self) -> None:
        samples = [
            "# Telegram message\nReply ONLY to that latest message.",
            "user: Estatus",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized = _sanitize_chat_response(sample)
                lowered = sanitized.lower()

                self.assertFalse(_chat_response_has_internal_leak(sanitized))
                self.assertNotIn("telegram message", lowered)
                self.assertNotIn("reply only", lowered)
                self.assertNotIn("user:", lowered)


if __name__ == "__main__":
    unittest.main()
