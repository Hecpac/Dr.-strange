from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .types import Lane


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str | None
    telegram_allowed_user_id: str | None
    openai_api_key: str | None
    google_api_key: str | None
    claude_cli_path: str
    claude_auth_mode: str
    approval_secret: str
    brain_provider: str
    brain_model: str
    worker_provider: str
    worker_model: str
    verifier_provider: str | None
    verifier_model: str | None
    research_provider: str | None
    research_model: str | None
    judge_provider: str | None
    judge_model: str | None
    worker_effort: str
    brain_effort: str
    max_budget_usd: float
    db_path: Path
    heartbeat_interval: int
    daily_token_budget: float
    workspace_root: Path
    agent_state_root: Path
    eval_artifacts_root: Path
    eval_on_self_improve: bool
    use_compaction: bool
    cache_prefix_ttl: int
    approvals_root: Path
    pipeline_repo_root: Path | None
    pipeline_label: str
    pipeline_max_retries: int
    pipeline_state_root: Path
    social_accounts_root: Path
    social_keychain_prefix: str
    allowed_read_paths: list[Path]

    @classmethod
    def from_env(cls) -> "AppConfig":
        home = Path.home()
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_allowed_user_id=os.getenv("TELEGRAM_ALLOWED_USER_ID"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            claude_cli_path=os.getenv("CLAUDE_CLI_PATH", "claude"),
            claude_auth_mode=os.getenv("CLAUDE_AUTH_MODE", "subscription"),
            approval_secret=os.getenv("APPROVAL_SECRET", "local-dev-secret"),
            brain_provider=os.getenv("BRAIN_PROVIDER", "anthropic"),
            brain_model=os.getenv("BRAIN_MODEL", "claude-opus-4-6"),
            worker_provider=os.getenv("WORKER_PROVIDER", "anthropic"),
            worker_model=os.getenv("WORKER_MODEL", "claude-sonnet-4-5"),
            verifier_provider=os.getenv("VERIFIER_PROVIDER"),
            verifier_model=os.getenv("VERIFIER_MODEL"),
            research_provider=os.getenv("RESEARCH_PROVIDER"),
            research_model=os.getenv("RESEARCH_MODEL"),
            judge_provider=os.getenv("JUDGE_PROVIDER"),
            judge_model=os.getenv("JUDGE_MODEL"),
            worker_effort=os.getenv("WORKER_EFFORT", "medium"),
            brain_effort=os.getenv("BRAIN_EFFORT", "high"),
            max_budget_usd=float(os.getenv("MAX_BUDGET_USD", "0.50")),
            db_path=Path(os.getenv("DB_PATH", "data/claw.db")),
            heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", "1800")),
            daily_token_budget=float(os.getenv("DAILY_TOKEN_BUDGET", "10.00")),
            workspace_root=Path(os.getenv("WORKSPACE_ROOT", str(home / "claw_workspace"))),
            agent_state_root=Path(os.getenv("AGENT_STATE_ROOT", str(home / ".claw" / "agents"))),
            eval_artifacts_root=Path(os.getenv("EVAL_ARTIFACTS_ROOT", str(home / ".claw" / "evals"))),
            eval_on_self_improve=_env_bool("EVAL_ON_SELF_IMPROVE", True),
            use_compaction=_env_bool("USE_COMPACTION", True),
            cache_prefix_ttl=int(os.getenv("CACHE_PREFIX_TTL", "3600")),
            approvals_root=Path(os.getenv("APPROVALS_ROOT", str(home / ".claw" / "pending_approvals"))),
            pipeline_repo_root=Path(pr) if (pr := os.getenv("PIPELINE_REPO_ROOT")) else None,
            pipeline_label=os.getenv("PIPELINE_LABEL", "claw-auto"),
            pipeline_max_retries=int(os.getenv("PIPELINE_MAX_RETRIES", "3")),
            pipeline_state_root=Path(os.getenv("PIPELINE_STATE_ROOT", str(home / ".claw" / "pipeline"))),
            social_accounts_root=Path(os.getenv("SOCIAL_ACCOUNTS_ROOT", str(Path(__file__).parent / "agents" / "social" / "accounts"))),
            social_keychain_prefix=os.getenv("SOCIAL_KEYCHAIN_PREFIX", "com.pachano.claw.social"),
            allowed_read_paths=[Path(p) for p in os.getenv("ALLOWED_READ_PATHS", ":".join([str(home / "Projects"), "/private/tmp", str(home / ".claude"), str(home / ".claw")])).split(":")],
        )

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.agent_state_root.mkdir(parents=True, exist_ok=True)
        self.eval_artifacts_root.mkdir(parents=True, exist_ok=True)
        self.approvals_root.mkdir(parents=True, exist_ok=True)
        self.pipeline_state_root.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.brain_provider != "anthropic":
            raise ValueError("brain_provider must be 'anthropic' in the current runtime design.")
        if self.worker_provider != "anthropic":
            raise ValueError("worker_provider must be 'anthropic' in the current runtime design.")
        if self.claude_auth_mode not in {"subscription", "api_key", "auto"}:
            raise ValueError("claude_auth_mode must be one of: subscription, api_key, auto.")
        supported = {"anthropic", "openai", "google"}
        secondary = {
            "verifier_provider": self.verifier_provider,
            "research_provider": self.research_provider,
            "judge_provider": self.judge_provider,
        }
        for field_name, value in secondary.items():
            if value is not None and value not in supported:
                raise ValueError(f"{field_name} must be one of {sorted(supported)}.")

    def provider_for_lane(self, lane: Lane) -> str:
        mapping = {
            "brain": self.brain_provider,
            "worker": self.worker_provider,
            "verifier": self.verifier_provider or self.brain_provider,
            "research": self.research_provider or self.brain_provider,
            "judge": self.judge_provider or self.brain_provider,
        }
        return mapping[lane]

    def model_for_lane(self, lane: Lane) -> str:
        mapping = {
            "brain": self.brain_model,
            "worker": self.worker_model,
            "verifier": self.verifier_model or self.worker_model,
            "research": self.research_model or self.worker_model,
            "judge": self.judge_model or self.worker_model,
        }
        return mapping[lane]

    def effort_for_lane(self, lane: Lane) -> str:
        if lane == "brain":
            return self.brain_effort
        if lane == "worker":
            return self.worker_effort
        return "low"
