from __future__ import annotations

import unittest

from claw_v2.bot_helpers import (
    _computer_instruction_requires_actions,
    _looks_like_computer_read_request,
    _normalize_command_text,
)


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


if __name__ == "__main__":
    unittest.main()
