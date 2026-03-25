from __future__ import annotations

from pathlib import Path

from claw_v2.config import AppConfig


def make_config(root: Path) -> AppConfig:
    config = AppConfig(
        telegram_bot_token=None,
        telegram_allowed_user_id="123",
        openai_api_key=None,
        google_api_key=None,
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
        allowed_paths=[],
        approvals_root=root / "approvals",
    )
    config.validate()
    config.ensure_directories()
    return config
