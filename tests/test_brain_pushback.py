"""Brain pushback / autonomy contract presence tests.

Encode behavior Hector reported as missing in Wave 1:
- Brain offered A/B/C options when the obvious next action should execute.
- Brain did not push back on factually wrong / ambiguous premises.

Wave 2 added BRAIN_PUSHBACK_CONTRACT + extended CONVERSATIONAL_STYLE_CONTRACT.
These tests now verify the contracts are present in the assembled prompt.

Reference: Anthropic, "Sycophancy in personal guidance" (2026-04). The
prefill stress-test methodology described there is the validation method
behind these contracts (real prefill tests live separately as integration).
"""
from __future__ import annotations

import unittest

from claw_v2.brain import _brain_system_prompt


def _build_prompt() -> str:
    return _brain_system_prompt("you are an autonomous agent for testing")


class BrainPushbackContractTests(unittest.TestCase):
    """The brain prompt must explicitly authorize disagreement on bad premises."""

    def test_prompt_authorizes_pushback_on_wrong_or_ambiguous_premise(self) -> None:
        prompt = _build_prompt().lower()
        # Any of these phrasings counts as an explicit pushback authorization.
        cues = [
            "discrepa",
            "push back",
            "premisa",
            "premise is wrong",
            "premise is unclear",
            "compliance ≠ utilidad",
            "compliance != utility",
        ]
        self.assertTrue(
            any(cue in prompt for cue in cues),
            f"Brain prompt has no pushback authorization. Looked for any of: {cues}",
        )

    def test_prompt_discourages_offering_options_when_action_is_obvious(self) -> None:
        prompt = _build_prompt().lower()
        # The prompt should explicitly discourage A/B/C enumeration when one path
        # is obvious. Generic "be terse" is not enough — Hector reports it does
        # not stop option-spam in practice.
        cues = [
            "no ofrezcas a/b/c",
            "no a/b/c",
            "do not enumerate options",
            "decide one path",
            "decide un solo camino",
        ]
        self.assertTrue(
            any(cue in prompt for cue in cues),
            f"Brain prompt does not discourage A/B/C enumeration. Looked for: {cues}",
        )


class ConversationalStyleAntiDashboardTests(unittest.TestCase):
    """Conversational style contract must explicitly forbid Checkpoint/tablas."""

    def test_style_contract_explicitly_forbids_checkpoint_and_tables(self) -> None:
        from claw_v2.brain import CONVERSATIONAL_STYLE_CONTRACT

        contract = CONVERSATIONAL_STYLE_CONTRACT.lower()
        # Generic anti-template phrasing already exists. Hector reports it isn't
        # enough — the bot still drifts to "Checkpoint", tablas, and headers in
        # casual conversational turns. The contract should call them out by name.
        explicit_cues = ["checkpoint", "tabla", "no headers", "sin tablas", "sin checkpoint"]
        self.assertTrue(
            any(cue in contract for cue in explicit_cues),
            f"Conversational style contract does not name dashboard-format anti-patterns. Looked for: {explicit_cues}",
        )


if __name__ == "__main__":
    unittest.main()
