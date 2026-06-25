from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from claw_v2.bot import BotService
from claw_v2.delegation_intents import classify_authenticated_browse_intent
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime

_STARTED_MARKER = "Voy con eso.\n\nTarea autónoma iniciada: `t`\nModo: chat"


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


# ---- JobService atomic creator-election primitive --------------------------
class JobServiceReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jobs = JobService(Path(self._tmp.name) / "claw.db")

    def test_reserve_elects_exactly_one_creator(self) -> None:
        rec1, created1 = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
        rec2, created2 = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(rec1.job_id, rec2.job_id)  # duplicate gets the winner's row

    def test_reservation_stays_active_as_dedup_token(self) -> None:
        # The reservation kind is claimed by no runner, so it stays an active
        # (queued) durable dedup token — every later reserve dedups against it,
        # independent of any task's lifecycle (the "redelivery after completion"
        # guarantee). It only releases on an explicit delete.
        rec, created = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
        self.assertTrue(created)
        self.assertEqual(self.jobs.get_by_resume_key("k").status, "queued")
        for _ in range(3):
            _, dup = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
            self.assertFalse(dup)

    def test_delete_releases_key_for_recreate(self) -> None:
        rec, created = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
        self.assertTrue(created)
        self.jobs.delete(rec.job_id)
        self.assertIsNone(self.jobs.get_by_resume_key("k"))
        _, created2 = self.jobs.reserve(resume_key="k", kind="f4b.delegation_reservation")
        self.assertTrue(created2)


# ---- gate helpers ----------------------------------------------------------
class _Observe:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def emit(self, event: str, **kwargs: Any) -> None:
        with self._lock:
            self.events.append((event, dict(kwargs)))

    def types(self) -> list[str]:
        return [event for event, _ in self.events]


class _FakeTaskHandler:
    """Stand-in for the delegation boundary. The gate elects the creator via the
    real JobService.reserve, then calls start_autonomous_task on the winner only;
    this fake returns the positive durable-start marker (or a non-start result /
    raises / has no coordinator) so the gate's "did a task start" check is real."""

    def __init__(
        self,
        job_service: JobService,
        *,
        result: str = _STARTED_MARKER,
        raise_exc: bool = False,
        coordinator: bool = True,
    ) -> None:
        self.job_service = job_service
        self.coordinator = object() if coordinator else None
        self._result = result
        self._raise = raise_exc
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def start_autonomous_task(
        self,
        session_id: str,
        objective: str,
        *,
        source_text: str | None = None,
        task_kind: str | None = None,
        delegation_metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        with self._lock:
            self.calls.append(objective)
        if self._raise:
            raise RuntimeError("simulated start failure")
        return self._result


def _ctx(message_id: int | None = 111) -> dict[str, Any]:
    inbound: dict[str, Any] = {"channel": "telegram"}
    if message_id is not None:
        inbound["message_id"] = message_id
    return {"inbound": inbound}


def _bot(job_service: JobService, *, flag: bool, task_handler: Any, observe: Any) -> BotService:
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

    def _gate(self, text: str, *, flag: bool = True, ctx: Any = "__default__", th: Any = None):
        bot = _bot(self.jobs, flag=flag, task_handler=th or self.th, observe=self.observe)
        return bot._maybe_handle_f4_deterministic_delegation(
            text, session_id="tg-1", context_metadata=_ctx() if ctx == "__default__" else ctx
        )

    def _reservation(self, message_id: int = 111):
        return self.jobs.get_by_resume_key(f"f4b-delegation:tg-1:{message_id}")

    def _assert_no_unsupported_promise(self, resp: str) -> None:
        low = resp.lower()
        for forbidden in (
            "reintento",
            "reintentar",
            "te aviso cuando",
            "cuando se resuelva",
            "quedó registrado",
        ):
            self.assertNotIn(forbidden, low)

    # 1. incident: one creator, one reservation, one start, truthful ack
    def test_incident_elects_one_creator_with_truthful_ack(self) -> None:
        resp = self._gate("Haz un repaso por X")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("repaso de tu feed de X", resp)
        self.assertNotIn("ToolSearch", resp)
        self.assertEqual(len(self.th.calls), 1)
        res = self._reservation()
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.kind, "f4b.delegation_reservation")
        self.assertIn("f4_deterministic_delegation_enqueued", self.observe.types())

    # 2. flag OFF: fall through, no reservation, no start
    def test_flag_off_falls_through(self) -> None:
        self.assertIsNone(self._gate("Haz un repaso por X", flag=False))
        self.assertEqual(self.th.calls, [])
        self.assertIsNone(self._reservation())

    # 3 + 4. gate independent of CLAW_DISABLE_TASK_INTENT_ROUTER; captures first
    def test_gate_independent_of_broad_router_flag(self) -> None:
        for val in ("0", "1"):
            with self.subTest(broad_router=val), tempfile.TemporaryDirectory() as tmp:
                jobs = JobService(Path(tmp) / "claw.db")
                th = _FakeTaskHandler(jobs)
                bot = _bot(jobs, flag=True, task_handler=th, observe=_Observe())
                with patch.dict(os.environ, {"CLAW_DISABLE_TASK_INTENT_ROUTER": val}):
                    resp = bot._maybe_handle_f4_deterministic_delegation(
                        "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx()
                    )
                self.assertIsNotNone(resp)
                self.assertEqual(len(th.calls), 1)

    # 5. duplicate delivery (same message_id) -> one creator
    def test_duplicate_delivery_one_creator(self) -> None:
        r1 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        r2 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        assert r2 is not None
        self.assertEqual(len(self.th.calls), 1)  # 2nd deduped, no start
        self.assertIn("ya está en marcha", r2)

    # 6. legitimate repeat (new message_id) -> new task
    def test_legitimate_repeat_new_creator(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        self._gate("Haz un repaso por X", ctx=_ctx(222))
        self.assertEqual(len(self.th.calls), 2)
        self.assertIsNotNone(self._reservation(111))
        self.assertIsNotNone(self._reservation(222))

    # 7. non-matching conversation not captured
    def test_non_matching_not_captured(self) -> None:
        for text in ("¿Qué es X?", "Escribe un post para X", "hola"):
            with self.subTest(text=text):
                self.assertIsNone(self._gate(text))
        self.assertEqual(self.th.calls, [])

    def test_no_delivery_id_falls_through(self) -> None:
        resp = self._gate("Haz un repaso por X", ctx=_ctx(message_id=None))
        self.assertIsNone(resp)
        self.assertEqual(self.th.calls, [])
        self.assertIn("f4_deterministic_delegation_skipped_no_delivery_id", self.observe.types())

    # 8 / #4. start failure -> reservation released, truthful, no orphan
    def test_start_returning_no_task_releases_reservation(self) -> None:
        th = _FakeTaskHandler(self.jobs, result="coordinator unavailable")
        resp = self._gate("Haz un repaso por X", th=th)
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("no quedó nada encolado", resp)
        self._assert_no_unsupported_promise(resp)
        self.assertIsNone(self._reservation())  # released -> no claimable orphan, retry possible
        self.assertIn("f4_deterministic_delegation_failed", self.observe.types())

    def test_start_exception_releases_reservation(self) -> None:
        th = _FakeTaskHandler(self.jobs, raise_exc=True)
        resp = self._gate("Haz un repaso por X", th=th)
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("no quedó nada encolado", resp)
        self._assert_no_unsupported_promise(resp)
        self.assertIsNone(self._reservation())

    def test_no_coordinator_releases_reservation(self) -> None:
        th = _FakeTaskHandler(self.jobs, coordinator=False)
        resp = self._gate("Haz un repaso por X", th=th)
        assert resp is not None
        self.assertIn("no quedó nada encolado", resp)
        self.assertEqual(th.calls, [])  # never called start without a coordinator
        self.assertIsNone(self._reservation())

    # Concurrent duplicate delivery: prove ONE execution + ONE logical task.
    def test_concurrent_duplicate_elects_one_creator(self) -> None:
        th = _FakeTaskHandler(self.jobs)
        bot = _bot(self.jobs, flag=True, task_handler=th, observe=self.observe)
        barrier = threading.Barrier(2)
        results: dict[int, Any] = {}

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = bot._maybe_handle_f4_deterministic_delegation(
                "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(th.calls), 1)  # exactly one creator started a task
        self.assertIsNotNone(self._reservation())  # one reservation row
        self.assertEqual(len(results), 2)
        for r in results.values():
            self.assertIsNotNone(r)
            self.assertIn("repaso de tu feed de X", r)
        dedup = sum(1 for r in results.values() if "ya está en marcha" in r)
        self.assertEqual(dedup, 1)  # exactly one duplicate got the dedup ack

    def test_observe_none_does_not_crash(self) -> None:
        bot = _bot(self.jobs, flag=True, task_handler=self.th, observe=None)
        resp = bot._maybe_handle_f4_deterministic_delegation(
            "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
        )
        self.assertIsNotNone(resp)
        self.assertEqual(len(self.th.calls), 1)
        self.assertIsNotNone(self._reservation())


class RealChainIntegrationTests(unittest.TestCase):
    """End-to-end through the REAL gate → JobService.reserve → start_autonomous_task
    → _reject_non_actionable_objective → coordinator job enqueue (the gate unit
    tests stub the boundary; these exercise the real chain incl. the prod path)."""

    def _env(self, root: Path) -> dict[str, str]:
        return {
            "DB_PATH": str(root / "data" / "claw.db"),
            "WORKSPACE_ROOT": str(root / "workspace"),
            "AGENT_STATE_ROOT": str(root / "agents"),
            "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
            "APPROVALS_ROOT": str(root / "approvals"),
            "TELEGRAM_ALLOWED_USER_ID": "123",
            "CLAW_F4_DETERMINISTIC_DELEGATION": "1",
        }

    def _assert_durable_task(self, runtime: Any, message_id: int) -> None:
        reservation = runtime.job_service.get_by_resume_key(f"f4b-delegation:tg-1:{message_id}")
        self.assertIsNotNone(reservation)
        assert reservation is not None
        self.assertEqual(reservation.kind, "f4b.delegation_reservation")
        coordinator_jobs = runtime.job_service.list(kinds=["coordinator.autonomous_task"], limit=50)
        self.assertGreaterEqual(len(coordinator_jobs), 1)

    def test_real_bot_handle_text_creates_durable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, self._env(Path(tmpdir)), clear=False):
                runtime = build_runtime(anthropic_executor=lambda r: self.fail("no model call"))
                runtime.bot._task_handler.coordinator = MagicMock()
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="tg-1",
                    text="Haz un repaso por X",
                    context_metadata={"inbound": {"channel": "telegram", "message_id": 777}},
                )
                assert reply is not None
                self.assertIn("tarea de fondo", reply)
                self._assert_durable_task(runtime, 777)

    def test_agent_runtime_path_forwards_inbound_id_to_gate(self) -> None:
        # The PROD path the P1 regression silently broke: AgentRuntime relays
        # metadata -> bot_service.handle_text context_metadata -> gate.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, self._env(Path(tmpdir)), clear=False):
                runtime = build_runtime(anthropic_executor=lambda r: self.fail("no model call"))
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
                self._assert_durable_task(runtime, 888)


if __name__ == "__main__":
    unittest.main()
