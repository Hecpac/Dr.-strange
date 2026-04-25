from __future__ import annotations

import unittest

from claw_v2.model_registry import ModelRegistry, normalize_model_lane, parse_model_selector


class ModelRegistryTests(unittest.TestCase):
    def test_codex_provider_is_marked_as_chatgpt_subscription(self) -> None:
        registry = ModelRegistry.default()

        ref = registry.resolve("codex:gpt-5.5")

        self.assertEqual(ref.provider, "codex")
        self.assertEqual(ref.model, "gpt-5.5")
        self.assertEqual(ref.billing, "chatgpt_subscription")

    def test_openai_provider_is_marked_as_api_billing(self) -> None:
        registry = ModelRegistry.default()

        ref = registry.resolve("openai:gpt-5.5")

        self.assertEqual(ref.provider, "openai")
        self.assertEqual(ref.billing, "api")

    def test_subscription_alias_routes_to_codex_not_openai(self) -> None:
        self.assertEqual(parse_model_selector("subscription:gpt-5.5"), ("codex", "gpt-5.5"))
        self.assertEqual(parse_model_selector("chatgpt:gpt-5.5"), ("codex", "gpt-5.5"))

    def test_bare_gpt_model_infers_openai_api(self) -> None:
        self.assertEqual(parse_model_selector("gpt-5.5"), ("openai", "gpt-5.5"))

    def test_coding_lane_alias_maps_to_worker(self) -> None:
        self.assertEqual(normalize_model_lane("coding"), "worker")


if __name__ == "__main__":
    unittest.main()
