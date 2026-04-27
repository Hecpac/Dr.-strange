from __future__ import annotations

import unittest

from claw_v2.adapters.base import AdapterError, LLMRequest
from claw_v2.adapters.openai import OpenAIAdapter
from claw_v2.types import LLMResponse


class _RecordingTransport:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_request(session_id: str | None = None) -> LLMRequest:
    return LLMRequest(
        prompt="hola",
        system_prompt="sys",
        lane="brain",
        provider="openai",
        model="gpt-test",
        effort=None,
        session_id=session_id,
        max_budget=1.0,
        evidence_pack={},
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=300.0,
    )


class StalePreviousResponseRetryTests(unittest.TestCase):
    def test_retries_once_without_session_id_on_stale_previous_response(self) -> None:
        ok = LLMResponse(
            content="recovered",
            lane="brain",
            provider="openai",
            model="gpt-test",
            artifacts={"response_id": "resp_new"},
        )
        transport = _RecordingTransport([
            AdapterError("previous_response_not_found: previous_response_id cannot be resolved"),
            ok,
        ])
        adapter = OpenAIAdapter(transport=transport)

        response = adapter.complete(_make_request(session_id="resp_old"))

        self.assertEqual(response.content, "recovered")
        self.assertEqual(response.artifacts["session_recovery"], "previous_response_id_reset")
        self.assertEqual(response.artifacts["stale_session_id"], "resp_old")
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(transport.calls[1].session_id)
        self.assertEqual(transport.calls[1].evidence_pack["session_recovery"], "openai_previous_response_id_reset")

    def test_does_not_retry_when_no_session_id(self) -> None:
        transport = _RecordingTransport([
            AdapterError("previous_response_not_found"),
        ])
        adapter = OpenAIAdapter(transport=transport)

        with self.assertRaises(AdapterError):
            adapter.complete(_make_request(session_id=None))

        self.assertEqual(len(transport.calls), 1)

    def test_unrelated_adapter_error_not_retried(self) -> None:
        transport = _RecordingTransport([
            AdapterError("rate limit exceeded"),
        ])
        adapter = OpenAIAdapter(transport=transport)

        with self.assertRaises(AdapterError):
            adapter.complete(_make_request(session_id="resp_old"))

        self.assertEqual(len(transport.calls), 1)

    def test_retry_failure_propagates(self) -> None:
        transport = _RecordingTransport([
            AdapterError("Invalid 'previous_response_id'"),
            AdapterError("provider down"),
        ])
        adapter = OpenAIAdapter(transport=transport)

        with self.assertRaises(AdapterError):
            adapter.complete(_make_request(session_id="resp_old"))

        self.assertEqual(len(transport.calls), 2)


class StreamInterruptedTests(unittest.TestCase):
    def test_stream_idle_marker_raises_stream_interrupted(self) -> None:
        from claw_v2.adapters.base import StreamInterruptedError
        from claw_v2.adapters.openai import OpenAIAdapter

        def transport(_request: LLMRequest) -> LLMResponse:
            raise StreamInterruptedError(
                "OpenAI stream idle timeout - partial response received",
                partial_output="hello partial",
            )

        adapter = OpenAIAdapter(transport=transport)
        with self.assertRaises(StreamInterruptedError) as ctx:
            adapter.complete(_make_request(session_id=None))

        self.assertEqual(ctx.exception.metadata["reason"], "stream_idle_timeout")
        self.assertEqual(ctx.exception.metadata["retryable"], True)
        self.assertIn("partial", ctx.exception.metadata["partial_output"])


if __name__ == "__main__":
    unittest.main()
