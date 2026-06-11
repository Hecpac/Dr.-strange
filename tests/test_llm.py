from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.eval_mocks import StaticAdapter, echo_response
from claw_v2.llm import LLMRouter
from claw_v2.observation_window import ObservationWindowConfig, ObservationWindowState
from claw_v2.retry_policy import ProviderCircuitBreaker
from claw_v2.types import LLMResponse

from tests.helpers import make_config


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs: object) -> None:
        self.events.append((event, dict(kwargs)))


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

    def test_cost_metering_unknown_abort_is_recorded_for_the_daily_gate(self) -> None:
        # PR #89 review round 3 (codex P1): when the adapter aborts a billable
        # round it could not price (reason=cost_metering_unknown), ask() must
        # emit the cost_metering_unknown marker even though no llm_response is
        # built — otherwise the already-billed round is invisible to the daily
        # freeze gate and repeated calls leak one paid round each.
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []

            def aborting(request: LLMRequest) -> LLMResponse:
                raise AdapterError(
                    "OpenAI request cannot be cost-metered; aborting under max_budget",
                    metadata={"reason": "cost_metering_unknown", "model": request.model},
                )

            router = LLMRouter(
                config=config,
                adapters={"openai": StaticAdapter("openai", tool_capable=True, responder=aborting)},
                audit_sink=audit_events.append,
            )

            with self.assertRaises(AdapterError):
                router.ask("do the task", lane="worker", provider="openai", model="gpt-unpriced-xyz")

            self.assertTrue(
                any(event.get("action") == "cost_metering_unknown" for event in audit_events),
                "abort path must emit cost_metering_unknown so the daily gate can freeze",
            )

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

    def test_llm_response_audit_includes_prompt_size_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                },
                audit_sink=audit_events.append,
            )

            router.ask(
                "verify this",
                lane="verifier",
                provider="anthropic",
                system_prompt="extra verifier rule",
                evidence_pack={"diff": "x"},
            )

            prompt_size = audit_events[-1]["metadata"]["prompt_size"]
            self.assertEqual(prompt_size["lane"], "verifier")
            self.assertEqual(prompt_size["provider"], "anthropic")
            self.assertGreater(prompt_size["prompt_chars"], 0)
            self.assertGreater(prompt_size["effective_input_chars"], prompt_size["prompt_chars"])
            self.assertGreater(prompt_size["effective_system_prompt_chars"], 0)
            self.assertFalse(prompt_size["evidence_pack_truncated"])

    def test_anthropic_cache_reads_do_not_count_against_token_window_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []
            observe = _RecordingObserve()
            window = ObservationWindowState(
                observe=observe,
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(
                    token_window_cap=100_000,
                    token_soft_limit_ratio=0.8,
                    token_hard_limit_ratio=1.0,
                ),
            )

            def cached_anthropic_response(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="done",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    artifacts={
                        "usage": {
                            "input_tokens": 5_884,
                            "cache_creation_input_tokens": 83_965,
                            "cache_read_input_tokens": 1_734_440,
                            "output_tokens": 6_861,
                        }
                    },
                )

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter(
                        "anthropic",
                        tool_capable=True,
                        responder=cached_anthropic_response,
                    ),
                },
                audit_sink=audit_events.append,
                observation_window=window,
            )

            router.ask("inspect", lane="brain", provider="anthropic")

            token_usage = audit_events[-1]["metadata"]["token_usage"]
            self.assertEqual(token_usage["provider_total_tokens"], 1_831_150)
            self.assertEqual(token_usage["cache_read_input_tokens"], 1_734_440)
            self.assertEqual(token_usage["cache_creation_input_tokens"], 83_965)
            self.assertEqual(token_usage["raw_input_tokens"], 5_884)
            self.assertEqual(token_usage["total_tokens"], 96_710)
            self.assertTrue(token_usage["token_window_excludes_cache_read"])
            token_window = window.status_payload()["token_window"]
            self.assertEqual(token_window["total_tokens"], 96_710)
            self.assertFalse(window.frozen)

            recorded = [
                payload["payload"]
                for name, payload in observe.events
                if name == "llm_token_window_recorded"
            ][0]
            self.assertEqual(recorded["tokens"], 96_710)
            self.assertEqual(recorded["provider_total_tokens"], 1_831_150)
            self.assertEqual(recorded["excluded_cache_read_tokens"], 1_734_440)
            self.assertTrue(recorded["token_window_excludes_cache_read"])

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

    def test_worker_heavy_lane_rejects_non_tool_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "google": StaticAdapter("google", tool_capable=False, responder=echo_response("google")),
                },
            )
            with self.assertRaises(ValueError):
                router.ask("debug this", lane="worker_heavy", provider="google")

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

    def test_control_role_selects_policy_provider_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.judge_provider = "codex"
            seen: dict[str, object] = {}

            def responder(request: LLMRequest) -> LLMResponse:
                seen["provider"] = request.provider
                seen["role"] = request.role
                seen["timeout"] = request.timeout
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )

            response = router.ask(
                "decide",
                lane="judge",
                role="control_judge",
                evidence_pack={"context": "x"},
            )

            self.assertEqual(response.provider, "anthropic")
            self.assertEqual(seen["provider"], "anthropic")
            self.assertEqual(seen["role"], "control_judge")
            self.assertEqual(seen["timeout"], 30.0)

    def test_control_role_rejects_explicit_codex_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={"codex": StaticAdapter("codex", tool_capable=True, responder=echo_response("codex"))},
            )

            with self.assertRaisesRegex(ValueError, "codex is not allowed"):
                router.ask(
                    "decide",
                    lane="judge",
                    role="control_judge",
                    provider="codex",
                    evidence_pack={"context": "x"},
                )

    def test_adapter_timeout_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []

            def timeout(_: LLMRequest) -> LLMResponse:
                raise AdapterError("provider timed out after 30s")

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=timeout)},
                audit_sink=audit_events.append,
            )

            with self.assertRaises(AdapterError):
                router.ask(
                    "decide",
                    lane="judge",
                    role="control_judge",
                    evidence_pack={"context": "x"},
                )

            timeout_events = [event for event in audit_events if event["action"] == "llm_timeout"]
            self.assertEqual(len(timeout_events), 1)
            self.assertEqual(timeout_events[0]["metadata"]["role"], "control_judge")
            self.assertEqual(timeout_events[0]["metadata"]["timeout_seconds"], 30.0)

    def test_subscription_brain_budget_uses_lane_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "subscription"
            seen: dict[str, float] = {}

            def responder(request: LLMRequest) -> LLMResponse:
                seen["max_budget"] = request.max_budget
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )
            router.ask("think", lane="brain", max_budget=0.05)
            self.assertEqual(seen["max_budget"], 1.0)

    def test_api_brain_budget_keeps_requested_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "api_key"
            seen: dict[str, float] = {}

            def responder(request: LLMRequest) -> LLMResponse:
                seen["max_budget"] = request.max_budget
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )
            router.ask("think", lane="brain", max_budget=0.05)
            self.assertEqual(seen["max_budget"], 0.05)

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

    def test_worker_heavy_lane_defaults_to_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "codex": StaticAdapter("codex", tool_capable=True, responder=echo_response("codex")),
                },
            )
            response = router.ask("debug terminal failure", lane="worker_heavy")
            self.assertEqual(response.provider, "codex")
            self.assertEqual(response.model, "gpt-5.5")

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

    def test_fallback_post_hooks_receive_fallback_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            def fallback(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="ok", lane=request.lane,
                    provider="anthropic", model=request.model,
                )

            seen: dict = {}

            def post_hook(req: LLMRequest, response: LLMResponse) -> LLMResponse:
                seen["provider"] = req.provider
                seen["model"] = req.model
                return response

            router = LLMRouter(
                config=config,
                adapters={
                    "openai": StaticAdapter("openai", tool_capable=False, responder=failing),
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=fallback),
                },
            )
            router.post_hooks.append(post_hook)
            router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})

            self.assertEqual(seen["provider"], "anthropic")

    def test_fallback_audit_uses_fallback_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            def fallback(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="ok", lane=request.lane,
                    provider="anthropic", model=request.model,
                )

            audit_log: list[dict] = []
            router = LLMRouter(
                config=config,
                adapters={
                    "openai": StaticAdapter("openai", tool_capable=False, responder=failing),
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=fallback),
                },
                audit_sink=audit_log.append,
            )
            router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})

            fallback_events = [e for e in audit_log if e["action"] == "llm_fallback"]
            self.assertEqual(len(fallback_events), 1)
            event = fallback_events[0]
            self.assertEqual(event["provider"], "anthropic")
            self.assertEqual(event["metadata"]["fallback_provider"], "anthropic")

    def test_fallback_post_hooks_symmetric(self) -> None:
        for primary, fallback_provider in (("openai", "anthropic"), ("anthropic", "openai")):
            with self.subTest(primary=primary, fallback=fallback_provider):
                with tempfile.TemporaryDirectory() as tmpdir:
                    config = make_config(Path(tmpdir))

                    def failing(_: LLMRequest) -> LLMResponse:
                        raise AdapterUnavailableError("provider unavailable")

                    def fallback(request: LLMRequest, fb=fallback_provider) -> LLMResponse:
                        return LLMResponse(
                            content="ok", lane=request.lane,
                            provider=fb, model=request.model,
                        )

                    seen: dict = {}

                    def post_hook(req: LLMRequest, response: LLMResponse) -> LLMResponse:
                        seen["provider"] = req.provider
                        return response

                    router = LLMRouter(
                        config=config,
                        adapters={
                            primary: StaticAdapter(primary, tool_capable=False, responder=failing),
                            fallback_provider: StaticAdapter(fallback_provider, tool_capable=True, responder=fallback),
                        },
                    )
                    router.post_hooks.append(post_hook)
                    router.ask("verify", lane="verifier", evidence_pack={"diff": "x"}, provider=primary)

                    self.assertEqual(seen["provider"], fallback_provider)

    def test_provider_override_uses_compatible_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            seen: dict[str, str] = {}

            def responder(request: LLMRequest) -> LLMResponse:
                seen["model"] = request.model
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )
            response = router.ask("verify", lane="verifier", provider="anthropic", evidence_pack={"diff": "x"})

            self.assertEqual(response.provider, "anthropic")
            self.assertTrue(seen["model"].startswith("claude-"))

    def test_rejects_known_incompatible_provider_model_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic"))},
            )

            with self.assertRaisesRegex(ValueError, "Anthropic provider cannot serve OpenAI model"):
                router.ask("verify", lane="verifier", provider="anthropic", model="gpt-5.5", evidence_pack={"diff": "x"})

    def test_suppresses_corrupt_internal_tool_trace_from_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            def corrupt(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content='to=multi_tool_use.parallel {"tool_uses":[]}',
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    confidence=0.8,
                )

            audit_log: list[dict] = []
            router = LLMRouter(
                config=config,
                adapters={
                    "openai": StaticAdapter("openai", tool_capable=False, responder=failing),
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=corrupt),
                },
                audit_sink=audit_log.append,
            )

            response = router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})

            self.assertEqual(response.confidence, 0.0)
            self.assertTrue(response.artifacts["internal_tool_trace_suppressed"])
            self.assertNotIn("multi_tool_use.parallel", response.content)
            fallback_event = [event for event in audit_log if event["action"] == "llm_fallback"][0]
            self.assertNotIn("multi_tool_use.parallel", fallback_event["metadata"]["response_text"])

    def test_pre_hook_blocked_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def block(_request: LLMRequest) -> None:
                return None

            block.__name__ = "test_block"

            audit_log: list[dict] = []
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                },
                audit_sink=audit_log.append,
            )
            router.pre_hooks.append(block)
            response = router.ask("hola", lane="brain")

            self.assertEqual(response.provider, "none")
            blocked_events = [e for e in audit_log if e["action"] == "llm_pre_hook_blocked"]
            self.assertEqual(len(blocked_events), 1)
            self.assertEqual(blocked_events[0]["metadata"]["blocked_by"], "test_block")

    def test_router_uses_provider_after_pre_hook_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def primary(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="from anthropic",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            def secondary(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="from openai",
                    lane=request.lane,
                    provider="openai",
                    model=request.model,
                )

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=primary),
                    "openai": StaticAdapter("openai", tool_capable=True, responder=secondary),
                },
            )

            def reroute_to_openai(request: LLMRequest) -> LLMRequest:
                request.provider = "openai"
                request.model = "gpt-5.5"
                return request

            router.pre_hooks.append(reroute_to_openai)
            response = router.ask("hi", lane="brain", provider="anthropic")

            self.assertEqual(response.provider, "openai")
            self.assertEqual(response.content, "from openai")

    def test_observation_window_receives_llm_audit_events_and_trips_cost_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            window = ObservationWindowState(
                state_path=Path(tmpdir) / "window.json",
                config=ObservationWindowConfig(cost_per_hour_threshold=0.005),
            )
            audit_log: list[dict] = []
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                },
                audit_sink=audit_log.append,
                observation_window=window,
            )

            router.ask("hola", lane="brain")

            self.assertEqual(audit_log[-1]["action"], "llm_response")
            self.assertTrue(window.frozen)
            self.assertEqual(window.freeze_reason, "circuit_breaker:cost_per_hour")


class DelegationHandlerThreadingTests(unittest.TestCase):
    def test_brain_lane_threads_delegation_handler_to_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            recorded: dict[str, object] = {}

            def responder(request: LLMRequest) -> LLMResponse:
                recorded["delegation_handler"] = request.delegation_handler
                return echo_response("anthropic")(request)

            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=responder)},
            )

            def handler(payload: dict) -> dict:
                return {"ok": True, "ack": "started"}

            router.ask("hazlo", lane="brain", delegation_handler=handler)
            self.assertIs(recorded["delegation_handler"], handler)

    def test_advisory_lane_rejects_delegation_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic"))},
            )
            with self.assertRaises(ValueError):
                router.ask(
                    "verify",
                    lane="verifier",
                    evidence_pack={"diff": "x"},
                    delegation_handler=lambda payload: {"ack": "no"},
                )

    def test_fallback_request_preserves_closure_delegation_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            recorded: dict[str, object] = {}

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            def fallback(request: LLMRequest) -> LLMResponse:
                recorded["delegation_handler"] = request.delegation_handler
                return echo_response("openai")(request)

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=failing),
                    "openai": StaticAdapter("openai", tool_capable=True, responder=fallback),
                },
            )

            def handler(payload: dict) -> dict:
                return {"ok": True, "ack": "started"}

            response = router.ask("hazlo", lane="brain", delegation_handler=handler)
            self.assertEqual(response.provider, "openai")
            handed = recorded["delegation_handler"]
            self.assertTrue(callable(handed))
            self.assertEqual(handed({"objective": "x"}), {"ok": True, "ack": "started"})


if __name__ == "__main__":
    unittest.main()
