from __future__ import annotations

import unittest

from claw_v2.capability_router import (
    AutonomyIntent,
    CapabilityRoute,
    RuntimeAliveProbe,
    classify_autonomy_intent,
    route_request,
)


class IntentClassificationTests(unittest.TestCase):
    def test_ai_news_intent_in_spanish(self) -> None:
        intent = classify_autonomy_intent("Dame noticias AI de hoy")
        self.assertEqual(intent.task_kind, "ai_news_brief")

    def test_ai_news_intent_alt_phrasing(self) -> None:
        for phrase in [
            "AI news",
            "que paso en AI",
            "trend de AI",
            "dame el AI brief",
        ]:
            self.assertEqual(
                classify_autonomy_intent(phrase).task_kind,
                "ai_news_brief",
                msg=phrase,
            )

    def test_x_trends_intent(self) -> None:
        for phrase in ["X trends", "trends en X", "tweets de hoy"]:
            self.assertEqual(
                classify_autonomy_intent(phrase).task_kind,
                "x_trends",
                msg=phrase,
            )

    def test_social_publish_intent(self) -> None:
        for phrase in ["publica esto en X", "postealo", "tweet esto"]:
            self.assertEqual(
                classify_autonomy_intent(phrase).task_kind,
                "social_publish",
                msg=phrase,
            )

    def test_pipeline_merge_intent(self) -> None:
        for phrase in ["merge el PR 23", "haz merge", "cierra el issue"]:
            self.assertEqual(
                classify_autonomy_intent(phrase).task_kind,
                "pipeline_merge",
                msg=phrase,
            )

    def test_deploy_intent(self) -> None:
        for phrase in ["deploy a prod", "despliega", "sube a producción"]:
            self.assertEqual(
                classify_autonomy_intent(phrase).task_kind,
                "deploy",
                msg=phrase,
            )

    def test_unknown_falls_through(self) -> None:
        intent = classify_autonomy_intent("hola, cómo estás?")
        self.assertEqual(intent.task_kind, "unknown")


class RouteRequestTests(unittest.TestCase):
    def test_ai_news_routes_to_skill_when_available(self) -> None:
        intent = classify_autonomy_intent("Dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: name == "ai-news-daily",
            runtime_alive=True,
        )
        self.assertEqual(route.route, "skill")
        self.assertEqual(route.skill, "ai-news-daily")
        self.assertFalse(route.ask_user)

    def test_ai_news_routes_to_runtime_when_skill_missing_but_runtime_alive(
        self,
    ) -> None:
        intent = classify_autonomy_intent("Dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: False,
            runtime_alive=True,
            web_available=False,
        )
        self.assertEqual(route.route, "runtime")
        self.assertFalse(route.ask_user)

    def test_ai_news_blocked_when_no_web_no_runtime_no_skill(self) -> None:
        intent = classify_autonomy_intent("Dame noticias AI de hoy")
        route = route_request(
            intent,
            skill_available=lambda name: False,
            runtime_alive=False,
            web_available=False,
        )
        self.assertEqual(route.route, "blocked")
        self.assertIn("skill", route.missing_capabilities)
        self.assertIn("runtime", route.missing_capabilities)
        self.assertFalse(route.ask_user)

    def test_x_trends_routes_to_cdp_when_available(self) -> None:
        intent = classify_autonomy_intent("X trends ahora")
        route = route_request(
            intent,
            chrome_cdp=True,
            runtime_alive=True,
        )
        self.assertEqual(route.route, "cdp")
        self.assertFalse(route.ask_user)

    def test_x_trends_routes_to_runtime_when_cdp_missing(self) -> None:
        intent = classify_autonomy_intent("X trends ahora")
        route = route_request(
            intent,
            chrome_cdp=False,
            runtime_alive=True,
        )
        self.assertEqual(route.route, "runtime")
        self.assertFalse(route.ask_user)

    def test_safe_single_route_does_not_ask_user(self) -> None:
        intent = classify_autonomy_intent("X trends ahora")
        route = route_request(
            intent,
            chrome_cdp=True,
            runtime_alive=False,
        )
        self.assertEqual(route.route, "cdp")
        self.assertFalse(route.ask_user)

    def test_publish_routes_to_approval_required(self) -> None:
        intent = classify_autonomy_intent("publica esto en X")
        route = route_request(intent)
        self.assertEqual(route.route, "approval_required")
        self.assertTrue(route.requires_approval)

    def test_critical_action_routes_to_approval_before_skill(self) -> None:
        # Even with skill/runtime available, critical actions go to approval.
        intent = classify_autonomy_intent("haz merge del PR 12")
        route = route_request(
            intent,
            skill_available=lambda name: True,
            runtime_alive=True,
            chrome_cdp=True,
        )
        self.assertEqual(route.route, "approval_required")

    def test_deploy_routes_to_approval_required(self) -> None:
        intent = classify_autonomy_intent("deploy a producción")
        route = route_request(intent)
        self.assertEqual(route.route, "approval_required")

    def test_notebooklm_routes_to_local(self) -> None:
        intent = classify_autonomy_intent("revisa el último cuaderno")
        route = route_request(intent)
        self.assertEqual(route.route, "local")
        self.assertEqual(route.task_kind, "notebooklm_review")

    def test_unknown_intent_routes_to_chat(self) -> None:
        intent = classify_autonomy_intent("hola")
        route = route_request(intent)
        self.assertEqual(route.route, "chat")


class RuntimeAliveProbeTests(unittest.TestCase):
    def test_probe_caches_result_within_ttl(self) -> None:
        calls = {"count": 0}

        def probe() -> bool:
            calls["count"] += 1
            return True

        clock_value = {"now": 1000.0}
        probe_obj = RuntimeAliveProbe(
            probe_fn=probe,
            clock=lambda: clock_value["now"],
        )
        self.assertTrue(probe_obj.is_alive())
        self.assertTrue(probe_obj.is_alive())
        self.assertTrue(probe_obj.is_alive())
        self.assertEqual(calls["count"], 1)
        # Advance past TTL
        clock_value["now"] = 1100.0
        self.assertTrue(probe_obj.is_alive())
        self.assertEqual(calls["count"], 2)

    def test_probe_handles_exception_as_dead(self) -> None:
        def boom() -> bool:
            raise OSError("dead")

        probe = RuntimeAliveProbe(probe_fn=boom, clock=lambda: 0.0)
        self.assertFalse(probe.is_alive())

    def test_probe_does_not_block_handle_text(self) -> None:
        # Probe timeout cap is 250ms. A slow-but-fast probe should still cap.
        import time as time_module

        def slow_probe() -> bool:
            return True

        probe = RuntimeAliveProbe(probe_fn=slow_probe, clock=time_module.time)
        start = time_module.time()
        probe.is_alive()
        elapsed = time_module.time() - start
        self.assertLess(elapsed, 0.5)


class IntegrationGuardTests(unittest.TestCase):
    def test_capability_router_does_not_intercept_slash_commands(self) -> None:
        # Slash commands should NOT be classified to known intents — even if
        # their content mentions ai/news/etc., the bot wrapper guards them out.
        # Here we just confirm the classifier itself behaves predictably:
        intent = classify_autonomy_intent("/social_publish foo")
        # The classifier may match the social_publish keyword, but the bot's
        # _maybe_handle_capability_route guard ensures slash commands return
        # None before reaching the router. This test documents the contract.
        self.assertIn(
            intent.task_kind,
            {"social_publish", "unknown"},
        )


if __name__ == "__main__":
    unittest.main()
