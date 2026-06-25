from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from claw_v2.bot import BotService
from claw_v2.delegation_intents import classify_authenticated_browse_intent
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime


class ClassifierTests(unittest.TestCase):
    def _match(self, text: str) -> bool:
        return classify_authenticated_browse_intent(text) is not None

    # --- must match (high-confidence authenticated feed review) ---
    def test_incident_phrase_matches(self) -> None:
        intent = classify_authenticated_browse_intent("Haz un repaso por X")
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.kind, "authenticated_browse")
        self.assertTrue(intent.objective.strip())

    def test_feed_review_variants_match(self) -> None:
        for text in (
            "haz un barrido de X",
            "revisa mi feed de X",
            "dale una vuelta a mi timeline de twitter",
            "chequea mi TL de X",
            "repasa mi feed de X",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._match(text))

    # P2: bare feed/timeline without X must NOT match (prefer false negatives).
    def test_bare_feed_without_x_not_matched(self) -> None:
        for text in ("repasa mi feed", "revisa mi timeline", "dale una vuelta a mi tl"):
            with self.subTest(text=text):
                self.assertFalse(self._match(text))

    # P2: an explicit non-X platform must NOT enqueue an X review.
    def test_other_platform_not_matched(self) -> None:
        for text in (
            "revisa mi feed de Instagram",
            "revisa mi timeline de Facebook",
            "dale una vuelta a mi feed de tiktok",
            "chequea mi linkedin",
            "haz un repaso de mi feed de threads",
        ):
            with self.subTest(text=text):
                self.assertFalse(self._match(text))

    # --- must NOT match (explicit non-captures from the brief) ---
    def test_brief_non_matches(self) -> None:
        for text in (
            "¿Qué es X?",
            "Escribe un post para X",
            "Qué opinas de Twitter",
            "Resume este texto sobre redes sociales",
        ):
            with self.subTest(text=text):
                self.assertFalse(self._match(text))

    # --- adversarial X-as-placeholder (must NOT match) ---
    def test_x_placeholder_non_matches(self) -> None:
        for text in (
            "revisá el punto X de la lista",
            "revisa por X razón los datos",
            "haz un repaso por X o por Y de los pendientes",
            "cuál es el valor de X",
            "revisa la variable X",
        ):
            with self.subTest(text=text):
                self.assertFalse(self._match(text))

    def test_empty_and_unrelated_non_matches(self) -> None:
        for text in ("", "   ", "hola", "qué hora es", "haz un resumen del día"):
            with self.subTest(text=text):
                self.assertFalse(self._match(text))


class _Observe:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, dict(kwargs)))

    def types(self) -> list[str]:
        return [event for event, _ in self.events]


class _FakeTaskHandler:
    """Faithful stand-in for the delegation boundary: start_autonomous_task with
    an idempotency_key enqueues a coordinator.autonomous_task whose resume_key IS
    that key (mirrors task_handler._enqueue_autonomous_job), so dedup is real."""

    def __init__(
        self, job_service: JobService, *, succeed: bool = True, enqueue: bool = True
    ) -> None:
        self.job_service = job_service
        self.coordinator = object()
        self._succeed = succeed
        self._enqueue = enqueue
        self.calls: list[str | None] = []

    def start_autonomous_task(
        self,
        session_id: str,
        objective: str,
        *,
        source_text: str | None = None,
        task_kind: str | None = None,
        delegation_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        **_: Any,
    ) -> str:
        self.calls.append(idempotency_key)
        if not self._succeed:
            raise RuntimeError("simulated enqueue failure")
        if self._enqueue:
            self.job_service.enqueue(
                kind="coordinator.autonomous_task",
                payload={"session_id": session_id, "objective": objective},
                resume_key=idempotency_key,
            )
        return "coordinator unavailable" if not self._enqueue else "ack"


def _ctx(message_id: int | None = 111) -> dict[str, Any]:
    inbound: dict[str, Any] = {"channel": "telegram"}
    if message_id is not None:
        inbound["message_id"] = message_id
    return {"inbound": inbound}


def _bot(
    job_service: JobService, *, flag: bool, task_handler: Any, observe: _Observe
) -> BotService:
    bot = BotService.__new__(BotService)
    bot.config = SimpleNamespace(f4_deterministic_delegation=flag)
    bot.observe = observe
    bot._task_handler = task_handler
    return bot


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jobs = JobService(Path(self._tmp.name) / "claw.db")
        self.observe = _Observe()
        self.th = _FakeTaskHandler(self.jobs)

    def _gate(
        self, text: str, *, flag: bool = True, ctx: dict | None = "__default__", th: Any = None
    ):
        bot = _bot(self.jobs, flag=flag, task_handler=th or self.th, observe=self.observe)
        return bot._maybe_handle_f4_deterministic_delegation(
            text,
            session_id="tg-1",
            context_metadata=_ctx() if ctx == "__default__" else ctx,
        )

    # 1. exact incident: matches, one durable job, truthful ack, no model call
    def test_incident_enqueues_one_durable_job_with_truthful_ack(self) -> None:
        resp = self._gate("Haz un repaso por X")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("tarea de fondo", resp)
        self.assertNotIn("ToolSearch", resp)
        self.assertNotIn("tool_polic", resp)
        self.assertEqual(len(self.th.calls), 1)
        self.assertEqual(self.th.calls[0], "f4b-delegation:tg-1:111")
        self.assertIsNotNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:111"))
        self.assertIn("f4_deterministic_delegation_enqueued", self.observe.types())

    # 2. flag OFF: fall through, no enqueue
    def test_flag_off_falls_through_no_enqueue(self) -> None:
        resp = self._gate("Haz un repaso por X", flag=False)
        self.assertIsNone(resp)
        self.assertEqual(self.th.calls, [])

    # 3 + 4. gate is independent of CLAW_DISABLE_TASK_INTENT_ROUTER, and captures
    # (returns non-None) so the broad router never double-handles.
    def test_gate_independent_of_broad_router_flag(self) -> None:
        for val in ("0", "1"):
            with self.subTest(broad_router=val):
                with tempfile.TemporaryDirectory() as tmp:
                    jobs = JobService(Path(tmp) / "claw.db")
                    th = _FakeTaskHandler(jobs)
                    observe = _Observe()
                    bot = _bot(jobs, flag=True, task_handler=th, observe=observe)
                    with patch.dict(os.environ, {"CLAW_DISABLE_TASK_INTENT_ROUTER": val}):
                        resp = bot._maybe_handle_f4_deterministic_delegation(
                            "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx()
                        )
                    self.assertIsNotNone(resp)  # captures → body returns before task_intent
                    self.assertEqual(len(th.calls), 1)

    # 5. duplicate delivery (same message_id) -> one job
    def test_duplicate_delivery_creates_one_job(self) -> None:
        r1 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        r2 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(len(self.th.calls), 1)  # 2nd deduped BEFORE start_autonomous_task
        self.assertIsNotNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:111"))
        deduped = [p for e, p in self.observe.events if e == "f4_deterministic_delegation_matched"]
        self.assertTrue(any(p.get("payload", {}).get("deduped") for p in deduped))

    # 6. legitimate repeat (new message_id) -> new job
    def test_legitimate_repeat_creates_new_job(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        self._gate("Haz un repaso por X", ctx=_ctx(222))
        self.assertEqual(len(self.th.calls), 2)
        self.assertIsNotNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:111"))
        self.assertIsNotNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:222"))

    # 7. non-matching conversation is not captured
    def test_non_matching_not_captured(self) -> None:
        for text in ("¿Qué es X?", "Escribe un post para X", "Qué opinas de Twitter", "hola"):
            with self.subTest(text=text):
                self.assertIsNone(self._gate(text))
        self.assertEqual(self.th.calls, [])

    # no stable delivery id -> never enqueue (fall through), safe event
    def test_no_delivery_id_falls_through(self) -> None:
        resp = self._gate("Haz un repaso por X", ctx=_ctx(message_id=None))
        self.assertIsNone(resp)
        self.assertEqual(self.th.calls, [])
        self.assertIn("f4_deterministic_delegation_skipped_no_delivery_id", self.observe.types())

    # 8. enqueue failure -> no job, safe failure event, no invented detail
    def test_enqueue_exception_is_truthful_with_no_job(self) -> None:
        th = _FakeTaskHandler(self.jobs, succeed=False)
        resp = self._gate("Haz un repaso por X", th=th)
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("no quedó nada encolado", resp)
        self.assertNotIn("ToolSearch", resp)
        self.assertNotIn("tool_polic", resp)
        self._assert_no_unsupported_promise(resp)
        self.assertIsNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:111"))
        self.assertIn("f4_deterministic_delegation_failed", self.observe.types())

    # truthful verify: start_autonomous_task returns but no durable job -> failure
    def test_enqueue_returns_without_job_is_truthful_failure(self) -> None:
        th = _FakeTaskHandler(self.jobs, enqueue=False)
        resp = self._gate("Haz un repaso por X", th=th)
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("no quedó nada encolado", resp)
        self.assertNotIn("ToolSearch", resp)
        self.assertNotIn("tool_polic", resp)
        self._assert_no_unsupported_promise(resp)
        self.assertIsNone(self.jobs.get_by_resume_key("f4b-delegation:tg-1:111"))
        self.assertIn("f4_deterministic_delegation_failed", self.observe.types())

    def _assert_no_unsupported_promise(self, resp: str) -> None:
        # No durable retry/scheduler exists on the failure path, so the message
        # must not promise a retry, a later notification, or future execution.
        low = resp.lower()
        for forbidden in ("reintento", "reintentar", "te aviso cuando", "cuando se resuelva"):
            self.assertNotIn(forbidden, low)


class RealChainIntegrationTests(unittest.TestCase):
    """End-to-end through the REAL classifier → gate → start_autonomous_task →
    _reject_non_actionable_objective → JobService.enqueue (the gate unit tests use
    a fake task_handler, which bypasses the real non-actionable guard)."""

    def test_real_handle_text_enqueues_durable_job_without_model_call(self) -> None:
        def _no_model(request: Any) -> Any:
            raise AssertionError("brain executor must not be called for deterministic delegation")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "CLAW_F4_DETERMINISTIC_DELEGATION": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=_no_model)
                # Keep the background coordinator thread cheap; the durable job is
                # enqueued synchronously inside start_autonomous_task before it.
                runtime.bot._task_handler.coordinator = MagicMock()
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="tg-1",
                    text="Haz un repaso por X",
                    context_metadata={"inbound": {"channel": "telegram", "message_id": 777}},
                )
                self.assertIsNotNone(reply)
                assert reply is not None
                self.assertIn("tarea de fondo", reply)
                self.assertNotIn("ToolSearch", reply)
                job = runtime.job_service.get_by_resume_key("f4b-delegation:tg-1:777")
                self.assertIsNotNone(job)
                assert job is not None
                self.assertEqual(job.kind, "coordinator.autonomous_task")

    def test_agent_runtime_path_forwards_inbound_id_to_gate(self) -> None:
        # The PROD path: TelegramTransport runs with an agent_runtime, whose
        # handle_text relays metadata -> bot_service.handle_text context_metadata.
        # This is the path the P1 regression (stripping inbound) silently broke.
        def _no_model(request: Any) -> Any:
            raise AssertionError("brain executor must not be called for deterministic delegation")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "CLAW_F4_DETERMINISTIC_DELEGATION": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=_no_model)
                runtime.bot._task_handler.coordinator = MagicMock()
                resp = runtime.agent_runtime.handle_text(
                    channel="telegram",
                    external_user_id="123",
                    external_session_id="1",
                    session_id="tg-1",
                    text="Haz un repaso por X",
                    metadata={"inbound": {"channel": "telegram", "message_id": 888}},
                )
                self.assertIn("tarea de fondo", resp.text)
                job = runtime.job_service.get_by_resume_key("f4b-delegation:tg-1:888")
                self.assertIsNotNone(job)
                assert job is not None
                self.assertEqual(job.kind, "coordinator.autonomous_task")


if __name__ == "__main__":
    unittest.main()
