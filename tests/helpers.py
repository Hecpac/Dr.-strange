from __future__ import annotations

from pathlib import Path

from claw_v2.config import AppConfig


def make_config(root: Path) -> AppConfig:
    config = AppConfig(
        telegram_bot_token=None,
        telegram_allowed_user_id="123",
        openai_api_key=None,
        google_api_key=None,
        linear_api_key=None,
        claude_cli_path="claude",
        claude_auth_mode="subscription",
        approval_secret="test-secret",
        brain_provider="anthropic",
        brain_model="claude-opus-4-6",
        worker_provider="anthropic",
        worker_model="claude-sonnet-4-5",
        verifier_provider="openai",
        verifier_model="gpt-5.4-mini",
        research_provider="google",
        research_model="gemini-2.5-pro",
        judge_provider="openai",
        judge_model="gpt-5.4-mini",
        worker_effort="medium",
        brain_effort="high",
        max_budget_usd=0.5,
        db_path=root / "claw.db",
        heartbeat_interval=30,
        daily_token_budget=10.0,
        workspace_root=root / "workspace",
        agent_state_root=root / "agents",
        eval_artifacts_root=root / "evals",
        eval_on_self_improve=True,
        use_compaction=False,
        cache_prefix_ttl=3600,
        approvals_root=root / "approvals",
        pipeline_repo_root=None,
        pipeline_label="claw-auto",
        pipeline_max_retries=3,
        pipeline_state_root=root / "pipeline",
        social_accounts_root=root / "social_accounts",
        social_keychain_prefix="com.test.claw.social",
        allowed_read_paths=[root / "projects"],
        extra_workspace_roots=[],
        brain_context_window=1_000_000,
        brain_max_output=128_000,
        worker_context_window=1_000_000,
        worker_max_output=64_000,
        dev_browser_path="dev-browser",
        dev_browser_browsers_path="/tmp/pw-browsers",
        dev_browser_timeout=30,
        sdk_bypass_permissions=False,
        daily_cost_limit=10.0,
    )
    config.validate()
    config.ensure_directories()
    return config
