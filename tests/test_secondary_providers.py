from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.base import AdapterUnavailableError, LLMRequest, build_effective_input
from claw_v2.adapters.google import GoogleAdapter
from claw_v2.adapters.openai import OpenAIAdapter


def make_request() -> LLMRequest:
    return LLMRequest(
        prompt="Review whether the rollout should proceed.",
        system_prompt=None,
        lane="verifier",
        provider="openai",
        model="gpt-5.4-mini",
        effort="low",
        session_id="prev-123",
        max_budget=0.5,
        evidence_pack={"diff": "print('x')", "tests": "ok"},
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=30.0,
    )


class SecondaryProviderAdapterTests(unittest.TestCase):
    def test_build_effective_input_embeds_evidence_pack_for_advisory_lanes(self) -> None:
        rendered = build_effective_input(make_request())
        self.assertIn("# Evidence Pack", rendered)
        self.assertIn("\"diff\": \"print('x')\"", rendered)
        self.assertIn("# Task", rendered)

    def test_openai_adapter_uses_responses_api(self) -> None:
        recorded: dict[str, object] = {}
        fake_response = SimpleNamespace(
            id="resp_123",
            output_text="proceed with caution",
            usage=SimpleNamespace(input_tokens=12, output_tokens=4),
        )

        class FakeClient:
            def __init__(self, **kwargs):
                recorded["client_kwargs"] = kwargs
                self.responses = SimpleNamespace(create=self.create)

            def create(self, **kwargs):
                recorded["request_kwargs"] = kwargs
                return fake_response

        fake_sdk = SimpleNamespace(OpenAI=FakeClient)
        adapter = OpenAIAdapter(api_key="sk-test")
        with patch("claw_v2.adapters.openai.import_module", return_value=fake_sdk):
            response = adapter.complete(make_request())
        self.assertEqual(response.content, "proceed with caution")
        self.assertEqual(response.artifacts["response_id"], "resp_123")
        self.assertEqual(recorded["client_kwargs"], {"api_key": "sk-test"})
        self.assertEqual(recorded["request_kwargs"]["previous_response_id"], "prev-123")
        self.assertIn("# Evidence Pack", recorded["request_kwargs"]["input"])

    def test_google_adapter_uses_generate_content(self) -> None:
        recorded: dict[str, object] = {}
        fake_response = SimpleNamespace(
            response_id="resp_google_1",
            text="risk is moderate",
            usage_metadata=SimpleNamespace(total_token_count=42),
        )

        class FakeModelAPI:
            def generate_content(self, **kwargs):
                recorded["request_kwargs"] = kwargs
                return fake_response

        class FakeClient:
            def __init__(self, **kwargs):
                recorded["client_kwargs"] = kwargs
                self.models = FakeModelAPI()

        def fake_import(name: str):
            if name == "google.genai":
                return SimpleNamespace(Client=FakeClient)
            if name == "google.genai.types":
                return SimpleNamespace(
                    GenerateContentConfig=lambda **kwargs: {"kind": "config", **kwargs}
                )
            raise ModuleNotFoundError(name)

        request = make_request()
        request.provider = "google"
        request.model = "gemini-2.5-pro"
        adapter = GoogleAdapter(api_key="google-test")
        with patch("claw_v2.adapters.google.import_module", side_effect=fake_import):
            response = adapter.complete(request)
        self.assertEqual(response.content, "risk is moderate")
        self.assertEqual(response.artifacts["response_id"], "resp_google_1")
        self.assertEqual(recorded["client_kwargs"], {"api_key": "google-test"})
        self.assertIn("# Evidence Pack", recorded["request_kwargs"]["contents"])
        self.assertEqual(recorded["request_kwargs"]["config"]["kind"], "config")

    def test_openai_adapter_fails_explicitly_without_sdk(self) -> None:
        adapter = OpenAIAdapter()
        with patch("claw_v2.adapters.openai.import_module", side_effect=ModuleNotFoundError):
            with self.assertRaises(AdapterUnavailableError):
                adapter.complete(make_request())

    def test_google_adapter_fails_explicitly_without_sdk(self) -> None:
        request = make_request()
        request.provider = "google"
        request.model = "gemini-2.5-pro"
        adapter = GoogleAdapter()
        with patch("claw_v2.adapters.google.import_module", side_effect=ModuleNotFoundError):
            with self.assertRaises(AdapterUnavailableError):
                adapter.complete(request)


if __name__ == "__main__":
    unittest.main()
