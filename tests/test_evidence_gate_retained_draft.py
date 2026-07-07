from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from claw_v2.bot import EVIDENCE_GATE_RETAINED_DRAFT_TTL_SECONDS, BotService
from claw_v2.memory import MemoryStore
from claw_v2.state_handler import (
    PENDING_ACTION_TTL_SECONDS,
    StateHandler,
    _BrainShortcut,
)

from tests.test_state_handler import _TaskHandler

_DRAFT = (
    "Arranqué el plan de cobertura: Slice 1 corre los tests de approval, "
    "Slice 2 agrega los casos de reissue, y Slice 3 cierra con el reporte "
    "de coverage. Te aviso cuando esté." + " Detalle adicional del plan." * 40
)
_USER_ASK = "Crea el plan para mejorar la cobertura de tests del módulo de aprobaciones"


def _record_retention(memory: MemoryStore, session_id: str, *, draft: str = _DRAFT) -> str | None:
    fake = SimpleNamespace(
        task_ledger=None,
        brain=SimpleNamespace(memory=memory),
        _emit_safe=lambda *args, **kwargs: None,
        _stable_text_hash=lambda text: "hash",
        _build_retained_draft_directive=lambda **kwargs: BotService._build_retained_draft_directive(
            fake, **kwargs
        ),
    )
    return BotService._record_evidence_gate_explicit_blocker(
        fake,
        session_id=session_id,
        source_text=_USER_ASK,
        blocked_content=draft,
        reason="start_claim_without_evidence",
    )


class RetainedDraftRecorderTests(unittest.TestCase):
    """F4-B2a (diagnóstico 2026-07-06): «ejecútalo» tras una retención
    re-derivaba desde cero porque el draft se perdía. El gate ahora lo
    preserva completo como pending_action ejecutable."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self._tmp.name) / "claw.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_retention_preserves_full_draft_as_pending_action(self) -> None:
        _record_retention(self.memory, "s1")

        state = self.memory.get_session_state("s1")
        pending = state.get("pending_action") or ""
        self.assertIn(_DRAFT, pending)
        self.assertIn("Ejecuta AHORA", pending)
        meta = (state.get("active_object") or {}).get("pending_action_meta") or {}
        self.assertEqual(meta.get("source"), "evidence_gate_retained_draft")
        self.assertEqual(meta.get("ttl_seconds"), EVIDENCE_GATE_RETAINED_DRAFT_TTL_SECONDS)
        self.assertGreater(
            EVIDENCE_GATE_RETAINED_DRAFT_TTL_SECONDS,
            PENDING_ACTION_TTL_SECONDS,
        )
        proposal = (state.get("active_object") or {}).get("last_actionable_proposal") or {}
        self.assertLessEqual(len(proposal.get("objective") or ""), 500)

    def test_secret_shaped_draft_is_not_preserved(self) -> None:
        # Case 1: known prefix (caught by _contains_sensitive_redaction).
        synthetic_secret = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
        _record_retention(self.memory, "s2", draft=f"Arranqué con la key {synthetic_secret}")

        state = self.memory.get_session_state("s2")
        self.assertFalse((state.get("pending_action") or "").strip())
        self.assertNotIn(
            "pending_action_meta",
            state.get("active_object") or {},
        )

    def test_embedded_high_entropy_token_in_multiword_draft_is_not_preserved(self) -> None:
        # gemini review #221 (HIGH): _is_secret_shaped_token returns False on
        # whitespace, so a multi-word draft with an embedded secret must be
        # checked per-token, not on the whole draft.
        entropy_token = "8eyt8R1Hp008liTCA98a"
        _record_retention(
            self.memory, "s2b", draft=f"Arranqué exportando el token {entropy_token} al deploy"
        )

        state = self.memory.get_session_state("s2b")
        self.assertFalse((state.get("pending_action") or "").strip())

    def test_empty_draft_is_noop(self) -> None:
        _record_retention(self.memory, "s3", draft="   ")
        state = self.memory.get_session_state("s3")
        self.assertFalse((state.get("pending_action") or "").strip())


class RetainedDraftSurvivesTurnWriteTests(unittest.TestCase):
    def test_post_turn_state_write_preserves_retained_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryStore(Path(tmpdir) / "claw.db")
            handler = StateHandler(brain_memory=memory, task_handler=_TaskHandler())
            _record_retention(memory, "s1")
            canned = (
                "No ejecuté nada aún: mi respuesta afirmaba un arranque sin evidencia. "
                "Dime «ejecútalo» y ejecuto lo que ese borrador prometía, con evidencia real."
            )

            handler.remember_assistant_turn_state("s1", _USER_ASK, canned)

            state = memory.get_session_state("s1")
            self.assertIn(_DRAFT, state.get("pending_action") or "")
            meta = (state.get("active_object") or {}).get("pending_action_meta") or {}
            self.assertEqual(meta.get("source"), "evidence_gate_retained_draft")


class RetainedDraftTtlTests(unittest.TestCase):
    def _handler_with_backdated_retention(self, age_seconds: float):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryStore(Path(tmp.name) / "claw.db")
        handler = StateHandler(brain_memory=memory, task_handler=_TaskHandler())
        _record_retention(memory, "s1")
        state = memory.get_session_state("s1")
        active_object = dict(state.get("active_object") or {})
        meta = dict(active_object.get("pending_action_meta") or {})
        meta["created_at"] = time.time() - age_seconds
        active_object["pending_action_meta"] = meta
        memory.update_session_state("s1", active_object=active_object)
        return handler, memory.get_session_state("s1")

    def test_fresh_past_native_ttl_but_within_retained_ttl(self) -> None:
        # 15 min: past the native 600s TTL — the meta ttl_seconds must govern.
        handler, state = self._handler_with_backdated_retention(900.0)
        self.assertTrue(handler._pending_action_still_fresh(state, session_id="s1"))

    def test_stale_past_retained_ttl(self) -> None:
        handler, state = self._handler_with_backdated_retention(
            EVIDENCE_GATE_RETAINED_DRAFT_TTL_SECONDS + 60.0
        )
        self.assertFalse(handler._pending_action_still_fresh(state, session_id="s1"))


class RetainedDraftExecutionTests(unittest.TestCase):
    def test_ejecutalo_seeds_brain_with_real_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryStore(Path(tmpdir) / "claw.db")
            handler = StateHandler(brain_memory=memory, task_handler=_TaskHandler())
            memory.store_message("s1", "user", _USER_ASK)
            _record_retention(memory, "s1")
            memory.store_message(
                "s1",
                "assistant",
                "No ejecuté nada aún: mi respuesta afirmaba un arranque sin evidencia. "
                "Dime «ejecútalo» y ejecuto lo que ese borrador prometía.",
            )

            shortcut = handler.maybe_resolve_stateful_followup("ejecútalo", session_id="s1")

            self.assertIsInstance(shortcut, _BrainShortcut)
            assert isinstance(shortcut, _BrainShortcut)
            self.assertIn(_DRAFT, shortcut.text)
            self.assertIn("Continúa con esta acción pendiente", shortcut.text)

    def test_conversation_that_moved_on_expires_retained_draft(self) -> None:
        # coderabbit review #221: reactivation must not happen after the
        # conversation moved on. The message-delta guard (not topic cosine —
        # the original ask lingers in history and keeps the vector high) is
        # the drift protection: once the owner sends more than the delta of
        # messages past the retention, «ejecútalo» expires and re-derives.
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryStore(Path(tmpdir) / "claw.db")
            handler = StateHandler(brain_memory=memory, task_handler=_TaskHandler())
            memory.store_message("s1", "user", _USER_ASK)
            _record_retention(memory, "s1")
            for _ in range(5):
                memory.store_message("s1", "assistant", "otro tema intermedio")
                memory.store_message("s1", "user", "hablemos de otra cosa distinta")

            result = handler.maybe_resolve_stateful_followup("dale", session_id="s1")

            self.assertNotIsInstance(result, _BrainShortcut)


if __name__ == "__main__":
    unittest.main()
