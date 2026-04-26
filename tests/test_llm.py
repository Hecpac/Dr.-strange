from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.eval_mocks import StaticAdapter, echo_response
from claw_v2.llm import LLMRouter
from claw_v2.retry_policy import ProviderCircuitBreaker
from claw_v2.types import LLMResponse

from tests.helpers import make_config


class LLMRouterTests(unittest.TestCase):
    def test_secondary_lane_requires_evidence_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic"))},
            )
            with self.assertRaises(ValueError):
                router.ask("judge this", lane="judge")

    def test_secondary_lane_falls_back_to_anthropic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=failing),
                },
            )
            response = router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})
            self.assertEqual(response.provider, "anthropic")
            self.assertTrue(response.degraded_mode)
            self.assertIn("fallback_reason", response.artifacts)

    def test_cross_provider_fallback_does_not_reuse_incompatible_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            recorded: dict[str, object] = {}

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterError("Claude SDK execution failed")

            def fallback(request: LLMRequest) -> LLMResponse:
                recorded["session_id"] = request.session_id
                return LLMResponse(
                    content="fallback ok",
                    lane=request.lane,
                    provider="openai",
                    model=request.model,
                    confidence=0.7,
                    cost_estimate=0.0,
                )

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=failing),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=fallback),
                },
            )
            response = router.ask(
                "verify",
                lane="verifier",
                provider="anthropic",
                session_id="bb1e321e-claude-session",
                evidence_pack={"diff": "x"},
            )
            self.assertEqual(response.provider, "openai")
            self.assertIsNone(recorded["session_id"])

    def test_ollama_lane_routes_to_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.judge_provider = "ollama"
            config.judge_model = "gemma4"
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "ollama": StaticAdapter("ollama", tool_capable=True, responder=echo_response("ollama")),
                },
            )
            response = router.ask("classify this", lane="judge", evidence_pack={"data": "x"})
            self.assertEqual(response.provider, "ollama")
            self.assertEqual(response.model, "gemma4")

    def test_ollama_lane_uses_default_gemma4_model_when_model_not_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.judge_provider = "ollama"
            config.judge_model = None
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "ollama": StaticAdapter("ollama", tool_capable=True, responder=echo_response("ollama")),
                },
            )
            response = router.ask("classify this", lane="judge", evidence_pack={"data": "x"})
            self.assertEqual(response.provider, "ollama")
            self.assertEqual(response.model, "gemma4")

    def test_secondary_fallback_uses_anthropic_worker_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.verifier_provider = "ollama"
            config.verifier_model = None

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "ollama": StaticAdapter("ollama", tool_capable=True, responder=failing),
                },
            )
            response = router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})
            self.assertEqual(response.provider, "anthropic")
            self.assertEqual(response.model, config.worker_model)
            self.assertTrue(response.degraded_mode)

    def test_provider_circuit_opens_after_repeated_adapter_errors_and_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterError("persistent provider timeout")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=failing),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=echo_response("openai")),
                },
                audit_sink=audit_events.append,
                circuit_breaker=ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=60),
            )

            first = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})
            second = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})
            third = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})

            self.assertEqual(first.provider, "openai")
            self.assertEqual(second.provider, "openai")
            self.assertEqual(third.provider, "openai")
            self.assertTrue(any(event["action"] == "llm_circuit_open" for event in audit_events))
            self.assertTrue(any(event["action"] == "llm_circuit_blocked" for event in audit_events))

    def test_provider_circuit_recovers_after_cooldown_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            now = [100.0]
            audit_events: list[dict] = []
            calls = {"anthropic": 0}

            def flaky(request: LLMRequest) -> LLMResponse:
                calls["anthropic"] += 1
                if calls["anthropic"] == 1:
                    raise AdapterError("temporary outage")
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=flaky),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=echo_response("openai")),
                },
                audit_sink=audit_events.append,
                circuit_breaker=ProviderCircuitBreaker(
                    failure_threshold=1,
                    cooldown_seconds=10,
                    clock=lambda: now[0],
                ),
            )

            fallback = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})
            now[0] = 111.0
            recovered = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})

            self.assertEqual(fallback.provider, "openai")
            self.assertEqual(recovered.provider, "anthropic")
            self.assertTrue(any(event["action"] == "llm_circuit_recovered" for event in audit_events))

    def test_fallback_to_anthropic_uses_safe_anthropic_model_when_worker_is_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            config.verifier_provider = "openai"
            config.verifier_model = None
            audit_events: list[dict] = []

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "openai": StaticAdapter("openai", tool_capable=True, responder=failing),
                },
                audit_sink=audit_events.append,
            )
            response = router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})
            self.assertEqual(response.provider, "anthropic")
            self.assertEqual(response.model, "claude-sonnet-4-6")
            self.assertTrue(response.degraded_mode)
            self.assertEqual(audit_events[-1]["action"], "llm_fallback")

    def test_worker_lane_rejects_non_tool_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=echo_response("openai")),
                },
            )
            with self.assertRaises(ValueError):
                router.ask("do work", lane="worker", provider="openai")

    def test_invalid_request_fails_before_adapter_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            calls = 0

            def responder(_: LLMRequest) -> LLMResponse:
                nonlocal calls
                calls += 1
                return echo_response("anthropic")(_)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )
            with self.assertRaisesRegex(ValueError, "timeout"):
                router.ask("do work", lane="brain", timeout=0)
            self.assertEqual(calls, 0)

    def test_codex_worker_lane_routes_to_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            from claw_v2.eval_mocks import StaticAdapter, echo_response
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "codex": StaticAdapter("codex", tool_capable=True, responder=echo_response("codex")),
                },
            )
            response = router.ask("write a function", lane="worker")
            self.assertEqual(response.provider, "codex")

    def test_codex_worker_lane_does_not_fallback_to_anthropic_on_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterError("Codex CLI timed out after 120s")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "codex": StaticAdapter("codex", tool_capable=True, responder=failing),
                },
            )
            with self.assertRaises(AdapterError):
                router.ask("implement change", lane="worker")

    def test_codex_secondary_lane_does_not_fallback_to_anthropic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.research_provider = "codex"
            config.research_model = "gpt-5.5"

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterError("Codex CLI timed out after 300s")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "codex": StaticAdapter("codex", tool_capable=True, responder=failing),
                },
            )
            with self.assertRaises(AdapterError):
                router.ask("research", lane="research", evidence_pack={"task": "x"})

    def test_default_router_includes_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter.default(config)
            self.assertIn("codex", router.adapters)
            from claw_v2.adapters.codex import CodexAdapter
            self.assertIsInstance(router.adapters["codex"], CodexAdapter)


if __name__ == "__main__":
    unittest.main()
