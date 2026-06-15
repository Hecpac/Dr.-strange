from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.bot_helpers import _extract_pending_action_from_reply, _looks_like_proceed_request
from claw_v2.memory import MemoryStore
from claw_v2.state_handler import StateHandler, _BrainShortcut


class _TaskHandler:
    def derive_task_dependencies(self, *_args, **_kwargs):
        return []

    def upsert_task_queue_entry(
        self, queue, *, summary, mode, status, source, priority, depends_on
    ):
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

    def test_no_context_falls_through_to_brain(self) -> None:
        # SOUL routing policy (2026-06-10 audit A5): proceed-class turns the
        # state handler cannot resolve fall through silently to the brain —
        # no pre-brain clarification question.
        _memory, handler = self._handler()

        reply = handler.maybe_resolve_stateful_followup("Procede", session_id="s1")

        self.assertIsNone(reply)

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
            "Ármalo",
            "Armalo",
            "Ya esta desbloqueada",
            "Ya está desbloqueada",
            "Ya entré al escritorio",
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
            "Go",
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
            ("Plan: lanzar el smoke test de Inworld TTS-2 y comparar con Sal.\n¿Lo arranco?"),
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dale", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("smoke test de Inworld", shortcut.text)
        self.assertIn("recent_assistant", shortcut.text)

    def test_dale_resolves_recent_mi_voto_recommendation(self) -> None:
        memory, handler = self._handler()
        memory.store_message(
            "s1",
            "assistant",
            (
                "Conclusión: la seguridad ya no es el bloqueador.\n\n"
                "¿Quieres que (a) persista esta reconciliación en `docs/audit/`, "
                "(b) siga y barra los 5 highs restantes, o (c) volvamos al outreach?\n"
                "Mi voto: persisto la nota (2 min) y pasamos al outreach, "
                "que es lo que mueve plata."
            ),
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dale", session_id="s1")
        state = memory.get_session_state("s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("acción propuesta previamente", shortcut.text)
        self.assertIn("persisto la nota", shortcut.text)
        self.assertIn("recent_assistant", shortcut.text)
        self.assertIn("persisto la nota", state["pending_action"])

    def test_resolves_markdown_pending_line_from_reply_context(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            active_object={
                "reply_context": {
                    "source": "telegram_reply",
                    "text": (
                        "**Checkpoint:**\n"
                        "- **Hecho:** inspeccion de observe_stream post-restart.\n"
                        "- **Pendiente:** validacion de la rama nueva (`brain_shortcut`) "
                        '— requiere un "Procede"/"Continua" pelado de tu parte.\n'
                        "Sigo activo esperando."
                    ),
                    "created_at": time.time(),
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Continúa", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("acción propuesta previamente", shortcut.text)
        self.assertIn("validacion de la rama nueva", shortcut.text)
        self.assertNotIn("requiere un", shortcut.text)

    def test_armalo_resolves_dime_y_lo_armo_reply_context(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            active_object={
                "reply_context": {
                    "source": "telegram_reply",
                    "text": (
                        'Si quieres, convierto el bloque "skills are the prompts + loop engineering" '
                        "en el primer post de tu cadena semanal. Dime y lo armo."
                    ),
                    "created_at": time.time(),
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Ármalo", session_id="s1")
        state = memory.get_session_state("s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("acción propuesta previamente", shortcut.text)
        self.assertIn("primer post de tu cadena semanal", shortcut.text)
        self.assertNotIn("Dime y lo armo", shortcut.text)
        self.assertIn("primer post de tu cadena semanal", state["pending_action"])

    def test_unlock_notice_resolves_chatgpt_option(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            last_options=[
                "Termina de entrar al escritorio y me avisas → voy por ChatGPT con la referencia.",
                '"Dale con nano banana" → arranco ahora mismo.',
            ],
            active_object={
                "last_options_meta": {
                    "created_at": time.time(),
                    "source": "assistant_numbered_options",
                    "topic": "ChatGPT bloqueado por pantalla",
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Ya esta desbloqueada", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("ChatGPT con la referencia", shortcut.text)
        self.assertIn("last_options_unlock_ready", shortcut.text)
        self.assertNotIn("nano banana", shortcut.text)

    def test_textual_option_selection_beats_generic_dale_pending_action(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            pending_action="Ir por ChatGPT cuando la Mac quede desbloqueada",
            last_options=[
                "Termina de entrar al escritorio y me avisas → voy por ChatGPT con la referencia.",
                '"Dale con nano banana" → arranco ahora mismo.',
            ],
            active_object={
                "last_options_meta": {
                    "created_at": time.time(),
                    "source": "assistant_numbered_options",
                    "topic": "ChatGPT bloqueado por pantalla",
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dale con nano banana", session_id="s1")
        state = memory.get_session_state("s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("Opción elegida (2)", shortcut.text)
        self.assertIn("nano banana", shortcut.text)
        self.assertEqual(state["pending_action"], '"Dale con nano banana" → arranco ahora mismo.')

    def test_extracts_unlock_resume_action_from_assistant_reply(self) -> None:
        reply = (
            "Apenas la desbloquees, ejecuto solo: chat nuevo en ChatGPT → "
            "pego tu imagen de referencia → genero el tile hero."
        )

        pending = _extract_pending_action_from_reply(reply)

        self.assertEqual(
            pending,
            "chat nuevo en ChatGPT → pego tu imagen de referencia → genero el tile hero.",
        )


if __name__ == "__main__":
    unittest.main()
