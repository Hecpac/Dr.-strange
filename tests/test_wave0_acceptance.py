from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.bot_helpers import OwnerDelegationIntent, detect_owner_delegation
from claw_v2.idle_executor import IdleOwnershipExecutor
from claw_v2.main import build_runtime
from claw_v2.memory import MemoryStore
from claw_v2.state_handler import (
    PENDING_ACTION_TTL_SECONDS,
    StateHandler,
)
from claw_v2.telemetry import read_jsonl
from claw_v2.types import LLMResponse


class _StubTaskHandler:
    def derive_task_dependencies(self, *_args, **_kwargs):
        return []

    def upsert_task_queue_entry(self, queue, **_kwargs):
        return queue

    def mark_first_task_queue_entry(self, queue, *, from_status, to_status):
        out = []
        changed = False
        for item in queue:
            if not changed and item.get("status") == from_status:
                out.append({**item, "status": to_status})
                changed = True
            else:
                out.append(item)
        return out

    def mark_task_queue_in_progress(self, queue, **_kwargs):
        return queue


class _Observe:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs.get("payload") or {})))


class _IdleStartTaskHandler:
    def __init__(self, response: str | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response or "Voy con eso.\nTarea autónoma iniciada: `tg-wave0:idle`\nModo: research"

    def start_autonomous_task(self, session_id: str, objective: str, **kwargs):
        self.calls.append({"session_id": session_id, "objective": objective, **kwargs})
        return self.response


class _IdleResumeTaskHandler:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result or {
            "advanced": True,
            "reason": "resumed_by_idle_executor",
            "message": "La retomé automáticamente.",
        }

    def resume_idle_autonomous_task(self, session_id: str, task_id: str) -> dict:
        self.calls.append((session_id, task_id))
        return dict(self.result)


def _handler(memory: MemoryStore, observe: _Observe | None = None) -> StateHandler:
    return StateHandler(
        brain_memory=memory,
        task_handler=_StubTaskHandler(),
        observe=observe,
    )


def _execution_intent() -> OwnerDelegationIntent:
    intent = detect_owner_delegation("hazlo tú, no me preguntes")
    assert intent is not None
    return intent


def _runtime_with_executor(tmp: str, executor):
    root = Path(tmp)
    env = {
        "DB_PATH": str(root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "AGENT_STATE_ROOT": str(root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
        "APPROVALS_ROOT": str(root / "approvals"),
        "TELEMETRY_ROOT": str(root / "telemetry"),
        "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "CLAW_DISABLE_TASK_INTENT_ROUTER": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        return build_runtime(anthropic_executor=executor)


class Wave0GoldenTraceTests(unittest.TestCase):
    def test_owner_delegation_true_positive_resolves_safe_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            handler = _handler(memory)
            memory.update_session_state(
                "tg-wave0",
                pending_action="generar reporte local de tareas pendientes",
                active_object={
                    "pending_action_meta": {
                        "created_at": time.time(),
                        "created_message_id": 0,
                        "ttl_seconds": PENDING_ACTION_TTL_SECONDS,
                        "topic": "reporte local de tareas pendientes",
                    }
                },
            )

            resolution = handler.resolve_delegated_objective(
                session_id="tg-wave0",
                text="hazlo tú, no me preguntes",
                intent=_execution_intent(),
            )

            self.assertEqual(
                resolution.objective, "generar reporte local de tareas pendientes"
            )
            self.assertFalse(resolution.is_risky)
            self.assertEqual(resolution.resolution_source, "session_state.pending_action")

    def test_owner_delegation_false_positive_stays_chat(self) -> None:
        self.assertIsNone(detect_owner_delegation("¿debería hacerlo tú o yo?"))
        self.assertIsNone(
            detect_owner_delegation("antes de que hagas algo, dime si decide tú")
        )
        self.assertIsNone(detect_owner_delegation("what would happen if you decide?"))

    def test_implicit_approval_fresh_pending_action_executes_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            handler = _handler(memory)
            memory.store_message("tg-wave0", "assistant", "Tengo `make test` listo. ¿Lo ejecuto?")
            memory.update_session_state(
                "tg-wave0",
                current_goal="correr make test local",
                pending_action="correr make test local",
                active_object={
                    "pending_action_meta": {
                        "created_at": time.time(),
                        "created_message_id": memory.last_message_id("tg-wave0"),
                        "ttl_seconds": PENDING_ACTION_TTL_SECONDS,
                        "topic": "correr make test local",
                    }
                },
            )

            response = handler.maybe_resolve_stateful_followup(
                "dale", session_id="tg-wave0"
            )

            self.assertNotIsInstance(response, str)
            self.assertIn("Continúa con esta acción pendiente", response.text)  # type: ignore[union-attr]

    def test_implicit_approval_stale_pending_action_asks_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            observe = _Observe()
            handler = _handler(memory, observe)
            memory.update_session_state(
                "tg-wave0",
                current_goal="nuevo tema wave 0",
                pending_action="confirmar que la imagen quedó visible",
                active_object={
                    "pending_action_meta": {
                        "created_at": time.time() - PENDING_ACTION_TTL_SECONDS - 1,
                        "created_message_id": 0,
                        "ttl_seconds": PENDING_ACTION_TTL_SECONDS,
                        "topic": "imagen visible",
                    }
                },
            )

            response = handler.maybe_resolve_stateful_followup(
                "dale", session_id="tg-wave0"
            )

            self.assertIsInstance(response, str)
            self.assertEqual(response.count("?"), 0)
            self.assertIn("ya no está vigente", response)
            self.assertEqual(memory.get_session_state("tg-wave0")["pending_action"], "")

    def test_implicit_approval_destructive_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            handler = _handler(memory)
            memory.update_session_state(
                "tg-wave0",
                current_goal="deploy a production",
                pending_action="deploy a production y publicar el release",
                active_object={
                    "pending_action_meta": {
                        "created_at": time.time(),
                        "created_message_id": 0,
                        "ttl_seconds": PENDING_ACTION_TTL_SECONDS,
                        "topic": "deploy a production",
                    }
                },
            )

            response = handler.maybe_resolve_stateful_followup(
                "ok", session_id="tg-wave0"
            )

            self.assertIsInstance(response, str)
            self.assertIn("No la ejecuto con un ok corto", response)

    def test_idle_executor_flag_off_emits_would_advance_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = MemoryStore(root / "claw.db")
            observe = _Observe()
            memory.update_session_state(
                "tg-wave0",
                task_queue=[
                    {
                        "task_id": "task-safe-1",
                        "summary": "generar reporte local de pendientes",
                        "mode": "research",
                        "status": "pending",
                    }
                ],
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                observe=observe,
                telemetry_root=root / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "0"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertTrue(result.telemetry_only)
            self.assertFalse(result.advanced)
            self.assertEqual(result.event_names, ("idle_executor_would_advance",))
            self.assertIn(
                "idle_executor_would_advance",
                [name for name, _ in observe.events],
            )
            rows = read_jsonl(root / "telemetry" / "idle_executor.jsonl")
            self.assertEqual(rows[-1]["event_type"], "idle_executor_would_advance")
            active_object = memory.get_session_state("tg-wave0")["active_object"]
            self.assertIn("idle_executor_last_candidate", active_object)
            self.assertNotIn("idle_executor", active_object)

    def test_idle_executor_circuit_breaker_suspends_stalled_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            observe = _Observe()
            memory.update_session_state(
                "tg-wave0",
                verification_status="pending",
                task_queue=[
                    {
                        "task_id": "task-stalled",
                        "summary": "validar manifiesto local",
                        "mode": "ops",
                        "status": "in_progress",
                    }
                ],
                active_object={
                    "idle_executor": {
                        "task_id": "task-stalled",
                        "verification_status": "pending",
                        "unchanged_count": 2,
                        "advanced": True,
                    }
                },
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                observe=observe,
                telemetry_root=Path(tmp) / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "1"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertTrue(result.circuit_broke)
            self.assertIn("idle_executor_circuit_broke", result.event_names)
            state = memory.get_session_state("tg-wave0")
            self.assertEqual(state["verification_status"], "blocked")
            self.assertEqual(state["task_queue"][0]["status"], "blocked")
            self.assertEqual(
                state["task_queue"][0]["blocked_reason"],
                "idle_executor_stall",
            )

    def test_idle_executor_flag_on_starts_safe_pending_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = MemoryStore(root / "claw.db")
            observe = _Observe()
            task_handler = _IdleStartTaskHandler()
            memory.update_session_state(
                "tg-wave0",
                task_queue=[
                    {
                        "task_id": "task-safe-1",
                        "summary": "generar reporte local de pendientes",
                        "mode": "research",
                        "status": "pending",
                    }
                ],
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                observe=observe,
                task_handler=task_handler,
                telemetry_root=root / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "1"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertFalse(result.telemetry_only)
            self.assertTrue(result.advanced)
            self.assertEqual(result.event_names, ("idle_executor_would_advance", "idle_executor_did_advance"))
            self.assertEqual(task_handler.calls[0]["objective"], "generar reporte local de pendientes")
            self.assertEqual(task_handler.calls[0]["task_kind"], "idle_executor_advance")
            state = memory.get_session_state("tg-wave0")
            self.assertTrue(state["active_object"]["idle_executor"]["advanced"])
            rows = read_jsonl(root / "telemetry" / "idle_executor.jsonl")
            self.assertEqual(rows[-1]["event_type"], "idle_executor_did_advance")

    def test_idle_executor_flag_on_resumes_durable_autonomous_task(self) -> None:
        from claw_v2.task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = MemoryStore(root / "memory.db")
            ledger = TaskLedger(root / "ledger.db")
            observe = _Observe()
            task_handler = _IdleResumeTaskHandler()
            ledger.create(
                task_id="tg-wave0:durable",
                session_id="tg-wave0",
                objective="revisar logs locales",
                mode="research",
                runtime="coordinator",
                status="running",
                metadata={"autonomous": True},
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                task_ledger=ledger,
                observe=observe,
                task_handler=task_handler,
                telemetry_root=root / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "1"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertTrue(result.advanced)
            self.assertEqual(task_handler.calls, [("tg-wave0", "tg-wave0:durable")])
            self.assertIn("idle_executor_did_advance", result.event_names)
            self.assertTrue(memory.get_session_state("tg-wave0")["active_object"]["idle_executor"]["advanced"])

    def test_idle_executor_does_not_duplicate_in_progress_queue_without_durable_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "claw.db")
            observe = _Observe()
            task_handler = _IdleStartTaskHandler()
            memory.update_session_state(
                "tg-wave0",
                task_queue=[
                    {
                        "task_id": "task-active",
                        "summary": "validar reporte local",
                        "mode": "research",
                        "status": "in_progress",
                    }
                ],
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                observe=observe,
                task_handler=task_handler,
                telemetry_root=Path(tmp) / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "1"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertFalse(result.advanced)
            self.assertEqual(task_handler.calls, [])
            self.assertIn("idle_executor_blocked", result.event_names)
            event_payloads = [payload for name, payload in observe.events if name == "idle_executor_blocked"]
            self.assertEqual(event_payloads[-1]["reason"], "non_durable_in_progress_queue_item")

    def test_idle_executor_already_running_noop_does_not_increment_stall(self) -> None:
        from claw_v2.task_ledger import TaskLedger

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = MemoryStore(root / "memory.db")
            ledger = TaskLedger(root / "ledger.db")
            observe = _Observe()
            task_handler = _IdleResumeTaskHandler(
                {
                    "advanced": False,
                    "reason": "already_running",
                    "message": "task tg-wave0:durable is already running",
                }
            )
            ledger.create(
                task_id="tg-wave0:durable",
                session_id="tg-wave0",
                objective="revisar logs locales",
                mode="research",
                runtime="coordinator",
                status="running",
                metadata={"autonomous": True},
            )
            memory.update_session_state(
                "tg-wave0",
                active_object={
                    "idle_executor": {
                        "task_id": "tg-wave0:durable",
                        "verification_status": "unknown",
                        "unchanged_count": 1,
                        "advanced": True,
                    }
                },
            )
            executor = IdleOwnershipExecutor(
                memory=memory,
                task_ledger=ledger,
                observe=observe,
                task_handler=task_handler,
                telemetry_root=root / "telemetry",
                env={"CLAW_IDLE_EXECUTOR_ENABLED": "1"},
            )

            result = executor.inspect_turn(session_id="tg-wave0")

            self.assertFalse(result.advanced)
            self.assertIn("idle_executor_noop", result.event_names)
            idle_state = memory.get_session_state("tg-wave0")["active_object"]["idle_executor"]
            self.assertFalse(idle_state["advanced"])
            self.assertEqual(idle_state["unchanged_count"], 0)
            self.assertNotIn("idle_executor_circuit_broke", [name for name, _ in observe.events])

    def test_introspection_routing_does_not_start_coding_coordinator(self) -> None:
        def reflective(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="Fallé por no sostener contexto y evidencia. Lo correcto es ajustar el runtime.",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime_with_executor(tmp, reflective)
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-wave0",
                text="¿por qué no completas tareas fáciles?",
                runtime_channel="telegram",
            )

            self.assertIn("contexto", response)
            self.assertNotIn("Tarea autónoma iniciada", response)
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=50)]
            self.assertIn("meta_introspection_match", events)
            self.assertNotIn("autonomous_task_started", events)

    def test_evidence_gate_blocks_completion_claim_without_manifest(self) -> None:
        def false_done(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="Listo, hecho. Cambié el archivo y corrí tests.",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime_with_executor(tmp, false_done)
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-wave0",
                text="arregla el bug de wave 0",
                runtime_channel="telegram",
            )

            self.assertIn("bloqueo verificado", response)
            self.assertNotIn("explicit_blocker", response)
            self.assertIn("Task:", response)
            self.assertNotIn("Cambié el archivo", response)
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=50)]
            self.assertIn("evidence_gate_blocked_completion_claim", events)
            self.assertIn("evidence_gate_explicit_blocker_recorded", events)

    def test_evidence_gate_blocks_start_claim_without_task_or_tool(self) -> None:
        def false_start(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="Voy a depurar el ledger. Arrancando.",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime_with_executor(tmp, false_start)
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-wave0",
                text="arregla el ledger",
                runtime_channel="telegram",
            )

            self.assertIn("bloqueo verificado", response)
            self.assertNotIn("explicit_blocker", response)
            self.assertIn("Task:", response)
            self.assertNotIn("Voy a depurar", response)
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=50)]
            self.assertIn("evidence_gate_blocked_start_claim", events)
            self.assertIn("evidence_gate_explicit_blocker_recorded", events)

    def test_evidence_gate_chat_only_exception_allows_conversational_listo(self) -> None:
        def chat_done(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="Listo, te leí.",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime_with_executor(tmp, chat_done)
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-wave0",
                text="¿entendiste?",
                runtime_channel="telegram",
            )

            self.assertEqual(response, "Listo, te leí.")
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=50)]
            self.assertNotIn("evidence_gate_blocked_completion_claim", events)

    def test_manual_handoff_ban_replaces_run_this_command_reply(self) -> None:
        def handoff(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="Listo. Corre este comando tú: pytest tests/test_wave0_acceptance.py",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime_with_executor(tmp, handoff)
            response = runtime.bot.handle_text(
                user_id="123",
                session_id="tg-wave0",
                text="arregla el bug de wave 0",
                runtime_channel="telegram",
            )

            self.assertIn("No cierro esto con handoff manual", response)
            self.assertNotIn("Corre este comando", response)
            events = [event["event_type"] for event in runtime.observe.recent_events(limit=50)]
            self.assertIn("operator_handoff_guard_triggered", events)


if __name__ == "__main__":
    unittest.main()
