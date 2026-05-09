from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.bot_helpers import _looks_like_proceed_request
from claw_v2.memory import MemoryStore
from claw_v2.state_handler import StateHandler, _BrainShortcut


class _TaskHandler:
    def derive_task_dependencies(self, *_args, **_kwargs):
        return []

    def upsert_task_queue_entry(self, queue, *, summary, mode, status, source, priority, depends_on):
        return [
            *queue,
            {
                "task_id": f"{mode}:{source}:{summary.replace(' ', '-')}",
                "summary": summary,
                "mode": mode,
                "status": status,
                "source": source,
                "priority": priority,
                "depends_on": depends_on,
            },
        ]

    def mark_first_task_queue_entry(self, queue, *, from_status, to_status):
        return queue

    def mark_task_queue_in_progress(self, queue, *, summary=None, task_id=None):
        updated = []
        for item in queue:
            if item.get("summary") == summary or item.get("task_id") == task_id:
                updated.append({**item, "status": "in_progress"})
            else:
                updated.append(item)
        return updated


class ContinuationResolverTests(unittest.TestCase):
    def _handler(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryStore(Path(tmp.name) / "claw.db")
        return memory, StateHandler(brain_memory=memory, task_handler=_TaskHandler())

    def test_procede_resolves_against_reply_context(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            active_object={
                "reply_context": {
                    "source": "telegram_reply",
                    "text": (
                        "Voy a corregir el handler de continuaciones y agregar tests. "
                        "Toma ~3-5 min. ¿Lo arranco?"
                    ),
                    "created_at": time.time(),
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Procede", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("acción propuesta previamente", shortcut.text)
        self.assertIn("corregir el handler", shortcut.text)
        self.assertIn("reply_context", shortcut.text)

    def test_si_resolves_against_pending_action(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            pending_action="reiniciar daemon con scripts/restart.sh",
        )

        shortcut = handler.maybe_resolve_stateful_followup("Sí", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("reiniciar daemon", shortcut.text)

    def test_no_context_falls_back_to_question(self) -> None:
        _memory, handler = self._handler()

        reply = handler.maybe_resolve_stateful_followup("Procede", session_id="s1")

        self.assertIsInstance(reply, str)
        self.assertIn("Qué acción concreta", reply)

    def test_continuation_pattern_matches_variants(self) -> None:
        for word in (
            "procede",
            "Procede",
            "sí",
            "si",
            "dale",
            "Dale",
            "avanza",
            "Avanza",
            "hazlo",
            "ok",
            "Okay",
            "vale",
            "Vale",
            "adelante",
            "Adelante",
            "continúa",
            "continua",
        ):
            with self.subTest(word=word):
                self.assertTrue(
                    _looks_like_proceed_request(word),
                    msg=f"expected continuation match for {word!r}",
                )

    def test_uppercase_and_punctuation(self) -> None:
        for word in (
            "PROCEDE.",
            "Procede.",
            "Dale!",
            "ok.",
            "OK!",
            "Sí.",
            "Vale.",
            "Adelante!",
        ):
            with self.subTest(word=word):
                self.assertTrue(
                    _looks_like_proceed_request(word),
                    msg=f"expected continuation match for {word!r}",
                )

    def test_resolves_proposal_from_recent_assistant_message(self) -> None:
        memory, handler = self._handler()
        memory.store_message(
            "s1",
            "assistant",
            (
                "Plan: lanzar el smoke test de Inworld TTS-2 y comparar con Sal.\n"
                "¿Lo arranco?"
            ),
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dale", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("smoke test de Inworld", shortcut.text)
        self.assertIn("recent_assistant", shortcut.text)


if __name__ == "__main__":
    unittest.main()
