from __future__ import annotations

import unittest

from claw_v2.dispatch import (
    Route,
    RouteContext,
    RouteOutcome,
    dispatch_routes,
)


def _ctx(text: str = "hola") -> RouteContext:
    return RouteContext(
        user_id="u1",
        session_id="s1",
        text=text,
        stripped=text.strip(),
    )


class RouteOutcomeFactoryTests(unittest.TestCase):
    def test_fall_through_carries_reason(self) -> None:
        outcome = RouteOutcome.fall_through(reason="no_match")
        self.assertEqual(outcome.route, "fall_through")
        self.assertIsNone(outcome.response)
        self.assertFalse(outcome.captured)
        self.assertEqual(outcome.reason, "no_match")

    def test_intercepted_marks_captured(self) -> None:
        outcome = RouteOutcome.intercepted("hi", reason="hello_handler")
        self.assertEqual(outcome.route, "intercepted")
        self.assertEqual(outcome.response, "hi")
        self.assertTrue(outcome.captured)

    def test_intercepted_extra_defaults_to_empty(self) -> None:
        outcome = RouteOutcome.intercepted("ok")
        self.assertEqual(outcome.extra, {})

    def test_intercepted_extra_passes_through(self) -> None:
        outcome = RouteOutcome.intercepted("ok", extra={"matched_pattern": "greeting"})
        self.assertEqual(outcome.extra["matched_pattern"], "greeting")

    def test_intercepted_default_store_memory_limit(self) -> None:
        outcome = RouteOutcome.intercepted("ok")
        self.assertEqual(outcome.store_memory_limit, 2000)

    def test_intercepted_custom_store_memory_limit(self) -> None:
        outcome = RouteOutcome.intercepted("ok", store_memory_limit=3000)
        self.assertEqual(outcome.store_memory_limit, 3000)


class DispatchRoutesTests(unittest.TestCase):
    def test_first_intercepting_route_wins(self) -> None:
        first = Route("first", lambda ctx: RouteOutcome.intercepted("first wins"))
        second = Route("second", lambda ctx: RouteOutcome.intercepted("second wins"))
        outcome = dispatch_routes([first, second], _ctx())
        self.assertEqual(outcome.response, "first wins")

    def test_fall_through_continues_to_next(self) -> None:
        first = Route("first", lambda ctx: RouteOutcome.fall_through("not mine"))
        second = Route("second", lambda ctx: RouteOutcome.intercepted("second wins"))
        outcome = dispatch_routes([first, second], _ctx())
        self.assertEqual(outcome.response, "second wins")

    def test_all_routes_fall_through_returns_fall_through(self) -> None:
        first = Route("first", lambda ctx: RouteOutcome.fall_through("a"))
        second = Route("second", lambda ctx: RouteOutcome.fall_through("b"))
        outcome = dispatch_routes([first, second], _ctx())
        self.assertEqual(outcome.route, "fall_through")
        self.assertEqual(outcome.reason, "no_route_matched")
        self.assertIsNone(outcome.response)

    def test_empty_route_list_returns_fall_through(self) -> None:
        outcome = dispatch_routes([], _ctx())
        self.assertEqual(outcome.route, "fall_through")
        self.assertEqual(outcome.reason, "no_route_matched")

    def test_on_decision_called_for_every_visited_route(self) -> None:
        decisions: list[tuple[str, str]] = []

        def record(name: str, outcome: RouteOutcome, ctx: RouteContext) -> None:
            decisions.append((name, outcome.route))

        first = Route("first", lambda ctx: RouteOutcome.fall_through("x"))
        second = Route("second", lambda ctx: RouteOutcome.intercepted("ok"))
        third = Route("third", lambda ctx: RouteOutcome.intercepted("never"))

        dispatch_routes([first, second, third], _ctx(), on_decision=record)

        # third must NOT be visited — second already intercepted.
        self.assertEqual(decisions, [("first", "fall_through"), ("second", "intercepted")])

    def test_on_decision_receives_context(self) -> None:
        seen_text: list[str] = []

        def record(name: str, outcome: RouteOutcome, ctx: RouteContext) -> None:
            seen_text.append(ctx.text)

        only = Route("only", lambda ctx: RouteOutcome.fall_through())
        dispatch_routes([only], _ctx(text="payload"), on_decision=record)
        self.assertEqual(seen_text, ["payload"])

    def test_handler_can_use_runtime_channel_and_metadata(self) -> None:
        captured: dict[str, object] = {}

        def handler(ctx: RouteContext) -> RouteOutcome:
            captured["channel"] = ctx.runtime_channel
            captured["meta"] = ctx.metadata
            return RouteOutcome.fall_through()

        ctx = RouteContext(
            user_id="u",
            session_id="s",
            text="t",
            stripped="t",
            runtime_channel="telegram",
            metadata={"reply_to": 42},
        )
        dispatch_routes([Route("only", handler)], ctx)
        self.assertEqual(captured["channel"], "telegram")
        self.assertEqual(captured["meta"], {"reply_to": 42})


if __name__ == "__main__":
    unittest.main()
