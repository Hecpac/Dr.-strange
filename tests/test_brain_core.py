"""Tests for brain.py core: handle_message flow, JSON parser, recommendation normalizer."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.brain import (
    BrainService,
    _first_json_object,
    _normalize_recommendation,
    _normalize_risk_level,
    _strip_trace_tags,
    _try_parse_json_object,
    _validate_schema_keys,
)
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse


# ---------------------------------------------------------------------------
# _first_json_object — the parser we hardened (rfind → raw_decode)
# ---------------------------------------------------------------------------

class FirstJsonObjectTests(unittest.TestCase):
    def test_simple_object(self) -> None:
        result = _first_json_object('{"a": 1}')
        self.assertIsNotNone(result)
        self.assertIn('"a"', result)

    def test_extracts_first_when_multiple(self) -> None:
        # The old rfind bug: '{"rec":"deny"} extra {"rec":"approve"}'
        # rfind would grab from first { to last }, producing '{"rec":"deny"} extra {"rec":"approve"}'
        # raw_decode grabs only the first balanced object.
        result = _first_json_object('{"recommendation":"deny"} noise {"recommendation":"approve"}')
        self.assertIn("deny", result)
        self.assertNotIn("approve", result)

    def test_embedded_in_text(self) -> None:
        result = _first_json_object('Here is my analysis: {"ok": true} and more text')
        self.assertIn('"ok"', result)

    def test_no_json(self) -> None:
        self.assertIsNone(_first_json_object("no json here"))

    def test_nested_braces(self) -> None:
        result = _first_json_object('{"outer": {"inner": 1}}')
        self.assertIn("inner", result)

    def test_empty_string(self) -> None:
        self.assertIsNone(_first_json_object(""))

    def test_malformed_json(self) -> None:
        self.assertIsNone(_first_json_object("{bad json"))


# ---------------------------------------------------------------------------
# _try_parse_json_object — handles markdown fences
# ---------------------------------------------------------------------------

class TryParseJsonObjectTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        result = _try_parse_json_object('{"key": "val"}')
        self.assertEqual(result["key"], "val")

    def test_fenced_json(self) -> None:
        result = _try_parse_json_object('```json\n{"key": "val"}\n```')
        self.assertEqual(result["key"], "val")

    def test_returns_none_for_non_dict(self) -> None:
        self.assertIsNone(_try_parse_json_object("[1, 2, 3]"))

    def test_returns_none_for_garbage(self) -> None:
        self.assertIsNone(_try_parse_json_object("not json at all"))


# ---------------------------------------------------------------------------
# _normalize_recommendation
# ---------------------------------------------------------------------------

class NormalizeRecommendationTests(unittest.TestCase):
    def test_approve_variants(self) -> None:
        for word in ("approve", "approved", "allow", "proceed"):
            self.assertEqual(_normalize_recommendation(word), "approve")

    def test_deny_variants(self) -> None:
        for word in ("deny", "denied", "reject", "block"):
            self.assertEqual(_normalize_recommendation(word), "deny")

    def test_needs_approval_variants(self) -> None:
        for word in ("needs_approval", "needs approval", "review", "manual_review"):
            self.assertEqual(_normalize_recommendation(word), "needs_approval")

    def test_unknown_defaults_to_needs_approval(self) -> None:
        self.assertEqual(_normalize_recommendation("idk"), "needs_approval")
        self.assertEqual(_normalize_recommendation(None), "needs_approval")

    def test_case_insensitive(self) -> None:
        self.assertEqual(_normalize_recommendation("APPROVE"), "approve")
        self.assertEqual(_normalize_recommendation("Deny"), "deny")


class NormalizeRiskLevelTests(unittest.TestCase):
    def test_known_levels(self) -> None:
        for level in ("low", "medium", "high", "critical"):
            self.assertEqual(_normalize_risk_level(level), level)

    def test_unknown_defaults_to_medium(self) -> None:
        self.assertEqual(_normalize_risk_level("whatever"), "medium")
        self.assertEqual(_normalize_risk_level(None), "medium")


# ---------------------------------------------------------------------------
# BrainService.handle_message — basic flow
# ---------------------------------------------------------------------------

class HandleMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.memory = MemoryStore(self.db_path)
        self.router = MagicMock()
        self.brain = BrainService(
            router=self.router,
            memory=self.memory,
            system_prompt="You are Claw.",
        )

    def test_stores_user_and_assistant_messages(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content="Hola Hector",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
        )
        self.brain.handle_message("s1", "Dime algo")
        msgs = self.memory.get_recent_messages("s1")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Hola Hector")

    def test_passes_system_prompt_to_router(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content="ok", lane="brain", provider="anthropic", model="test",
        )
        self.brain.handle_message("s1", "test")
        call_kwargs = self.router.ask.call_args
        self.assertIn("You are Claw.", call_kwargs.kwargs["system_prompt"])
        self.assertIn("<response>", call_kwargs.kwargs["system_prompt"])

    def test_returns_llm_response(self) -> None:
        expected = LLMResponse(
            content="response", lane="brain", provider="anthropic", model="test",
        )
        self.router.ask.return_value = expected
        result = self.brain.handle_message("s1", "input")
        self.assertEqual(result.content, "response")

    def test_links_provider_session_when_returned(self) -> None:
        resp = LLMResponse(
            content="hi", lane="brain", provider="anthropic", model="test",
        )
        resp.artifacts["session_id"] = "sdk-abc-123"
        self.router.ask.return_value = resp
        self.brain.handle_message("s1", "test")
        session = self.memory.get_provider_session("s1", "anthropic")
        self.assertEqual(session, "sdk-abc-123")

    def test_extracts_reasoning_trace_and_stores_visible_response(self) -> None:
        observe = ObserveStream(self.db_path)
        brain = BrainService(
            router=self.router,
            memory=self.memory,
            system_prompt="You are Claw.",
            observe=observe,
        )
        self.router.ask.return_value = LLMResponse(
            content="<trace>Checked context and no blockers.</trace><response>Listo, Hector.</response>",
            lane="brain",
            provider="anthropic",
            model="test",
        )

        result = brain.handle_message("s2", "hazlo")

        self.assertEqual(result.content, "Listo, Hector.")
        self.assertEqual(result.artifacts["reasoning_trace"], "Checked context and no blockers.")
        msgs = self.memory.get_recent_messages("s2")
        self.assertEqual(msgs[-1]["content"], "Listo, Hector.")
        events = observe.recent_events(limit=10)
        self.assertTrue(any(e["event_type"] == "brain_reasoning_trace" for e in events))


# ---------------------------------------------------------------------------
# _strip_trace_tags
# ---------------------------------------------------------------------------

class StripTraceTagsTests(unittest.TestCase):
    def test_removes_trace_tags(self) -> None:
        self.assertEqual(_strip_trace_tags('<trace>reasoning</trace>{"a":1}'), '{"a":1}')

    def test_removes_thinking_tags(self) -> None:
        self.assertEqual(_strip_trace_tags('<thinking>stuff</thinking>{"b":2}'), '{"b":2}')

    def test_removes_response_tags(self) -> None:
        self.assertEqual(_strip_trace_tags('<response>{"c":3}</response>'), '{"c":3}')

    def test_passes_through_plain_json(self) -> None:
        self.assertEqual(_strip_trace_tags('{"d":4}'), '{"d":4}')


# ---------------------------------------------------------------------------
# _validate_schema_keys
# ---------------------------------------------------------------------------

class ValidateSchemaKeysTests(unittest.TestCase):
    def test_valid_data(self) -> None:
        schema = {"required": ["name"], "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}
        self.assertEqual(_validate_schema_keys({"name": "Hector", "age": 30}, schema), [])

    def test_missing_required(self) -> None:
        schema = {"required": ["name"], "properties": {"name": {"type": "string"}}}
        errors = _validate_schema_keys({}, schema)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required", errors[0])

    def test_wrong_type(self) -> None:
        schema = {"properties": {"count": {"type": "integer"}}}
        errors = _validate_schema_keys({"count": "not_int"}, schema)
        self.assertEqual(len(errors), 1)

    def test_no_properties(self) -> None:
        self.assertEqual(_validate_schema_keys({"x": 1}, {}), [])


# ---------------------------------------------------------------------------
# BrainService.handle_structured
# ---------------------------------------------------------------------------

class HandleStructuredTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.memory = MemoryStore(self.db_path)
        self.router = MagicMock()
        self.brain = BrainService(
            router=self.router,
            memory=self.memory,
            system_prompt="You are Claw.",
        )

    def test_parses_clean_json(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content='{"name": "test", "value": 42}',
            lane="brain", provider="anthropic", model="test",
        )
        result = self.brain.handle_structured("s1", "extract data", schema={
            "properties": {"name": {"type": "string"}, "value": {"type": "integer"}},
            "required": ["name"],
        })
        self.assertEqual(result["name"], "test")
        self.assertEqual(result["value"], 42)

    def test_strips_trace_before_parsing(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content='<trace>thinking</trace><response>{"status": "ok"}</response>',
            lane="brain", provider="anthropic", model="test",
        )
        result = self.brain.handle_structured("s1", "check", schema={
            "properties": {"status": {"type": "string"}},
        })
        self.assertEqual(result["status"], "ok")

    def test_retries_on_bad_json(self) -> None:
        self.router.ask.side_effect = [
            LLMResponse(content="not json at all", lane="brain", provider="anthropic", model="test"),
            LLMResponse(content='{"fixed": true}', lane="brain", provider="anthropic", model="test"),
        ]
        result = self.brain.handle_structured("s1", "retry test", schema={
            "properties": {"fixed": {"type": "boolean"}},
        })
        self.assertTrue(result["fixed"])
        self.assertEqual(self.router.ask.call_count, 2)

    def test_returns_raw_after_all_retries_fail(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content="still not json", lane="brain", provider="anthropic", model="test",
        )
        result = self.brain.handle_structured("s1", "fail test", schema={}, max_retries=0)
        self.assertIn("raw", result)

    def test_store_history_false_deletes_messages(self) -> None:
        self.router.ask.return_value = LLMResponse(
            content='{"ok": true}', lane="brain", provider="anthropic", model="test",
        )
        self.brain.handle_structured("s1", "ephemeral", schema={}, store_history=False)
        msgs = self.memory.get_recent_messages("s1")
        self.assertEqual(len(msgs), 0)


if __name__ == "__main__":
    unittest.main()
