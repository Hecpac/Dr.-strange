from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.approval import APPROVAL_TTL_SECONDS
from claw_v2.config import AppConfig, ProviderRolePolicyError
from claw_v2.sandbox import SandboxPolicy, sandbox_hook


class AppConfigDefaultsTests(unittest.TestCase):
    def test_workspace_root_defaults_to_current_working_directory(self) -> None:
        home = str(Path.home())
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"HOME": home}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.workspace_root, Path(tmpdir).resolve())

    def test_default_allowed_read_paths_scoped_to_claw_not_home(self) -> None:
        # 2026-05-31 audit (H2): the default read-root is ~/.claw (+ /private/tmp),
        # NOT all of $HOME. Agent state under ~/.claw and the workspace stay
        # readable; arbitrary private HOME files (Documents, Library, .aws,
        # browser stores) do not. Work dirs are opted in separately via
        # EXTRA_WORKSPACE_ROOTS, which this default change does not touch.
        home = Path.home()
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertIn(home / ".claw", config.allowed_read_paths)
        self.assertNotIn(home, config.allowed_read_paths)
        policy = SandboxPolicy(
            workspace_root=config.workspace_root,
            allowed_paths=config.allowed_read_paths,
            writable_paths=[config.workspace_root],
        )
        # ~/.claw agent state -> allowed
        self.assertTrue(
            sandbox_hook("Read", {"file_path": str(home / ".claw" / "state.json")}, policy=policy).allowed
        )
        # workspace_root -> allowed
        self.assertTrue(
            sandbox_hook("Read", {"file_path": str(config.workspace_root / "README.md")}, policy=policy).allowed
        )
        # arbitrary private HOME file -> blocked
        self.assertFalse(
            sandbox_hook("Read", {"file_path": str(home / "Documents" / "private.txt")}, policy=policy).allowed
        )

    def test_allowed_read_paths_env_override_intact(self) -> None:
        # The ALLOWED_READ_PATHS env override still fully replaces the default,
        # so operators can opt back into broader read roots explicitly.
        home = Path.home()
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(
                    os.environ,
                    {"HOME": str(home), "ALLOWED_READ_PATHS": str(home / "Documents")},
                    clear=True,
                ):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.allowed_read_paths, [home / "Documents"])

    def test_browse_backend_defaults_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.browse_backend, "auto")

                with patch.dict(os.environ, {"BROWSE_BACKEND": "playwright_local"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.browse_backend, "playwright_local")

                with patch.dict(os.environ, {"BROWSE_BACKEND": "browserbase_cdp"}, clear=True):
                    browserbase_configured = AppConfig.from_env()
                self.assertEqual(browserbase_configured.browse_backend, "browserbase_cdp")
            finally:
                os.chdir(previous_cwd)

    def test_sandbox_capability_profile_defaults_to_engineer_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.sandbox_capability_profile, "engineer")

                with patch.dict(os.environ, {"SANDBOX_CAPABILITY_PROFILE": "surgical"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.sandbox_capability_profile, "surgical")
            finally:
                os.chdir(previous_cwd)

    def test_sdk_bypass_permissions_defaults_to_enabled_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertTrue(config.sdk_bypass_permissions)

                with patch.dict(os.environ, {"SDK_BYPASS_PERMISSIONS": "false"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertFalse(configured.sdk_bypass_permissions)
            finally:
                os.chdir(previous_cwd)

    def test_brain_tooluse_verify_defaults_off_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertFalse(config.brain_tooluse_verify)

                with patch.dict(os.environ, {"BRAIN_TOOLUSE_VERIFY": "true"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertTrue(configured.brain_tooluse_verify)
            finally:
                os.chdir(previous_cwd)

    def test_approval_ttl_defaults_to_900_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.approval_ttl_seconds, APPROVAL_TTL_SECONDS)

                with patch.dict(os.environ, {"APPROVAL_TTL_SECONDS": "120"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.approval_ttl_seconds, 120)
            finally:
                os.chdir(previous_cwd)

    def test_approval_ttl_validation_rejects_non_positive_values(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"APPROVAL_TTL_SECONDS": "0"}, clear=True):
                    config = AppConfig.from_env()
                with self.assertRaises(ValueError):
                    config.validate()
            finally:
                os.chdir(previous_cwd)

    def test_hardening_config_surface_defaults_and_overrides(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertFalse(config.enable_trivial_automerge)
                self.assertEqual(config.token_window_seconds, 18_000)
                self.assertEqual(config.token_window_cap, 1_000_000)
                self.assertEqual(config.token_soft_limit_ratio, 0.8)
                self.assertEqual(config.token_hard_limit_ratio, 1.0)
                self.assertEqual(config.command_isolation_mode, "docker_ephemeral")

                with patch.dict(
                    os.environ,
                    {
                        "CLAW_ENABLE_TRIVIAL_AUTOMERGE": "true",
                        "CLAW_TOKEN_WINDOW_SECONDS": "9000",
                        "CLAW_TOKEN_WINDOW_CAP": "12345",
                        "CLAW_TOKEN_SOFT_LIMIT_RATIO": "0.7",
                        "CLAW_TOKEN_HARD_LIMIT_RATIO": "0.9",
                        "CLAW_COMMAND_ISOLATION_MODE": "host_sanitized",
                    },
                    clear=True,
                ):
                    configured = AppConfig.from_env()
                self.assertTrue(configured.enable_trivial_automerge)
                self.assertEqual(configured.token_window_seconds, 9000)
                self.assertEqual(configured.token_window_cap, 12345)
                self.assertEqual(configured.token_soft_limit_ratio, 0.7)
                self.assertEqual(configured.token_hard_limit_ratio, 0.9)
                self.assertEqual(configured.command_isolation_mode, "host_sanitized")
            finally:
                os.chdir(previous_cwd)

    def test_notebooklm_backend_defaults_to_cdp_and_accepts_jacob_adapter(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.notebooklm_backend, "cdp")
                self.assertEqual(config.notebooklm_cli_path, "nlm")

                with patch.dict(
                    os.environ,
                    {
                        "NOTEBOOKLM_BACKEND": "jacob",
                        "NOTEBOOKLM_CLI_PATH": "/opt/bin/nlm",
                        "NOTEBOOKLM_CLI_PROFILE": "work",
                        "NOTEBOOKLM_CLI_TIMEOUT_SECONDS": "30",
                        "NOTEBOOKLM_CLI_LONG_TIMEOUT_SECONDS": "900",
                    },
                    clear=True,
                ):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.notebooklm_backend, "jacob")
                self.assertEqual(configured.notebooklm_cli_path, "/opt/bin/nlm")
                self.assertEqual(configured.notebooklm_cli_profile, "work")
                self.assertEqual(configured.notebooklm_cli_timeout_seconds, 30)
                self.assertEqual(configured.notebooklm_cli_long_timeout_seconds, 900)

                with patch.dict(os.environ, {"NOTEBOOKLM_BACKEND": "local"}, clear=True):
                    local_alias = AppConfig.from_env()
                self.assertEqual(local_alias.notebooklm_backend, "cdp")
            finally:
                os.chdir(previous_cwd)

    def test_notebooklm_backend_validation(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"NOTEBOOKLM_BACKEND": "mcp"}, clear=True):
                    config = AppConfig.from_env()
                with self.assertRaises(ValueError):
                    config.validate()
            finally:
                os.chdir(previous_cwd)

    def test_hardening_config_surface_validation(self) -> None:
        invalid_envs = (
            {"CLAW_TOKEN_WINDOW_SECONDS": "0"},
            {"CLAW_TOKEN_WINDOW_CAP": "0"},
            {"CLAW_TOKEN_SOFT_LIMIT_RATIO": "1.1", "CLAW_TOKEN_HARD_LIMIT_RATIO": "1.0"},
            {"CLAW_COMMAND_ISOLATION_MODE": "none"},
        )
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                for env in invalid_envs:
                    with self.subTest(env=env):
                        with patch.dict(os.environ, env, clear=True):
                            config = AppConfig.from_env()
                        with self.assertRaises(ValueError):
                            config.validate()
            finally:
                os.chdir(previous_cwd)

    def test_morning_brief_configuration_loads_from_env(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "MORNING_BRIEF_ENABLED": "true",
                        "MORNING_BRIEF_HOUR": "7",
                        "MORNING_BRIEF_TIMEZONE": "America/Chicago",
                        "MORNING_BRIEF_LOCATION": "Dallas, TX",
                        "MORNING_BRIEF_EMAIL_COMMAND": "email-digest",
                        "MORNING_BRIEF_CALENDAR_COMMAND": "calendar-digest",
                        "EVENING_BRIEF_ENABLED": "true",
                        "EVENING_BRIEF_HOUR": "21",
                    },
                    clear=True,
                ):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(config.morning_brief_enabled)
        self.assertEqual(config.morning_brief_hour, 7)
        self.assertEqual(config.morning_brief_timezone, "America/Chicago")
        self.assertEqual(config.morning_brief_weather_location, "Dallas, TX")
        self.assertEqual(config.morning_brief_email_command, "email-digest")
        self.assertEqual(config.morning_brief_calendar_command, "calendar-digest")
        self.assertTrue(config.evening_brief_enabled)
        self.assertEqual(config.evening_brief_hour, 21)

    def test_morning_and_evening_brief_default_hours(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.morning_brief_hour, 5)
        self.assertEqual(config.evening_brief_hour, 21)

    def test_benchmark_informed_lane_defaults(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.provider_for_lane("brain"), "anthropic")
        self.assertEqual(config.model_for_lane("brain"), "claude-opus-4-7")
        self.assertEqual(config.provider_for_lane("worker"), "anthropic")
        self.assertEqual(config.model_for_lane("worker"), "claude-sonnet-4-6")
        self.assertEqual(config.provider_for_lane("worker_heavy"), "codex")
        self.assertEqual(config.model_for_lane("worker_heavy"), "gpt-5.5")
        self.assertEqual(config.effort_for_lane("worker_heavy"), "high")
        self.assertEqual(config.provider_for_lane("research"), "codex")
        self.assertEqual(config.model_for_lane("research"), "gpt-5.5")
        self.assertEqual(config.provider_for_lane("judge"), "codex")
        self.assertEqual(config.model_for_lane("judge"), "gpt-5.5")
        self.assertEqual(config.provider_for_lane("verifier"), "codex")
        self.assertEqual(config.claw_worker_summary_limit, 16_000)
        self.assertEqual(config.claw_phase_input_limit, 48_000)

    def test_coordinator_context_limits_accept_env_overrides(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "CLAW_WORKER_SUMMARY_LIMIT": "4000",
                        "CLAW_PHASE_INPUT_LIMIT": "12000",
                    },
                    clear=True,
                ):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.claw_worker_summary_limit, 4_000)
        self.assertEqual(config.claw_phase_input_limit, 12_000)

    def test_lane_distribution_env_overrides_still_win(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "RESEARCH_PROVIDER": "google",
                        "RESEARCH_MODEL": "gemini-2.5-pro",
                        "JUDGE_PROVIDER": "anthropic",
                        "JUDGE_MODEL": "claude-sonnet-4-6",
                        "WORKER_HEAVY_PROVIDER": "anthropic",
                        "WORKER_HEAVY_MODEL": "claude-opus-4-7",
                    },
                    clear=True,
                ):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.provider_for_lane("research"), "google")
        self.assertEqual(config.model_for_lane("research"), "gemini-2.5-pro")
        self.assertEqual(config.provider_for_lane("judge"), "anthropic")
        self.assertEqual(config.model_for_lane("judge"), "claude-sonnet-4-6")
        self.assertEqual(config.provider_for_lane("worker_heavy"), "anthropic")
        self.assertEqual(config.model_for_lane("worker_heavy"), "claude-opus-4-7")

    def test_provider_role_defaults_keep_codex_out_of_control_path(self) -> None:
        home = Path.home()
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.provider_for_lane("judge"), "codex")
        self.assertEqual(config.provider_for_role("control_judge"), "anthropic")
        self.assertEqual(config.provider_for_role("control_verifier"), "anthropic")
        self.assertEqual(config.provider_for_role("critical_verifier"), "anthropic")
        self.assertEqual(config.provider_for_role("heavy_coding"), "codex")
        self.assertEqual(config.timeout_for_role("control_judge"), 30.0)
        self.assertEqual(config.timeout_for_role("coordinator_research"), 90.0)
        self.assertEqual(config.timeout_for_role("coordinator_verification"), 60.0)
        self.assertEqual(config.timeout_for_role("coordinator_implementation"), 180.0)

    def test_control_role_policy_rejects_codex_and_slow_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))

        with self.assertRaisesRegex(ProviderRolePolicyError, "codex"):
            config.validate_provider_role_policy("control_judge", "codex", timeout=30.0)
        with self.assertRaisesRegex(ProviderRolePolicyError, "<= 30s"):
            config.validate_provider_role_policy("critical_verifier", "anthropic", timeout=31.0)

    def test_coordinator_verification_ignores_verifier_model_without_verifier_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.brain_provider = "anthropic"
            config.verifier_provider = None
            config.verifier_model = "gpt-5.4-mini"

            self.assertEqual(config.provider_for_role("coordinator_verification"), "anthropic")
            self.assertNotEqual(config.model_for_role("coordinator_verification"), "gpt-5.4-mini")
            self.assertTrue(config.model_for_role("coordinator_verification").startswith("claude-"))
            config.validate()

    def test_coordinator_verification_uses_verifier_model_with_verifier_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.verifier_provider = "openai"
            config.verifier_model = "gpt-5.4-mini"

            self.assertEqual(config.provider_for_role("coordinator_verification"), "openai")
            self.assertEqual(config.model_for_role("coordinator_verification"), "gpt-5.4-mini")
            config.validate()

    def test_billing_modes_separate_subscription_from_api_costs(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"CLAUDE_AUTH_MODE": "subscription"}, clear=True):
                    subscription = AppConfig.from_env()
                with patch.dict(os.environ, {"CLAUDE_AUTH_MODE": "api_key"}, clear=True):
                    api_key = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(subscription.provider_billing_mode("anthropic"), "subscription")
        self.assertEqual(subscription.provider_billing_mode("codex"), "subscription")
        self.assertNotIn("anthropic", subscription.billable_cost_providers())
        self.assertIn("anthropic", subscription.notional_cost_providers())
        self.assertEqual(api_key.provider_billing_mode("anthropic"), "api")
        self.assertIn("anthropic", api_key.billable_cost_providers())

    def test_subscription_budget_floor_prevents_tiny_brain_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "subscription"
            self.assertEqual(
                config.effective_max_budget_for_request(
                    lane="brain",
                    provider="anthropic",
                    requested_budget=0.05,
                ),
                1.0,
            )
            self.assertEqual(
                config.effective_max_budget_for_request(
                    lane="brain",
                    provider="anthropic",
                    requested_budget=2.0,
                ),
                2.0,
            )

    def test_api_budget_caps_are_not_raised_by_subscription_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "api_key"
            self.assertEqual(
                config.effective_max_budget_for_request(
                    lane="brain",
                    provider="anthropic",
                    requested_budget=0.05,
                ),
                0.05,
            )

    def test_invalid_morning_brief_timezone_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.morning_brief_timezone = "Mars/Olympus"
            with self.assertRaisesRegex(ValueError, "morning_brief_timezone"):
                config.validate()

    def test_invalid_evening_brief_hour_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.evening_brief_hour = 24
            with self.assertRaisesRegex(ValueError, "evening_brief_hour"):
                config.validate()

    def test_runtime_config_path_loads_monitored_sites_and_sub_agent_jobs(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                runtime_config = Path(tmpdir) / "runtime.yml"
                runtime_config.write_text(
                    "monitored_sites:\n"
                    "  - name: status page\n"
                    "    url: https://status.example.com\n"
                    "    interval_seconds: 900\n"
                    "scheduled_sub_agents:\n"
                    "  - agent: alma\n"
                    "    skill: daily-brief\n"
                    "    interval_seconds: 7200\n"
                    "    lane: worker\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"RUNTIME_CONFIG_PATH": str(runtime_config)}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.runtime_config_path, runtime_config)
        self.assertEqual(len(config.monitored_sites), 1)
        self.assertEqual(config.monitored_sites[0].name, "status page")
        self.assertEqual(config.monitored_sites[0].interval_seconds, 900)
        self.assertEqual(len(config.scheduled_sub_agents), 1)
        self.assertEqual(config.scheduled_sub_agents[0].agent, "alma")
        self.assertEqual(config.scheduled_sub_agents[0].skill, "daily-brief")


class CodexConfigTests(unittest.TestCase):
    def test_codex_worker_provider_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            # Should not raise
            config.validate()

    def test_codex_fields_have_defaults_from_env(self) -> None:
        import os
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.codex_model, "codex-mini-latest")
        self.assertEqual(config.computer_use_backend, "openai")
        self.assertFalse(config.computer_use_required)

    def test_computer_use_backend_codex_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.computer_use_backend = "codex"
            config.validate()

    def test_anthropic_advisory_model_does_not_reuse_codex_worker_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            config.research_provider = "anthropic"
            config.research_model = None
            self.assertEqual(config.provider_for_lane("research"), "anthropic")
            self.assertEqual(config.model_for_lane("research"), "claude-sonnet-4-6")

    def test_validate_rejects_incompatible_provider_model_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.verifier_provider = "anthropic"
            config.verifier_model = "gpt-5.5"

            with self.assertRaisesRegex(ValueError, "verifier"):
                config.validate()


class PerLaneThinkingAndEffortTests(unittest.TestCase):
    """Per-skill verification scaffolding: per-lane effort + thinking budget."""

    def _from_env(self, env: dict[str, str]) -> AppConfig:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, env, clear=True):
                    return AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

    def test_thinking_tokens_default_to_zero_for_every_lane(self) -> None:
        config = self._from_env({"HOME": str(Path.home())})
        for lane in ("brain", "worker", "worker_heavy", "verifier", "research", "judge"):
            self.assertEqual(config.thinking_tokens_for_lane(lane), 0, lane)

    def test_thinking_tokens_env_overrides_per_lane(self) -> None:
        config = self._from_env({
            "HOME": str(Path.home()),
            "BRAIN_THINKING_TOKENS": "8000",
            "WORKER_HEAVY_THINKING_TOKENS": "6000",
            "VERIFIER_THINKING_TOKENS": "4000",
        })
        self.assertEqual(config.thinking_tokens_for_lane("brain"), 8000)
        self.assertEqual(config.thinking_tokens_for_lane("worker_heavy"), 6000)
        self.assertEqual(config.thinking_tokens_for_lane("verifier"), 4000)
        self.assertEqual(config.thinking_tokens_for_lane("worker"), 0)
        self.assertEqual(config.thinking_tokens_for_lane("research"), 0)
        self.assertEqual(config.thinking_tokens_for_lane("judge"), 0)

    def test_verifier_and_research_effort_fall_back_to_judge_effort(self) -> None:
        config = self._from_env({
            "HOME": str(Path.home()),
            "JUDGE_EFFORT": "high",
        })
        self.assertEqual(config.effort_for_lane("verifier"), "high")
        self.assertEqual(config.effort_for_lane("research"), "high")
        self.assertEqual(config.effort_for_lane("judge"), "high")

    def test_verifier_and_research_effort_take_explicit_overrides(self) -> None:
        config = self._from_env({
            "HOME": str(Path.home()),
            "JUDGE_EFFORT": "medium",
            "VERIFIER_EFFORT": "high",
            "RESEARCH_EFFORT": "low",
        })
        self.assertEqual(config.effort_for_lane("verifier"), "high")
        self.assertEqual(config.effort_for_lane("research"), "low")
        self.assertEqual(config.effort_for_lane("judge"), "medium")


class BrowserUseModelConfigTests(unittest.TestCase):
    def test_default_and_env_override(self) -> None:
        home = str(Path.home())
        with patch.dict(os.environ, {"HOME": home}, clear=True):
            config = AppConfig.from_env()
        self.assertEqual(config.computer_browser_use_model, "gpt-5.4")
        with patch.dict(os.environ, {"HOME": home, "CLAW_BROWSER_USE_MODEL": "gpt-5.5"}, clear=True):
            configured = AppConfig.from_env()
        self.assertEqual(configured.computer_browser_use_model, "gpt-5.5")


class BrowserUseTimeoutConfigTests(unittest.TestCase):
    def test_default_and_env_override(self) -> None:
        home = str(Path.home())
        with patch.dict(os.environ, {"HOME": home}, clear=True):
            config = AppConfig.from_env()
        self.assertEqual(config.computer_browser_use_timeout_seconds, 420)
        with patch.dict(os.environ, {"HOME": home, "CLAW_BROWSER_USE_TIMEOUT": "600"}, clear=True):
            configured = AppConfig.from_env()
        self.assertEqual(configured.computer_browser_use_timeout_seconds, 600)

    def test_nonpositive_timeout_rejected(self) -> None:
        home = str(Path.home())
        with patch.dict(os.environ, {"HOME": home, "CLAW_BROWSER_USE_TIMEOUT": "0"}, clear=True):
            with self.assertRaises(ValueError):
                AppConfig.from_env().validate()


if __name__ == "__main__":
    unittest.main()
