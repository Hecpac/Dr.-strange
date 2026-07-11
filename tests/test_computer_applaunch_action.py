from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.bot_helpers import (
    _computer_instruction_requires_actions,
    _looks_like_computer_read_request,
    _normalize_command_text,
)
from claw_v2.bot_commands import CommandContext
from claw_v2.computer_handler import ComputerHandler


class ComputerAppLaunchActionTests(unittest.TestCase):
    """Bot breakage diagnosis 2026-07-06: launching a native app via /computer
    was classified as a read (screenshot only) because no action token matched,
    so the app never launched (Calculadora incident). App-launch phrasings must
    now classify as ACTION, while reads stay reads."""

    def test_app_launch_phrasings_are_actions(self) -> None:
        for text in (
            "abre la app Calculadora y dime qué ves",
            "abre la aplicación Finder",
            "abre el programa Vista Previa",
            "open the app Calculator",
            "open the application Calculator",
            "open the program Preview",
            "launch the app Calculator",
            "lanza la app Notas",
        ):
            with self.subTest(text=text):
                self.assertTrue(_computer_instruction_requires_actions(text))

    def test_bare_launch_verb_does_not_hijack_ordinary_prompts(self) -> None:
        # Codex #223: the classifier runs on general non-slash messages, so a
        # bare "launch " token would mis-route ordinary prompts to computer
        # control. Only qualified app/application/program launches match.
        for text in (
            "draft a launch plan for the campaign",
            "prelaunch checklist review",
            "cuándo es el launch del producto",
        ):
            with self.subTest(text=text):
                self.assertFalse(_computer_instruction_requires_actions(text))

    def test_reads_stay_reads(self) -> None:
        # A pure read must NOT be promoted to an action by the new tokens.
        for text in (
            "dime qué ves en la pantalla",
            "revisa la pantalla",
            "describe la pantalla",
            "qué hay en la pantalla",
        ):
            with self.subTest(text=text):
                self.assertFalse(_computer_instruction_requires_actions(text))
                self.assertTrue(_looks_like_computer_read_request(_normalize_command_text(text)))

    def test_bare_abre_does_not_match_page_read(self) -> None:
        # "abre la pagina actual" is a read, not an app launch — bare "abre"
        # was intentionally not added, so this must stay a non-action.
        self.assertFalse(_computer_instruction_requires_actions("abre la pagina actual y revisala"))

    def test_calculadora_incident_instruction_launches(self) -> None:
        # The exact shape from Turn B of the incident: mixed launch + read. The
        # action classification wins so the app is launched (the downstream
        # codex-desktop loop then handles the "dime qué ves").
        self.assertTrue(
            _computer_instruction_requires_actions("abre la app Calculadora del Mac y dime qué ves")
        )


class ExplicitComputerCommandRoutingTests(unittest.TestCase):
    @staticmethod
    def _context(text: str) -> CommandContext:
        return CommandContext(user_id="123", session_id="s1", text=text, stripped=text)

    def test_exact_calculator_incident_uses_capability_gated_action_path(self) -> None:
        handler = ComputerHandler()
        handler.action_response = MagicMock(return_value="action")
        handler.computer_response = MagicMock(return_value="read")
        text = (
            "/computer Abre Calculator, calcula 17 por 23, deja el resultado visible y "
            "toma una captura como evidencia. No cierres otras apps, no guardes archivos, "
            "no cambies settings y no hagas ninguna otra acción."
        )

        result = handler.handle_command(self._context(text))

        self.assertEqual(result, "action")
        handler.action_response.assert_called_once_with(text.split(maxsplit=1)[1], "s1")
        handler.computer_response.assert_not_called()

    def test_explicit_pure_read_keeps_screenshot_analysis_path(self) -> None:
        handler = ComputerHandler()
        handler.action_response = MagicMock(return_value="action")
        handler.computer_response = MagicMock(return_value="read")
        text = "/computer revisa la pantalla y dime qué ves"

        result = handler.handle_command(self._context(text))

        self.assertEqual(result, "read")
        handler.computer_response.assert_called_once_with(text.split(maxsplit=1)[1], "s1")
        handler.action_response.assert_not_called()

    def test_unavailable_control_fails_before_screenshot_or_brain_tools(self) -> None:
        computer = MagicMock()
        brain = MagicMock()
        handler = ComputerHandler(
            computer=computer,
            brain_handle_message=brain,
            capability_check=lambda name, _fallback: (
                "computer control unavailable" if name == "computer_control" else None
            ),
        )
        text = "/computer Abre Calculator, calcula 17 por 23"

        result = handler.handle_command(self._context(text))

        self.assertEqual(result, "computer control unavailable")
        computer.capture_screenshot.assert_not_called()
        brain.assert_not_called()
        self.assertEqual(handler._sessions, {})


if __name__ == "__main__":
    unittest.main()
