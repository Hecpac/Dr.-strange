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
from claw_v2.f4_delegation import F4_DELEGATION_JOB_KIND, f4b_delivery_task_id
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime
from claw_v2.task_ledger import TERMINAL_STATUSES, TaskLedger


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
        rec1, created1 = self.jobs.reserve(resume_key="k", kind="test.reserve")
        rec2, created2 = self.jobs.reserve(resume_key="k", kind="test.reserve")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(rec1.job_id, rec2.job_id)  # duplicate gets the winner's row

    def test_reservation_stays_active_as_dedup_token(self) -> None:
        # An unclaimed reserved key stays an active (queued) durable dedup
        # token — every later reserve dedups against it, independent of any
        # task's lifecycle (the "redelivery after completion" guarantee).
        rec, created = self.jobs.reserve(resume_key="k", kind="test.reserve")
        self.assertTrue(created)
        self.assertEqual(self.jobs.get_by_resume_key("k").status, "queued")
        for _ in range(3):
            _, dup = self.jobs.reserve(resume_key="k", kind="test.reserve")
            self.assertFalse(dup)


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


class _StubTaskHandler:
    """Gate boundary stub for the durable-delivery model.

    The rewritten gate depends only on the real ``JobService`` (durable enqueue)
    and the real ``TaskLedger`` (status-aware dedup lookup). It must NEVER call
    ``start_autonomous_task`` — that boundary moved to the runner — so this stub
    records (and loudly rejects) any accidental call so tests can assert it
    stayed unused.
    """

    def __init__(self, job_service: JobService, task_ledger: TaskLedger) -> None:
        self.job_service = job_service
        self.task_ledger = task_ledger
        self.start_calls: list[Any] = []

    def start_autonomous_task(self, *args: Any, **kwargs: Any) -> str:
        self.start_calls.append((args, kwargs))
        raise AssertionError("gate must not call start_autonomous_task")


def _ctx(message_id: int | None = 111) -> dict[str, Any]:
    inbound: dict[str, Any] = {"channel": "telegram"}
    if message_id is not None:
        inbound["message_id"] = message_id
    return {"inbound": inbound}


def _bot(*, flag: bool, task_handler: Any, observe: Any) -> BotService:
    bot = BotService.__new__(BotService)
    bot.config = SimpleNamespace(f4_deterministic_delegation=flag)
    bot.observe = observe
    bot._task_handler = task_handler
    return bot


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "claw.db"
        self.jobs = JobService(db)
        self.ledger = TaskLedger(db)
        self.observe = _Observe()
        self.th = _StubTaskHandler(self.jobs, self.ledger)

    def _gate(self, text: str, *, flag: bool = True, ctx: Any = "__default__", th: Any = None):
        bot = _bot(flag=flag, task_handler=th or self.th, observe=self.observe)
        return bot._maybe_handle_f4_deterministic_delegation(
            text, session_id="tg-1", context_metadata=_ctx() if ctx == "__default__" else ctx
        )

    def _delivery_key(self, message_id: int = 111) -> str:
        return f"f4b-delegation:tg-1:{message_id}"

    def _delivery_job(self, message_id: int = 111):
        return self.jobs.get_by_resume_key(self._delivery_key(message_id))

    def _seed_linked_task(self, message_id: int, status: str) -> str:
        task_id = f4b_delivery_task_id(self._delivery_key(message_id))
        self.ledger.create(
            task_id=task_id,
            session_id="tg-1",
            objective="Revisa el feed de X",
            runtime="coordinator",
            status=status,
        )
        return task_id

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

    # 1. match -> exactly one durable f4b.delegation job + truthful accepted ack
    def test_match_enqueues_one_delivery_job_with_accepted_ack(self) -> None:
        resp = self._gate("Haz un repaso por X")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("en una tarea de fondo", resp)
        self.assertNotIn("ToolSearch", resp)
        # the durable delivery job exists with the right kind + deterministic id
        job = self._delivery_job()
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.kind, F4_DELEGATION_JOB_KIND)
        self.assertEqual(job.payload["task_id"], f4b_delivery_task_id(self._delivery_key()))
        self.assertEqual(job.payload["session_id"], "tg-1")
        self.assertEqual(job.payload["mode"], "chat")
        self.assertEqual(job.payload["task_kind"], "authenticated_browse")
        # the runner is NOT run here -> no coordinator job yet, no start_autonomous_task
        self.assertEqual(self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50), [])
        self.assertEqual(self.th.start_calls, [])
        self.assertIn("f4_deterministic_delegation_enqueued", self.observe.types())

    # 2. flag OFF: fall through, no job, no start
    def test_flag_off_falls_through(self) -> None:
        self.assertIsNone(self._gate("Haz un repaso por X", flag=False))
        self.assertIsNone(self._delivery_job())
        self.assertEqual(self.th.start_calls, [])

    # 3 + 4. gate independent of CLAW_DISABLE_TASK_INTENT_ROUTER; captures first
    def test_gate_independent_of_broad_router_flag(self) -> None:
        for val in ("0", "1"):
            with self.subTest(broad_router=val), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "claw.db"
                jobs = JobService(db)
                th = _StubTaskHandler(jobs, TaskLedger(db))
                bot = _bot(flag=True, task_handler=th, observe=_Observe())
                with patch.dict(os.environ, {"CLAW_DISABLE_TASK_INTENT_ROUTER": val}):
                    resp = bot._maybe_handle_f4_deterministic_delegation(
                        "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx()
                    )
                self.assertIsNotNone(resp)
                job = jobs.get_by_resume_key("f4b-delegation:tg-1:111")
                self.assertIsNotNone(job)
                assert job is not None
                self.assertEqual(job.kind, F4_DELEGATION_JOB_KIND)
                self.assertEqual(th.start_calls, [])

    # 5. duplicate delivery (same message_id) while queued -> one job, queued dedup ack
    def test_duplicate_delivery_one_job_queued_dedup_ack(self) -> None:
        r1 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        r2 = self._gate("Haz un repaso por X", ctx=_ctx(111))
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        assert r1 is not None and r2 is not None
        self.assertIn("en una tarea de fondo", r1)
        # No linked task row exists yet (runner not run) -> queued dedup ack.
        self.assertIn("ya fue aceptada y está en cola", r2)
        self.assertNotIn("en marcha", r2)
        self.assertEqual(len(self.jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)), 1)
        self.assertEqual(self.th.start_calls, [])
        self.assertIn("f4_deterministic_delegation_matched", self.observe.types())

    # 6. legitimate repeat (new message_id) -> new job
    def test_legitimate_repeat_new_job(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        self._gate("Haz un repaso por X", ctx=_ctx(222))
        self.assertIsNotNone(self._delivery_job(111))
        self.assertIsNotNone(self._delivery_job(222))
        self.assertEqual(len(self.jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)), 2)
        self.assertEqual(self.th.start_calls, [])

    # 7a. status-aware dedup: linked task running -> "ya está en marcha"
    def test_dedup_ack_running_when_linked_task_running(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        self._seed_linked_task(111, "running")
        resp = self._gate("Haz un repaso por X", ctx=_ctx(111))
        assert resp is not None
        self.assertIn("ya está en marcha", resp)

    # 7b. status-aware dedup: linked task terminal -> "ya fue procesada"
    def test_dedup_ack_processed_when_linked_task_terminal(self) -> None:
        for status in sorted(TERMINAL_STATUSES):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "claw.db"
                jobs = JobService(db)
                ledger = TaskLedger(db)
                th = _StubTaskHandler(jobs, ledger)
                bot = _bot(flag=True, task_handler=th, observe=_Observe())
                bot._maybe_handle_f4_deterministic_delegation(
                    "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
                )
                ledger.create(
                    task_id=f4b_delivery_task_id("f4b-delegation:tg-1:111"),
                    session_id="tg-1",
                    objective="Revisa el feed de X",
                    runtime="coordinator",
                    status=status,
                )
                resp = bot._maybe_handle_f4_deterministic_delegation(
                    "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
                )
                assert resp is not None
                self.assertIn("ya fue procesada", resp)

    # 7c. status-aware dedup: no linked task row -> "ya fue aceptada y está en cola"
    def test_dedup_ack_queued_when_no_linked_task(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        resp = self._gate("Haz un repaso por X", ctx=_ctx(111))
        assert resp is not None
        self.assertIn("ya fue aceptada y está en cola", resp)

    # 7d. BLOCKER regression: once the delivery job TERMINALIZES, the active-only
    # resume_key index stops deduping; the deterministic-task_id ledger pre-check
    # must still dedup (no second delivery job, truthful "ya fue procesada").
    def test_redelivery_after_terminalized_delivery_job_dedups_no_new_job(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        job = self._delivery_job(111)
        assert job is not None
        self.jobs.complete(job.job_id, result={"task_id": "x"})  # terminalize delivery job
        self._seed_linked_task(111, "succeeded")  # bootstrap already materialised the task
        resp = self._gate("Haz un repaso por X", ctx=_ctx(111))
        assert resp is not None
        self.assertIn("ya fue procesada", resp)
        self.assertNotIn("tarea de fondo", resp)
        # No SECOND delivery job was enqueued.
        self.assertEqual(len(self.jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)), 1)
        self.assertEqual(self.th.start_calls, [])

    # 7e. existence-keying preserves legitimate retry: a redelivery whose
    # deterministic task_id has NO ledger row (e.g. a prior coordinator_unavailable
    # bootstrap that wrote no row) falls through -> accepted ack + a new job.
    def test_redelivery_without_ledger_row_falls_through_to_accepted(self) -> None:
        self._gate("Haz un repaso por X", ctx=_ctx(111))
        job = self._delivery_job(111)
        assert job is not None
        self.jobs.fail(job.job_id, error="coordinator_unavailable", retry=False)  # terminal, no row
        resp = self._gate("Haz un repaso por X", ctx=_ctx(111))
        assert resp is not None
        self.assertIn("en una tarea de fondo", resp)  # legitimate re-attempt
        self.assertEqual(len(self.jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)), 2)

    # 8. non-matching conversation not captured
    def test_non_matching_not_captured(self) -> None:
        for text in ("¿Qué es X?", "Escribe un post para X", "hola"):
            with self.subTest(text=text):
                self.assertIsNone(self._gate(text))
        self.assertEqual(self.th.start_calls, [])

    def test_no_delivery_id_falls_through(self) -> None:
        resp = self._gate("Haz un repaso por X", ctx=_ctx(message_id=None))
        self.assertIsNone(resp)
        self.assertEqual(self.th.start_calls, [])
        self.assertIsNone(self._delivery_job())
        self.assertIn("f4_deterministic_delegation_skipped_no_delivery_id", self.observe.types())

    # 9. reserve raises (job_service down) -> truthful failure, no fabricated promise
    def test_reserve_failure_returns_truthful_message(self) -> None:
        def _boom(*_: Any, **__: Any):
            raise RuntimeError("simulated job_service down")

        with patch.object(self.jobs, "reserve", side_effect=_boom):
            resp = self._gate("Haz un repaso por X")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("no se encoló nada", resp)
        self._assert_no_unsupported_promise(resp)
        self.assertEqual(self.th.start_calls, [])
        self.assertIn("f4_deterministic_delegation_failed", self.observe.types())

    # 10. concurrent duplicate delivery: ONE job, exactly one creator, both truthful.
    def test_concurrent_duplicate_elects_one_creator(self) -> None:
        for _ in range(25):
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "claw.db"
                jobs = JobService(db)
                th = _StubTaskHandler(jobs, TaskLedger(db))
                bot = _bot(flag=True, task_handler=th, observe=_Observe())
                barrier = threading.Barrier(2)
                results: dict[int, Any] = {}

                def worker(
                    i: int, _bot_ref: BotService = bot, _res: dict[int, Any] = results
                ) -> None:
                    barrier.wait()
                    _res[i] = _bot_ref._maybe_handle_f4_deterministic_delegation(
                        "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
                    )

                threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertEqual(len(jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)), 1)
                self.assertEqual(th.start_calls, [])
                self.assertEqual(len(results), 2)
                for r in results.values():
                    self.assertIsNotNone(r)
                accepted = sum(1 for r in results.values() if "en una tarea de fondo" in r)
                dedup = sum(1 for r in results.values() if "ya fue aceptada y está en cola" in r)
                self.assertEqual(accepted, 1)  # exactly one creator got the accepted ack
                self.assertEqual(dedup, 1)  # exactly one duplicate got the dedup ack

    def test_observe_none_does_not_crash(self) -> None:
        bot = _bot(flag=True, task_handler=self.th, observe=None)
        resp = bot._maybe_handle_f4_deterministic_delegation(
            "Haz un repaso por X", session_id="tg-1", context_metadata=_ctx(111)
        )
        self.assertIsNotNone(resp)
        self.assertIsNotNone(self._delivery_job())
        self.assertEqual(self.th.start_calls, [])


class RealChainIntegrationTests(unittest.TestCase):
    """End-to-end through the REAL gate -> JobService.reserve: the gate now
    enqueues exactly one durable ``f4b.delegation`` job (no inline start, no
    coordinator job yet — the runner materialises those off-tick). The brain
    executor must never be called."""

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

    def _assert_durable_delivery_job(self, runtime: Any, message_id: int) -> None:
        delivery_key = f"f4b-delegation:tg-1:{message_id}"
        job = runtime.job_service.get_by_resume_key(delivery_key)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.kind, F4_DELEGATION_JOB_KIND)
        self.assertEqual(job.payload["task_id"], f4b_delivery_task_id(delivery_key))
        # The gate does NOT execute the job: no coordinator task is enqueued yet.
        self.assertEqual(
            runtime.job_service.list(kinds=["coordinator.autonomous_task"], limit=50), []
        )

    def test_real_bot_handle_text_enqueues_durable_delivery_job(self) -> None:
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
                self._assert_durable_delivery_job(runtime, 777)

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
                self._assert_durable_delivery_job(runtime, 888)


if __name__ == "__main__":
    unittest.main()
