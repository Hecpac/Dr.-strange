"""E2E autonomy harness — verifies router/resolver/mission integration.

Each test exercises BotService through ``runtime.bot.handle_text`` to
confirm the full chain: capability classification, routing, slash-command
guarding, NotebookLM resolver, and approval-required negative controls.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def _stub_llm(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="ok",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


def _runtime(tmp_root: Path):
    env = {
        "DB_PATH": str(tmp_root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(tmp_root / "workspace"),
        "AGENT_STATE_ROOT": str(tmp_root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(tmp_root / "evals"),
        "APPROVALS_ROOT": str(tmp_root / "approvals"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
    }
    return env


class AutonomyE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self._pipeline_state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._pipeline_state_tmp.cleanup)
        patcher = patch.dict(
            os.environ,
            {"PIPELINE_STATE_ROOT": str(Path(self._pipeline_state_tmp.name) / "pipeline")},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _events_of(self, runtime, kind: str) -> list[dict]:
        return [
            event
            for event in runtime.observe.recent_events(limit=200)
            if event.get("event_type") == kind
        ]

    def _force_production_env(self, runtime) -> None:
        """Force the environment detector into ``claw_production`` so the
        router does NOT short-circuit to runtime_handoff during tests."""
        from claw_v2.execution_environment import ExecutionEnvironment

        runtime.bot._execution_environment = ExecutionEnvironment(
            kind="claw_production",
            can_run_bash=True,
            can_run_python_module=True,
            can_access_browser_cli=True,
            can_restart_launchd=True,
            reason="forced_for_test",
        )

    def test_ai_news_routes_to_skill_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                self._force_production_env(runtime)
                # Force runtime probe to True to avoid socket fluctuation
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: True}
                )()
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="dame noticias AI de hoy",
                )
                self.assertIn("ai-news-daily", reply)
                events = self._events_of(runtime, "capability_route_selected")
                self.assertTrue(events)
                payload = events[-1].get("payload") or {}
                self.assertEqual(payload.get("route"), "skill")
                self.assertEqual(payload.get("task_kind"), "ai_news_brief")
                self.assertFalse(payload.get("ask_user"))

    def test_ai_news_skill_route_executes_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                self._force_production_env(runtime)
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: True}
                )()
                calls: list[tuple[str, str, str, str]] = []

                def fake_run_skill(agent: str, skill: str, context: str = "", *, lane: str = "research") -> str:
                    calls.append((agent, skill, context, lane))
                    return "AI Brief listo\nFuente: test"

                runtime.bot.sub_agents.run_skill = fake_run_skill  # type: ignore[method-assign]
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="dame noticias AI de hoy",
                )

                self.assertIn("Tarea de skill iniciada", reply)
                self.assertIn("ai-news-daily", reply)
                match = re.search(r"`([^`]+:skill:\d+)`", reply)
                self.assertIsNotNone(match)
                task_id = match.group(1)
                self.assertTrue(runtime.bot.wait_for_skill_task(task_id, timeout=2.0))
                self.assertEqual(calls[0][0], "alma")
                self.assertEqual(calls[0][1], "ai-news-daily")
                self.assertIn("dame noticias AI de hoy", calls[0][2])
                record = runtime.task_ledger.get(task_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.status, "succeeded")
                self.assertEqual(record.verification_status, "passed")
                completed = self._events_of(runtime, "autonomous_task_completed")
                self.assertTrue(completed)
                self.assertEqual(completed[-1]["payload"]["task_id"], task_id)

    def test_plain_status_is_local_and_does_not_call_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: True}
                )()
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="tg-1",
                    text="Estas vivo",
                )
                self.assertIn("Estoy vivo", reply)
                self.assertIn("/restart", reply)
                self.assertEqual(self._events_of(runtime, "llm_response"), [])

    def test_ai_news_blocked_when_no_route_available(self) -> None:
        from claw_v2.execution_environment import ExecutionEnvironment

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                # Strip the ai-news-daily skill from config and force runtime down.
                runtime.bot.config.scheduled_sub_agents = []
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: False}
                )()
                # Pretend we're running on a local terminal (not sandbox) so
                # router can compute the blocked path instead of handoff.
                runtime.bot._execution_environment = ExecutionEnvironment(
                    kind="local_terminal",
                    can_run_bash=True,
                    can_run_python_module=True,
                    can_access_browser_cli=False,
                    can_restart_launchd=True,
                    reason="forced_for_test",
                )
                runtime.bot.set_capability_status(
                    "browser_use", available=False, reason="degraded"
                )
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="dame noticias AI de hoy",
                )
                self.assertIn("No puedo obtener noticias AI", reply)
                events = self._events_of(runtime, "capability_route_blocked")
                self.assertTrue(events)

    def test_sandbox_environment_dispatches_runtime_handoff(self) -> None:
        from claw_v2.execution_environment import ExecutionEnvironment

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                runtime.bot._execution_environment = ExecutionEnvironment(
                    kind="claude_code_sandbox",
                    can_run_bash=False,
                    can_run_python_module=False,
                    can_access_browser_cli=False,
                    can_restart_launchd=False,
                    reason="forced_for_test",
                )
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: False}
                )()
                runtime.config.web_chat_port = 1
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="dame noticias AI de hoy",
                )
                # Sandbox + gateway down → queued handoff message
                self.assertIn("./scripts/restart.sh", reply)
                events = self._events_of(runtime, "runtime_handoff_created")
                self.assertTrue(events)
                payload = events[-1].get("payload") or {}
                self.assertEqual(payload.get("dispatch_method"), "queue")

    def test_x_trends_routes_to_cdp_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                self._force_production_env(runtime)
                runtime.bot.set_capability_status("chrome_cdp", available=True)
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: True}
                )()
                # CDP route returns None (let chrome handler proceed); confirm
                # event is emitted before fallthrough.
                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="quiero ver X trends ahora",
                )
                events = self._events_of(runtime, "capability_route_selected")
                routes = [e["payload"].get("route") for e in events]
                self.assertIn("cdp", routes)

    def test_x_trends_routes_to_runtime_when_cdp_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                self._force_production_env(runtime)
                runtime.bot.set_capability_status("chrome_cdp", available=False)
                runtime.bot._runtime_probe = type(
                    "P", (), {"is_alive": lambda self: True}
                )()
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="quiero ver X trends ahora",
                )
                self.assertIn("vía runtime", reply)
                events = self._events_of(runtime, "capability_route_selected")
                routes = [e["payload"].get("route") for e in events]
                self.assertIn("runtime", routes)

    def test_capability_router_does_not_intercept_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                # /quality is a real slash command — must not get hijacked.
                reply = runtime.bot.handle_text(
                    user_id="123", session_id="s1", text="/quality"
                )
                # Quality returns a JSON payload string
                self.assertIn("\"tasks\"", reply)
                events = self._events_of(runtime, "capability_route_selected")
                self.assertEqual(events, [])

    def test_last_notebook_resolves_via_active_object(self) -> None:
        # NlmHandler now consults active_object before saying "no hay cuaderno".
        from claw_v2.nlm_handler import NlmHandler

        def get_state(_: str):
            return {
                "active_object": {"kind": "notebook", "id": "nb-final", "title": "FinalDoc"}
            }

        handler = NlmHandler(get_session_state=get_state)
        message = handler._missing_notebook_response("s1")
        self.assertIn("FinalDoc", message)
        self.assertNotIn("No hay cuaderno activo", message)

    def test_social_publish_natural_language_does_not_intercept(self) -> None:
        # The router should NOT intercept critical actions; existing autonomy
        # policy in task_handler/coordinator handles them with their own flow.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                runtime.bot.handle_text(
                    user_id="123", session_id="s1", text="/autonomy autonomous"
                )
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="publica esto en X y haz git push",
                )
                # Should be the existing autonomy policy block, not the router's
                # generic "approval required".
                self.assertIn("autonomy policy blocked", reply)

    def test_pipeline_merge_natural_language_blocked_by_autonomy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                runtime.bot.handle_text(
                    user_id="123", session_id="s1", text="/autonomy autonomous"
                )
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="haz merge del PR y deploy a prod",
                )
                self.assertIn("autonomy policy blocked", reply)

    def test_continue_resumes_mission(self) -> None:
        from claw_v2.mission_controller import MissionController

        states: dict[str, dict] = {}

        def get(session_id: str):
            return dict(states.get(session_id, {}))

        def update(session_id: str, **kwargs):
            states.setdefault(session_id, {}).update(kwargs)

        mc = MissionController(
            get_session_state=get, update_session_state=update
        )
        mc.start_or_resume(
            session_id="s1",
            objective="dame ai news",
            task_kind="ai_news_brief",
            route="skill",
        )
        # Latest relevant returns mission, even after restart-style "continue"
        latest = mc.latest_relevant("s1")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.task_kind, "ai_news_brief")

    def test_quality_metrics_include_capability_route_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime(root), clear=False):
                runtime = build_runtime(anthropic_executor=_stub_llm)
                # Emit some sample route events
                runtime.observe.emit(
                    "capability_route_selected",
                    payload={"route": "skill", "task_kind": "ai_news_brief"},
                )
                runtime.observe.emit(
                    "capability_route_blocked",
                    payload={"route": "blocked", "task_kind": "x_trends"},
                )
                reply = runtime.bot.handle_text(
                    user_id="123", session_id="s1", text="/quality"
                )
                import json as _json

                payload = _json.loads(reply)
                self.assertIn("autonomy_routing", payload)
                self.assertEqual(
                    payload["autonomy_routing"]["capability_route_selected_count"], 1
                )
                self.assertEqual(
                    payload["autonomy_routing"]["capability_route_blocked_count"], 1
                )


if __name__ == "__main__":
    unittest.main()
