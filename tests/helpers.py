from __future__ import annotations

import hashlib
import re
from pathlib import Path

from claw_v2.config import AppConfig


def make_config(root: Path) -> AppConfig:
    config = AppConfig(
        telegram_bot_token=None,
        telegram_allowed_user_id="123",
        web_chat_enabled=False,
        web_chat_host="127.0.0.1",
        web_chat_port=8765,
        web_chat_token=None,
        openai_api_key=None,
        google_api_key=None,
        linear_api_key=None,
        claude_cli_path="claude",
        claude_auth_mode="subscription",
        approval_secret="test-secret",
        brain_provider="anthropic",
        brain_model="claude-opus-4-7",
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
        judge_effort="medium",
        max_budget_usd=0.5,
        db_path=root / "claw.db",
        heartbeat_interval=30,
        daily_token_budget=10.0,
        workspace_root=root / "workspace",
        agent_state_root=root / "agents",
        agent_definitions_root=root / "agent_definitions",
        eval_artifacts_root=root / "evals",
        eval_on_self_improve=True,
        use_compaction=False,
        cache_prefix_ttl=3600,
        allowed_paths=[],
        approvals_root=root / "approvals",
        pipeline_repo_root=None,
        pipeline_label="claw-auto",
        pipeline_max_retries=3,
        pipeline_state_root=root / "pipeline",
        runtime_config_path=None,
        social_accounts_root=root / "social_accounts",
        social_keychain_prefix="com.test.claw.social",
        allowed_read_paths=[root / "projects"],
        extra_workspace_roots=[],
        monitored_sites=[],
        scheduled_sub_agents=[],
        brain_context_window=1_000_000,
        brain_max_output=128_000,
        worker_context_window=1_000_000,
        worker_max_output=64_000,
        dev_browser_path="dev-browser",
        dev_browser_browsers_path="/tmp/pw-browsers",
        dev_browser_timeout=30,
        browse_backend="auto",
        browserbase_api_key=None,
        browserbase_project_id=None,
        browserbase_api_url="https://api.browserbase.com",
        browserbase_region=None,
        browserbase_keep_alive=False,
        sandbox_capability_profile="engineer",
        sdk_bypass_permissions=False,
        daily_cost_limit=10.0,
        chrome_cdp_enabled=False,
        claw_chrome_port=9250,
        computer_use_enabled=False,
        computer_display_width=1280,
        computer_display_height=800,
        ollama_host="http://localhost:11434",
        sensitive_urls=["ads.google.com", "polymarket.com"],
        codex_cli_path="codex",
        codex_model="codex-mini-latest",
        computer_use_backend="openai",
        edge_enabled=False,
        edge_endpoint=None,
        edge_key_id="core",
        edge_secret=None,
        edge_capabilities=["computer_use", "computer_control", "chrome_cdp", "browser_use"],
    )
    config.validate()
    config.ensure_directories()
    return config


def strict_token_embed(text: str, *, dim: int = 4096) -> list[float]:
    """Test-only token-hashing embedder: cosine ≈ 0 when texts share no literal tokens.

    Each unique token hashes into its own slot in a wide vector. Collisions are rare
    for small test corpora, so single-shared-token overlap is clearly distinguishable
    from no-overlap. Avoids the bag-of-chars false-positives that _simple_embedding
    produces for English text.
    """
    tokens = set(re.findall(r"\w+", text.lower()))
    vec = [0.0] * dim
    for t in tokens:
        idx = int(hashlib.md5(t.encode()).hexdigest()[:8], 16) % dim
        vec[idx] = 1.0
    norm = (sum(x * x for x in vec)) ** 0.5 or 1.0
    return [x / norm for x in vec]
