from __future__ import annotations

import unittest
from types import SimpleNamespace

from claw_v2.bot import BotService


class _StubBrain:
    """Records handle_message calls and returns queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.memory = SimpleNamespace(get_session_state=lambda sid: {})

    def handle_message(self, session_id, message, *, memory_text=None, task_type=None):
        self.calls.append(message)
        return self._responses.pop(0)


def _resp(content, *, tools=None):
    return SimpleNamespace(content=content, tool_calls=tools or [])


def _bot(brain, *, evidence_signal_for=None):
    bot = BotService.__new__(BotService)
    bot.brain = brain
    events: list[tuple[str, dict]] = []
    bot._emit_safe = lambda et, payload=None: events.append((et, payload or {}))
    bot._events = events
    # A response "has evidence" iff its content is in evidence_signal_for.
    signal = set(evidence_signal_for or [])
    bot._response_has_evidence_signal = lambda r: getattr(r, "content", "") in signal
    bot._session_has_fresh_evidence = lambda sid: False
    return bot


# The narrated-without-action reply that trips the classifier: an operator
# action request + a starting-side-effect claim, no evidence.
_ASK = "ejecuta la limpieza de la cola de approvals"
_NARRATED = "Voy a ejecutar la limpieza de la cola de approvals ahora mismo."


class F4B2AutoRepromptTests(unittest.TestCase):
    def _run(self, bot, brain, source_text=_ASK, first=_NARRATED):
        return bot._maybe_auto_reprompt_unexecuted_action(
            session_id="tg-1",
            source_text=source_text,
            response=_resp(first),
        )

    def test_narrated_without_action_triggers_one_reprompt(self) -> None:
        # First reply narrates; the re-prompt executes (evidence signal).
        executed = _resp("Hecho: corrí la limpieza (evidencia).")
        brain = _StubBrain([executed])
        bot = _bot(brain, evidence_signal_for=[executed.content])

        out = self._run(bot, brain)

        self.assertIs(out, executed)  # returns the re-prompted response
        self.assertEqual(len(brain.calls), 1)  # exactly ONE re-prompt
        self.assertIn("EJECUTA AHORA", brain.calls[0])
        types = [e for e, _ in bot._events]
        self.assertIn("f4b2_auto_reprompt_issued", types)
        result = next(p for e, p in bot._events if e == "f4b2_auto_reprompt_result")
        self.assertTrue(result["executed"])

    def test_second_narration_falls_through_to_evidence_gate(self) -> None:
        # The re-prompt STILL narrates (no evidence). We return it unchanged so
        # the downstream evidence gate (F4-B2a) retains it and surfaces blocked
        # clearly — and we do NOT re-prompt again (structural max-one).
        still_narrating = _resp("Voy a ejecutar la limpieza, en serio esta vez.")
        brain = _StubBrain([still_narrating])
        bot = _bot(brain, evidence_signal_for=[])  # nothing has evidence

        out = self._run(bot, brain)

        self.assertIs(out, still_narrating)
        self.assertEqual(len(brain.calls), 1)  # only ONE re-prompt, no loop
        result = next(p for e, p in bot._events if e == "f4b2_auto_reprompt_result")
        self.assertFalse(result["executed"])

    def test_successful_delegation_stops_loop(self) -> None:
        # A re-prompt that delegates (evidence signal from the delegation) is
        # accepted and no further re-prompt happens.
        delegated = _resp("Delegué la tarea autónoma tg-1:123 (evidencia).")
        brain = _StubBrain([delegated])
        bot = _bot(brain, evidence_signal_for=[delegated.content])

        out = self._run(bot, brain)

        self.assertIs(out, delegated)
        self.assertEqual(len(brain.calls), 1)

    def test_normal_answer_does_not_trigger(self) -> None:
        # A plain informational answer (no action claim) never re-prompts.
        brain = _StubBrain([])  # would IndexError if a re-prompt were issued
        bot = _bot(brain)
        original = _resp("El módulo de approvals tiene 648 líneas y 17 métodos.")

        out = bot._maybe_auto_reprompt_unexecuted_action(
            session_id="tg-1", source_text="cuántas líneas tiene approvals", response=original
        )

        self.assertIs(out, original)
        self.assertEqual(brain.calls, [])
        self.assertNotIn("f4b2_auto_reprompt_issued", [e for e, _ in bot._events])

    def test_reply_with_tool_evidence_does_not_trigger(self) -> None:
        # The brain already ran a verifying tool this turn — no re-prompt.
        brain = _StubBrain([])
        executed_first = _resp("Voy a ejecutar la limpieza — hecho, aquí la evidencia.")
        bot = _bot(brain, evidence_signal_for=[executed_first.content])

        out = bot._maybe_auto_reprompt_unexecuted_action(
            session_id="tg-1", source_text=_ASK, response=executed_first
        )

        self.assertIs(out, executed_first)
        self.assertEqual(brain.calls, [])

    def test_none_response_is_noop(self) -> None:
        brain = _StubBrain([])
        bot = _bot(brain)
        out = bot._maybe_auto_reprompt_unexecuted_action(
            session_id="tg-1", source_text=_ASK, response=None
        )
        self.assertIsNone(out)
        self.assertEqual(brain.calls, [])


if __name__ == "__main__":
    unittest.main()
