from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

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
        updated = []
        transitioned = False
        for item in queue:
            current = dict(item)
            if not transitioned and current.get("status") == from_status:
                current["status"] = to_status
                transitioned = True
            updated.append(current)
        return updated

    def mark_task_queue_in_progress(self, queue, *, summary=None, task_id=None):
        updated = []
        for item in queue:
            if item.get("summary") == summary or item.get("task_id") == task_id:
                updated.append({**item, "status": "in_progress"})
            else:
                updated.append(item)
        return updated


class StateHandlerRegressionTests(unittest.TestCase):
    def _handler(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryStore(Path(tmp.name) / "claw.db")
        return memory, StateHandler(brain_memory=memory, task_handler=_TaskHandler())

    def test_dame_los_2_ratios_uses_recent_context(self) -> None:
        memory, handler = self._handler()
        memory.store_message(
            "s1",
            "assistant",
            "Si te gusta, te tiro los otros 2 ratios: 9:16 vertical y 1:1 cuadrado.",
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dame los 2 ratios", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("9:16 vertical", shortcut.text)
        self.assertIn("1:1 cuadrado", shortcut.text)
        self.assertIn("No lo trates como selección de opción 2", shortcut.text)

    def test_reply_context_resolves_ratios(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            active_object={
                "reply_context": {
                    "source": "telegram_reply",
                    "text": "Pendientes: 9:16 vertical y 1:1 cuadrado.",
                    "created_at": time.time(),
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Dame los 2", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("reply_to", shortcut.text)
        self.assertIn("9:16 vertical", shortcut.text)
        self.assertIn("1:1 cuadrado", shortcut.text)

    def test_last_options_stale_is_rejected(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            last_options=["opción vieja 1", "opción vieja 2"],
            active_object={
                "last_options_meta": {
                    "created_at": time.time() - 3600,
                    "source": "assistant_numbered_options",
                    "topic": "otro tema",
                }
            },
        )

        response = handler.maybe_resolve_stateful_followup("Vamos con la 2", session_id="s1")

        # SOUL routing policy (2026-06-10 audit A5): stale option picks fall
        # through to the brain (which re-derives options from the transcript)
        # instead of clarifying pre-brain. The stale pick must NOT execute.
        self.assertIsNone(response)

    def test_last_options_fresh_selects_option(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state(
            "s1",
            last_options=["revisar logs", "corregir bug"],
            active_object={
                "last_options_meta": {
                    "created_at": time.time(),
                    "source": "assistant_numbered_options",
                    "topic": "bug",
                }
            },
        )

        shortcut = handler.maybe_resolve_stateful_followup("Vamos con la 2", session_id="s1")

        self.assertIsInstance(shortcut, _BrainShortcut)
        assert isinstance(shortcut, _BrainShortcut)
        self.assertIn("Opción elegida: corregir bug", shortcut.text)

    def test_dale_rejects_redacted_pending_action(self) -> None:
        memory, handler = self._handler()
        token = "Aa1234567890Bb1234567890Cc1234567890"
        memory.update_session_state(
            "s1",
            mode="coding",
            pending_action=f"Objective: {token}",
        )

        response = handler.maybe_resolve_stateful_followup("Dale", session_id="s1")
        state = memory.get_session_state("s1")

        self.assertIsInstance(response, str)
        self.assertIn("valor sensible redactado", response)
        self.assertEqual(state["pending_action"], "")
        self.assertEqual(state["verification_status"], "blocked")
        self.assertEqual(state["last_checkpoint"]["reason"], "sensitive_context_redacted")

    def test_dale_rejects_redacted_task_queue_item(self) -> None:
        memory, handler = self._handler()
        token = "Aa1234567890Bb1234567890Cc1234567890"
        memory.update_session_state(
            "s1",
            mode="coding",
            task_queue=[
                {
                    "task_id": "task-sensitive",
                    "summary": f"Run objective {token}",
                    "mode": "coding",
                    "status": "pending",
                    "source": "coordinator",
                    "priority": 0,
                    "depends_on": [],
                }
            ],
        )

        response = handler.maybe_resolve_stateful_followup("Dale", session_id="s1")
        state = memory.get_session_state("s1")

        self.assertIsInstance(response, str)
        self.assertIn("valor sensible redactado", response)
        self.assertEqual(state["task_queue"][0]["status"], "blocked")
        self.assertEqual(state["verification_status"], "blocked")

    def test_assistant_turn_summary_does_not_overwrite_rolling_summary(self) -> None:
        memory, handler = self._handler()
        memory.update_session_state("s1", rolling_summary="resumen acumulado anterior")

        handler.remember_assistant_turn_state(
            "s1",
            "continua con el fix",
            "Apliqué el cambio y queda pendiente verificar.",
        )

        state = memory.get_session_state("s1")
        self.assertEqual(state["rolling_summary"], "resumen acumulado anterior")
        self.assertIn("Apliqué el cambio", state["last_turn_summary"])

    def test_direct_user_project_correction_persists_as_profile_fact(self) -> None:
        memory, handler = self._handler()

        handler.remember_assistant_turn_state(
            "s1",
            "PHD no es mi proyecto",
            "Entendido, corrijo el contexto.",
        )

        facts = memory.get_profile_facts()
        fact_by_key = {fact["key"]: fact for fact in facts}
        self.assertIn("profile.project.not_phd", fact_by_key)
        self.assertIn("PHD no es mi proyecto", fact_by_key["profile.project.not_phd"]["value"])
        self.assertEqual(fact_by_key["profile.project.not_phd"]["source_trust"], "trusted")

    def test_direct_repo_correction_persists_as_profile_fact(self) -> None:
        memory, handler = self._handler()

        handler.remember_assistant_turn_state(
            "s1",
            "El repo de mi página es Hecpac/hector-services-site",
            "Entendido.",
        )

        facts = memory.get_profile_facts()
        fact_by_key = {fact["key"]: fact for fact in facts}
        self.assertEqual(
            fact_by_key["profile.website.repo"]["value"],
            "El repo de mi página es Hecpac/hector-services-site.",
        )


if __name__ == "__main__":
    unittest.main()
