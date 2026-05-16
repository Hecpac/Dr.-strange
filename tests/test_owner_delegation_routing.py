"""PR 0C: owner-delegation kernel.

Three layers under test:

1. The classifier (`detect_owner_delegation`) — pure regex over a
   normalized string. Must catch every audit phrase, must NOT
   false-positive on casual chat.

2. The resolver (`StateHandler.resolve_delegated_objective`) — uses
   `brain_memory.get_session_state` and the recent message log to derive
   a concrete objective. For decision delegations with safe options it
   picks deterministically; with risky options it returns a clarifying
   question. Never returns "elige tú" / "decide tú".

3. The safety classifier (`is_destructive_or_external_objective`) —
   gates deploy / merge / publish / send / payment / secret / delete /
   production from auto-execution.

The router integration test invokes
`StateHandler.resolve_delegated_objective` with a mocked memory so we
don't need to construct a full `BotService`.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from claw_v2.bot_helpers import (
    OwnerDelegationIntent,
    detect_owner_delegation,
    is_destructive_or_external_objective,
)
from claw_v2.memory import MemoryStore
from claw_v2.state_handler import (
    DelegatedObjectiveResolution,
    StateHandler,
)


class _StubTaskHandler:
    """Minimal TaskHandler-shaped object for StateHandler construction."""

    def derive_task_dependencies(self, *_args, **_kwargs):
        return []

    def upsert_task_queue_entry(self, queue, **_kwargs):
        return queue

    def mark_first_task_queue_entry(self, queue, **_kwargs):
        return queue

    def mark_task_queue_in_progress(self, queue, **_kwargs):
        return queue


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs)))


def _make_handler() -> tuple[MemoryStore, StateHandler, _RecordingObserve]:
    tmp = tempfile.TemporaryDirectory()
    # The TemporaryDirectory must outlive the test; we attach it to the
    # returned handler so the GC keeps it alive.
    memory = MemoryStore(Path(tmp.name) / "claw.db")
    observe = _RecordingObserve()
    handler = StateHandler(
        brain_memory=memory, task_handler=_StubTaskHandler(), observe=observe
    )
    handler.__test_tmpdir__ = tmp  # type: ignore[attr-defined]
    return memory, handler, observe


# ---------------------------------------------------------------------------
# 1. Classifier — must match audit phrases, must avoid casual chat.
# ---------------------------------------------------------------------------


class OwnerDelegationClassifierTests(unittest.TestCase):
    def test_execution_delegation_spanish(self) -> None:
        cases = [
            "correlo tu mismo",
            "córrelo tú mismo",
            "corre los tu mismo",
            "correlos tu mismo",
            "puedes correrlo tu",
            "puedes correrlos tu",
            "hazlo tu",
            "hazlo tú",
            "ejecutalo tu",
            "ejecútalo tú",
            "lo haces tu",
            "lo corres tu mismo",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                intent = detect_owner_delegation(phrase)
                self.assertIsNotNone(intent, f"expected match for: {phrase!r}")
                assert intent is not None
                self.assertEqual(intent.kind, "execution")
                self.assertTrue(intent.is_execution_delegation)
                self.assertGreaterEqual(intent.confidence, 0.9)

    def test_execution_delegation_english(self) -> None:
        for phrase in (
            "run it yourself",
            "you run it",
            "do it yourself",
            "do it for me",
            "you handle it",
        ):
            with self.subTest(phrase=phrase):
                intent = detect_owner_delegation(phrase)
                self.assertIsNotNone(intent)
                assert intent is not None
                self.assertTrue(intent.is_execution_delegation)

    def test_decision_delegation(self) -> None:
        for phrase in (
            "decide tu",
            "decide tú",
            "tú decides",
            "tu eliges",
            "escoge tu",
            "you decide",
            "you choose",
            "you pick",
        ):
            with self.subTest(phrase=phrase):
                intent = detect_owner_delegation(phrase)
                self.assertIsNotNone(intent)
                assert intent is not None
                self.assertEqual(intent.kind, "decision")
                self.assertTrue(intent.is_decision_delegation)

    def test_no_manual_work_delegation(self) -> None:
        for phrase in (
            "te toca a ti",
            "ya no tengo que teclear nada",
            "no tengo que teclear nada",
            "no me pidas que lo haga",
            "no me preguntes",
            "no me devuelvas el trabajo",
            "no me hagas teclear",
            "encárgate tú",
            "encargate tu",
            "gestiona tu",
            "gestiónalo tú",
            "take ownership",
            "handle it",
            "don't ask me to do it",
            "stop asking me to run commands",
        ):
            with self.subTest(phrase=phrase):
                intent = detect_owner_delegation(phrase)
                self.assertIsNotNone(intent, f"expected match for: {phrase!r}")
                assert intent is not None
                self.assertTrue(
                    intent.is_no_manual_work_delegation
                    or intent.is_execution_delegation,  # "handle it" overlaps
                    f"expected no-manual-work or execution kind for {phrase!r}",
                )

    def test_inline_hint_extraction(self) -> None:
        intent = detect_owner_delegation("encárgate tú de actualizar el deck")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.explicit_action_hint, "actualizar el deck")
        self.assertFalse(intent.requires_resolution)

    def test_casual_chat_does_not_match(self) -> None:
        for benign in (
            "hola",
            "ok",
            "gracias",
            "perfecto",
            "dale",
            "que onda",
            "buen dia",
            "good morning",
            "how are you",
            "envia el informe diario",  # imperative but not delegation
            "abre la pagina de Linear",
            "lee el reporte",  # imperative read
        ):
            with self.subTest(benign=benign):
                self.assertIsNone(
                    detect_owner_delegation(benign),
                    f"benign input false-positived: {benign!r}",
                )

    def test_empty_text_returns_none(self) -> None:
        self.assertIsNone(detect_owner_delegation(""))
        self.assertIsNone(detect_owner_delegation("   "))


# ---------------------------------------------------------------------------
# 2. Resolver — uses session_state, decides safe vs risky.
# ---------------------------------------------------------------------------


class OwnerDelegationResolverTests(unittest.TestCase):
    def _intent(self, kind: str = "execution", hint: str | None = None) -> OwnerDelegationIntent:
        return OwnerDelegationIntent(
            kind=kind,
            confidence=0.95,
            normalized_text="(test)",
            requires_resolution=hint is None,
            is_execution_delegation=(kind == "execution"),
            is_decision_delegation=(kind == "decision"),
            is_no_manual_work_delegation=(kind == "no_manual_work"),
            explicit_action_hint=hint,
        )

    def test_inline_hint_wins(self) -> None:
        _, handler, _ = _make_handler()
        intent = self._intent(kind="no_manual_work", hint="actualizar el deck")
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="encargate tu de actualizar el deck", intent=intent
        )
        self.assertEqual(resolution.objective, "actualizar el deck")
        self.assertEqual(resolution.resolution_source, "user_text_inline_hint")
        self.assertFalse(resolution.is_risky)

    def test_hazlo_tu_resolves_from_pending_action(self) -> None:
        memory, handler, _ = _make_handler()
        memory.update_session_state(
            "tg-x", pending_action="generar el resumen semanal del fitness tracker"
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="hazlo tu", intent=self._intent()
        )
        self.assertEqual(
            resolution.objective, "generar el resumen semanal del fitness tracker"
        )
        self.assertEqual(resolution.resolution_source, "session_state.pending_action")
        self.assertFalse(resolution.is_risky)

    def test_correlo_tu_resolves_from_recent_assistant_proposal(self) -> None:
        memory, handler, _ = _make_handler()
        memory.store_message(
            "tg-x", "user", "puedo correr el script de backup nocturno?"
        )
        memory.store_message(
            "tg-x",
            "assistant",
            "Encontre el script `nightly_backup.sh`. ¿Lo corro?",
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="correlo tu mismo", intent=self._intent()
        )
        self.assertIsNotNone(resolution.objective)
        assert resolution.objective is not None
        self.assertIn("nightly_backup.sh", resolution.objective)
        self.assertEqual(resolution.resolution_source, "recent_assistant_proposal")

    def test_decide_tu_picks_first_safe_option_deterministically(self) -> None:
        memory, handler, _ = _make_handler()
        memory.update_session_state(
            "tg-x",
            last_options=[
                "generar el reporte mensual en pdf",
                "exportar las metricas a un csv local",
            ],
            active_object={"last_options_meta": {"created_at": time.time()}},
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="decide tu", intent=self._intent(kind="decision")
        )
        self.assertEqual(resolution.objective, "generar el reporte mensual en pdf")
        self.assertEqual(resolution.selected_option_index, 0)
        self.assertEqual(resolution.resolution_source, "last_options_deterministic")
        self.assertIsNone(resolution.clarifying_question)
        self.assertFalse(resolution.is_risky)

    def test_decide_tu_with_destructive_option_asks_one_question(self) -> None:
        memory, handler, _ = _make_handler()
        memory.update_session_state(
            "tg-x",
            last_options=[
                "generar el reporte local",
                "deploy a production y publicar el release",
            ],
            active_object={"last_options_meta": {"created_at": time.time()}},
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="decide tu", intent=self._intent(kind="decision")
        )
        self.assertIsNone(resolution.objective)
        self.assertTrue(resolution.is_risky)
        self.assertIsNotNone(resolution.clarifying_question)
        assert resolution.clarifying_question is not None
        # Single question — not multiple back-and-forth prompts.
        self.assertEqual(resolution.clarifying_question.count("?"), 0)
        self.assertNotIn("elige tu", resolution.clarifying_question.lower())
        self.assertNotIn("decide tu", resolution.clarifying_question.lower())

    def test_unresolved_hazlo_tu_returns_one_clarifying_question(self) -> None:
        _memory, handler, _ = _make_handler()
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="hazlo tu", intent=self._intent()
        )
        self.assertIsNone(resolution.objective)
        self.assertIsNotNone(resolution.clarifying_question)
        assert resolution.clarifying_question is not None
        # Must not bounce the choice back to the user.
        self.assertNotIn("decide tu", resolution.clarifying_question.lower())
        self.assertNotIn("elige tu", resolution.clarifying_question.lower())

    def test_resolver_marks_risky_when_pending_action_is_destructive(self) -> None:
        memory, handler, _ = _make_handler()
        memory.update_session_state(
            "tg-x", pending_action="deploy a production y publicar el release"
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="hazlo tu", intent=self._intent()
        )
        self.assertEqual(resolution.objective, "deploy a production y publicar el release")
        self.assertTrue(resolution.is_risky)


# ---------------------------------------------------------------------------
# 3. Safety classifier
# ---------------------------------------------------------------------------


class OwnerDelegationSafetyTests(unittest.TestCase):
    def test_destructive_or_external_actions_are_flagged(self) -> None:
        for phrase in (
            "deploy a prod",
            "deploy the new build",
            "merge a main",
            "publica el release",
            "publish the post on linkedin",
            "envía el email a inversionistas",
            "send the email to investors",
            "paga la factura",
            "charge the customer",
            "borra la cuenta del usuario",
            "drop the database",
            "rm -rf data",
            "rotar el token de la api",
            "rotate the api key",
            "modificar production",
            "deploy en producción",
            "sudo apt-get install",
            "git push --force origin main",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    is_destructive_or_external_objective(phrase),
                    f"expected risky for: {phrase!r}",
                )

    def test_safe_objectives_are_not_flagged(self) -> None:
        for phrase in (
            "generar el resumen semanal",
            "leer el archivo de configuracion",
            "buscar referencias en wiki",
            "calcular el promedio del fitness tracker",
            "abrir la pagina de Linear",
            "summarize the last meeting notes",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(
                    is_destructive_or_external_objective(phrase),
                    f"benign input false-positived: {phrase!r}",
                )


# ---------------------------------------------------------------------------
# 4. Cross-cutting contract: must work without autonomy_mode=autonomous
#    and without depending on disabled task-intent/semantic-prebrain flags.
# ---------------------------------------------------------------------------


class OwnerDelegationContractTests(unittest.TestCase):
    def test_classifier_is_pure_and_flag_independent(self) -> None:
        # The classifier is a free function and does not read env flags at
        # all — so it must succeed regardless of CLAW_DISABLE_*.
        import os

        os.environ["CLAW_DISABLE_TASK_INTENT_ROUTER"] = "1"
        os.environ["CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES"] = "0"
        try:
            intent = detect_owner_delegation("córrelo tú mismo")
            self.assertIsNotNone(intent)
        finally:
            os.environ.pop("CLAW_DISABLE_TASK_INTENT_ROUTER", None)
            os.environ.pop("CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES", None)

    def test_resolver_works_with_assisted_session(self) -> None:
        memory, handler, _ = _make_handler()
        # Session is "assisted" by default — never set autonomy_mode.
        memory.update_session_state(
            "tg-x", pending_action="generar el resumen semanal"
        )
        intent = OwnerDelegationIntent(
            kind="execution",
            confidence=0.95,
            normalized_text="hazlo tu",
            requires_resolution=True,
            is_execution_delegation=True,
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x", text="hazlo tu", intent=intent
        )
        self.assertEqual(resolution.objective, "generar el resumen semanal")

    def test_concatenated_message_resolves_via_recent_proposal(self) -> None:
        memory, handler, _ = _make_handler()
        memory.store_message(
            "tg-x",
            "assistant",
            "Tengo listo el resumen del fitness tracker. ¿Lo arranco?",
        )
        intent = OwnerDelegationIntent(
            kind="no_manual_work",
            confidence=0.9,
            normalized_text="(test)",
            requires_resolution=True,
            is_no_manual_work_delegation=True,
        )
        resolution = handler.resolve_delegated_objective(
            session_id="tg-x",
            text="Ya no tengo que teclear nada, ahora te toca a ti hacer el trabajo",
            intent=intent,
        )
        self.assertIsNotNone(resolution.objective)
        self.assertEqual(resolution.resolution_source, "recent_assistant_proposal")
        self.assertFalse(resolution.is_risky)


# ---------------------------------------------------------------------------
# 5. Resolution metadata shape (for bot-router consumption).
# ---------------------------------------------------------------------------


class DelegatedObjectiveResolutionShapeTests(unittest.TestCase):
    def test_default_resolution_has_no_selected_option(self) -> None:
        resolution = DelegatedObjectiveResolution(
            objective="x", resolution_source="src", mode="chat", is_risky=False
        )
        self.assertIsNone(resolution.selected_option_index)
        self.assertIsNone(resolution.clarifying_question)
        self.assertIsNone(resolution.pending_options)


if __name__ == "__main__":
    unittest.main()
