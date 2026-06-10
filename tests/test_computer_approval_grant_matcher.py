"""Regression tests for the computer-use approval grant matcher (2026-06-10 audit).

The matcher used substring containment, so unrelated messages containing
"sigue"/"continua"/"dale" as fragments ("consigue", "continuamos", "dale una
vuelta al plan") silently approved pending Tier-3 desktop actions. The matcher
now requires the whole normalized message to be an approval phrase.
"""
from __future__ import annotations

import unittest

from claw_v2.bot import (
    _looks_like_computer_approval_grant,
    _looks_like_computer_approval_reject,
)


class ComputerApprovalGrantMatcherTests(unittest.TestCase):
    def test_exact_approval_phrases_grant(self) -> None:
        for message in (
            "te autorizo",
            "Autorizo",
            "puedes continuar",
            "Continúa",
            "sigue",
            "hazlo",
            "dale",
            "sí",
            "ok",
            "approved",
        ):
            with self.subTest(message=message):
                self.assertTrue(_looks_like_computer_approval_grant(message))

    def test_explicit_authorization_verb_grants_inside_longer_message(self) -> None:
        for message in (
            "Abre ChatGPT y crea la imagen del mockup. Te autorizo",
            "lo apruebo, adelante con el click",
        ):
            with self.subTest(message=message):
                self.assertTrue(_looks_like_computer_approval_grant(message))

    def test_unrelated_messages_containing_grant_fragments_do_not_grant(self) -> None:
        for message in (
            "consigue el reporte de ventas",
            "continuamos mañana con esto",
            "dale una vuelta al plan antes",
            "el deploy sigue fallando, ¿por qué?",
            "hazlo cuando tengas el backup listo y avísame",
            "no te autorizo a nada más",
            "nunca te autorizo eso",
        ):
            with self.subTest(message=message):
                self.assertFalse(_looks_like_computer_approval_grant(message))

    def test_reject_phrases_still_win_over_grant(self) -> None:
        self.assertTrue(_looks_like_computer_approval_reject("no autorizo"))
        self.assertFalse(_looks_like_computer_approval_grant("no autorizo"))
        self.assertFalse(_looks_like_computer_approval_grant("cancela"))


if __name__ == "__main__":
    unittest.main()
