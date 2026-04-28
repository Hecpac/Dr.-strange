from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.llm import LLMRouter
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def _stub_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="ok",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


class _StubAdapter:
    tool_capable = True

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            lane=request.lane,
            provider="anthropic",
            model=request.model,
        )


class PreHookVisibilityTests(unittest.TestCase):
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

    def _make_router(self, hook) -> tuple[LLMRouter, list[dict]]:
        events: list[dict] = []

        def audit(event: dict) -> None:
            events.append(event)

        from claw_v2.config import AppConfig
        config = AppConfig.from_env()
        router = LLMRouter(
            config=config,
            adapters={"anthropic": _StubAdapter()},
            audit_sink=audit,
            pre_hooks=[hook],
        )
        return router, events

    def test_pre_hook_blocked_returns_reason_not_opaque(self) -> None:
        def cost_gate(request: LLMRequest):
            return None
        cost_gate.block_reason = "daily_cost_limit_exceeded"

        router, events = self._make_router(cost_gate)
        response = router.ask(
            "hi",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
        )
        self.assertIn("cost_gate", response.content)
        self.assertIn("daily_cost_limit_exceeded", response.content)
        self.assertEqual(response.artifacts.get("blocked_by"), "cost_gate")
        self.assertEqual(response.artifacts.get("block_reason"), "daily_cost_limit_exceeded")
        block_events = [e for e in events if e["action"] == "llm_pre_hook_blocked"]
        self.assertEqual(len(block_events), 1)
        self.assertEqual(block_events[0]["metadata"]["block_reason"], "daily_cost_limit_exceeded")

    def test_pre_hook_blocked_without_reason_marks_unknown(self) -> None:
        def silent_hook(request: LLMRequest):
            return None

        router, events = self._make_router(silent_hook)
        response = router.ask(
            "hi",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
        )
        self.assertIn("no_reason_provided", response.content)

    def test_pre_hook_blocked_count_in_quality(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=_stub_anthropic)
                for _ in range(3):
                    runtime.observe.emit(
                        "llm_pre_hook_blocked",
                        lane="brain",
                        provider="none",
                        model="none",
                        payload={
                            "blocked_by": "cost_gate",
                            "block_reason": "daily_cost_limit_exceeded",
                        },
                    )
                runtime.observe.emit(
                    "llm_pre_hook_blocked",
                    lane="brain",
                    provider="none",
                    model="none",
                    payload={"blocked_by": "rate_limit", "block_reason": "qps_exceeded"},
                )
                response = runtime.bot.handle_text(
                    user_id="123", session_id="tg-1", text="/quality"
                )
                payload = json.loads(response)
                self.assertIn("pre_hook_blocks", payload)
                self.assertEqual(payload["pre_hook_blocks"]["count"], 4)
                top = {entry["hook"]: entry["count"] for entry in payload["pre_hook_blocks"]["top_hooks"]}
                self.assertEqual(top.get("cost_gate"), 3)
                self.assertEqual(top.get("rate_limit"), 1)

    def test_repeated_block_emits_alert_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=_stub_anthropic)
                # Pre-seed 6 recent blocks for same hook (above threshold of 5)
                for _ in range(6):
                    runtime.observe.emit(
                        "llm_pre_hook_blocked",
                        lane="brain",
                        provider="none",
                        model="none",
                        payload={
                            "blocked_by": "cost_gate",
                            "block_reason": "daily_cost_limit_exceeded",
                        },
                    )
                augmented = runtime.bot._maybe_augment_pre_hook_block(
                    "Request blocked by pre-hook (cost_gate). Reason: daily_cost_limit_exceeded"
                )
                self.assertIn("Atención", augmented)
                self.assertIn("cost_gate", augmented)
                self.assertIn("Revisa configuración", augmented)
                # Also emits the repeated event
                recent = runtime.observe.recent_events(limit=50)
                kinds = [event.get("event_type") for event in recent]
                self.assertIn("pre_hook_blocked_repeated", kinds)

    def test_block_below_threshold_does_not_emit_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=_stub_anthropic)
                for _ in range(3):
                    runtime.observe.emit(
                        "llm_pre_hook_blocked",
                        lane="brain",
                        provider="none",
                        model="none",
                        payload={"blocked_by": "cost_gate", "block_reason": "x"},
                    )
                content = "Request blocked by pre-hook (cost_gate). Reason: x"
                augmented = runtime.bot._maybe_augment_pre_hook_block(content)
                self.assertEqual(augmented, content)
                recent = runtime.observe.recent_events(limit=50)
                kinds = [event.get("event_type") for event in recent]
                self.assertNotIn("pre_hook_blocked_repeated", kinds)

    def test_pre_hook_block_does_not_auto_disable_hook(self) -> None:
        invocations = {"count": 0}

        def cost_gate(request: LLMRequest):
            invocations["count"] += 1
            return None
        cost_gate.block_reason = "test_reason"

        router, _ = self._make_router(cost_gate)
        for _ in range(7):
            router.ask(
                "hi",
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-7",
            )
        # Hook is invoked every call; never auto-disabled
        self.assertEqual(invocations["count"], 7)


if __name__ == "__main__":
    unittest.main()
