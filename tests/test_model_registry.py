from __future__ import annotations

import unittest

from claw_v2.model_registry import (
    ModelRegistry,
    normalize_model_lane,
    parse_model_selector,
)


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

    def test_terminal_lane_alias_maps_to_worker_heavy(self) -> None:
        self.assertEqual(normalize_model_lane("terminal"), "worker_heavy")

    def test_computer_use_primary_role_uses_codex_subscription_gpt55(self) -> None:
        registry = ModelRegistry.default()

        role = registry.resolve_role("computer_use_primary")

        self.assertEqual(role.provider, "codex")
        self.assertEqual(role.model, "gpt-5.5")
        self.assertEqual(role.billing, "chatgpt_subscription")
        self.assertTrue(role.tool_capable)

    def test_computer_use_fast_role_uses_codex_subscription_mini(self) -> None:
        registry = ModelRegistry.default()

        role = registry.resolve_role("computer_use_fast")

        self.assertEqual(role.provider, "codex")
        self.assertEqual(role.model, "gpt-5.4-mini")
        self.assertEqual(role.billing, "chatgpt_subscription")

    def test_browser_agent_primary_role_uses_claude_subscription_lane(self) -> None:
        registry = ModelRegistry.default()

        role = registry.resolve_role("browser_agent_primary")

        self.assertEqual(role.provider, "anthropic")
        self.assertEqual(role.model, "claude-sonnet-4-6")
        self.assertEqual(role.billing, "claude_subscription_or_api")

    def test_default_automation_roles_do_not_use_deprecated_codex_model(self) -> None:
        registry = ModelRegistry.default()

        role_models = {role.model for role in registry.list_model_roles()}

        self.assertNotIn("gpt-5.3-codex", role_models)


if __name__ == "__main__":
    unittest.main()
