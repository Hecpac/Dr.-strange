from __future__ import annotations

import logging
import math
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claw_v2.approval import APPROVAL_TTL_SECONDS
from claw_v2.maintenance import maintenance_mode_enabled, no_job_claim_enabled

from .types import Lane, ProviderRole

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_tier(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower().removeprefix("tier_").removeprefix("tier")
    try:
        parsed = int(float(normalized))
    except ValueError:
        return default
    return max(parsed, 1)


def _daily_cost_limit_from_env() -> float | None:
    if "CLAW_BUDGET_CAP_DAILY" in os.environ:
        return _env_float("CLAW_BUDGET_CAP_DAILY", 0.0)
    if "DAILY_COST_LIMIT" in os.environ:
        return _env_float("DAILY_COST_LIMIT", 0.0)
    return None


def _autonomous_maintenance_from_env() -> bool:
    if "CLAW_AUTONOMOUS_MAINTENANCE" in os.environ:
        return _env_bool("CLAW_AUTONOMOUS_MAINTENANCE", True)
    return _env_bool("CLAW_AUTONOMOUS_MAINTENANCE_ENABLED", True)


def _brain_tooluse_verify_timeout_from_env() -> float | None:
    """Optional bound (seconds) for the brain tool-use verifier dispatch.

    - Absent → ``None``: the verifier lane keeps its role-default timeout
      (``coordinator_verification`` ≈ 60s); existing behavior is unchanged.
    - A positive number → that value, enforced as the per-dispatch provider
      timeout in ``verify_brain_tooluse`` (overrides the role default).
    - Invalid / non-positive → ``None`` + a warning: an operator typo fails
      closed to the bounded role default instead of silently unbounding the
      verifier. A timeout always resolves to ``pending`` (never ``succeeded``).
    """
    raw = os.getenv("BRAIN_TOOLUSE_VERIFY_TIMEOUT_SECONDS")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "BRAIN_TOOLUSE_VERIFY_TIMEOUT_SECONDS=%r is not a number; ignoring "
            "(verifier keeps its role-default timeout)",
            raw,
        )
        return None
    if not math.isfinite(value) or value <= 0:
        # nan/inf parse without ValueError and nan/inf <= 0 is False, so guard
        # finiteness explicitly — a non-finite bound would defeat fail-closed.
        logger.warning(
            "BRAIN_TOOLUSE_VERIFY_TIMEOUT_SECONDS=%r is not a positive finite "
            "number; ignoring (verifier keeps its role-default timeout)",
            raw,
        )
        return None
    return value


_SECONDARY_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.4-mini",
    "google": "gemini-2.5-pro",
    "ollama": "gemma4",
    # gpt-5.5 (no -codex suffix) is the generalist variant that works with
    # both ChatGPT auth (free Codex CLI plan) and API auth. The -codex
    # variant is API-only. Operators on API auth can override via
    # JUDGE_MODEL=gpt-5.5-codex.
    "codex": "gpt-5.5",
}

_SUBSCRIPTION_BUDGET_FLOORS: dict[Lane, float] = {
    "brain": 1.00,
    "worker": 0.25,
    "worker_heavy": 0.25,
    "verifier": 0.05,
    "research": 0.10,
    "judge": 0.05,
}

_PROVIDER_ROLE_TIMEOUT_DEFAULTS: dict[ProviderRole, float] = {
    "brain": 300.0,
    "worker": 120.0,
    "heavy_coding": 180.0,
    "research_synthesis": 90.0,
    "control_judge": 30.0,
    "control_verifier": 30.0,
    "critical_verifier": 30.0,
    "coordinator_worker": 120.0,
    "coordinator_research": 90.0,
    "coordinator_verification": 60.0,
    "coordinator_implementation": 180.0,
}
_CONTROL_PATH_PROVIDER_ROLES: frozenset[ProviderRole] = frozenset(
    {"control_judge", "control_verifier", "critical_verifier"}
)
_MAX_CONTROL_PATH_TIMEOUT_SECONDS = 30.0


class ProviderRolePolicyError(ValueError):
    """Raised when a provider role would violate runtime safety policy."""


def _is_anthropic_model(model: str | None) -> bool:
    return bool(model and model.startswith("claude-"))


def _validate_provider_model_pair(provider: str, model: str, *, lane: str) -> None:
    normalized_provider = provider.strip().lower()
    normalized_model = model.strip().lower()
    if normalized_provider == "anthropic" and (
        normalized_model.startswith("gpt-")
        or normalized_model.startswith("o3")
        or normalized_model.startswith("o4")
    ):
        raise ValueError(f"{lane}: anthropic provider cannot serve OpenAI model {model!r}.")
    if normalized_provider in {"openai", "codex"} and normalized_model.startswith("claude-"):
        raise ValueError(f"{lane}: {provider} provider cannot serve Anthropic model {model!r}.")
    if normalized_provider == "google" and not normalized_model.startswith("gemini-"):
        raise ValueError(f"{lane}: google provider cannot serve non-Gemini model {model!r}.")


@dataclass(slots=True)
class MonitoredSiteConfig:
    name: str
    url: str
    interval_seconds: int = 3600


@dataclass(slots=True)
class ScheduledSubAgentConfig:
    agent: str
    skill: str
    interval_seconds: int | None = None
    daily_at: str | None = None  # "HH:MM" 24-hour
    timezone: str | None = None  # e.g., "America/Chicago"
    lane: str = "worker"


def _default_monitored_sites() -> list[MonitoredSiteConfig]:
    return [
        MonitoredSiteConfig(
            name="premiumhome.design", url="https://premiumhome.design", interval_seconds=3600
        ),
        MonitoredSiteConfig(
            name="pachanodesign.com", url="https://www.pachanodesign.com", interval_seconds=3600
        ),
    ]


def _default_scheduled_sub_agents() -> list[ScheduledSubAgentConfig]:
    return [
        ScheduledSubAgentConfig(
            agent="alma", skill="daily-brief", interval_seconds=86400, lane="worker"
        ),
        ScheduledSubAgentConfig(
            agent="alma",
            skill="ai-news-daily",
            daily_at="08:00",
            timezone="America/Chicago",
            lane="worker",
        ),
        ScheduledSubAgentConfig(
            agent="alma", skill="content-radar", interval_seconds=43200, lane="worker"
        ),
        ScheduledSubAgentConfig(
            agent="hex", skill="bug-triage", interval_seconds=86400, lane="worker"
        ),
        ScheduledSubAgentConfig(
            agent="rook", skill="health-audit", interval_seconds=21600, lane="worker"
        ),
        ScheduledSubAgentConfig(
            agent="echo",
            skill="engagement-audit",
            daily_at="09:00",
            timezone="America/Chicago",
            lane="worker",
        ),
    ]


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _coerce_yaml_scalar(raw: str) -> str | int | bool:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_yaml_mapping_item(raw: str) -> tuple[str, str | int | bool]:
    if ":" not in raw:
        raise ValueError(f"invalid runtime config entry: {raw}")
    key, value = raw.split(":", 1)
    return key.strip(), _coerce_yaml_scalar(value)


def _parse_runtime_config_yaml_subset(text: str) -> dict[str, object]:
    """Parse the small YAML subset used by runtime scheduling config.

    Supported shape:
    monitored_sites:
      - name: foo
        url: https://example.com
        interval_seconds: 3600
    scheduled_sub_agents:
      - agent: alma
        skill: daily-brief
        interval_seconds: 86400
        lane: worker
    """

    data: dict[str, list[dict[str, object]]] = {}
    current_key: str | None = None
    current_item: dict[str, object] | None = None
    for raw_line in text.splitlines():
        uncommented = _strip_yaml_comment(raw_line).rstrip()
        if not uncommented.strip():
            continue
        indent = len(uncommented) - len(uncommented.lstrip(" "))
        stripped = uncommented.strip()
        if indent == 0:
            if not stripped.endswith(":"):
                raise ValueError(f"invalid runtime config section header: {stripped}")
            current_key = stripped[:-1].strip()
            data[current_key] = []
            current_item = None
            continue
        if indent == 2 and stripped.startswith("- "):
            if current_key is None:
                raise ValueError("runtime config list item without section header")
            current_item = {}
            data[current_key].append(current_item)
            inline = stripped[2:].strip()
            if inline:
                key, value = _parse_yaml_mapping_item(inline)
                current_item[key] = value
            continue
        if indent == 4:
            if current_item is None:
                raise ValueError("runtime config mapping entry without list item")
            key, value = _parse_yaml_mapping_item(stripped)
            current_item[key] = value
            continue
        raise ValueError(f"unsupported indentation in runtime config: {raw_line}")
    return data


def _load_or_create_approval_secret() -> str:
    secret_path = Path.home() / ".claw" / "approval_secret"
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    secret_path.write_text(secret, encoding="utf-8")
    secret_path.chmod(0o600)
    return secret


def _load_runtime_config_file(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        parsed = _parse_runtime_config_yaml_subset(text)
    else:
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise ValueError("runtime config file must contain a top-level mapping")
        parsed = loaded
    return parsed


def _coerce_monitored_sites(raw: object) -> list[MonitoredSiteConfig]:
    if raw is None:
        return _default_monitored_sites()
    if not isinstance(raw, list):
        raise ValueError("monitored_sites must be a list")
    sites: list[MonitoredSiteConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each monitored_sites entry must be a mapping")
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        interval = int(entry.get("interval_seconds", 3600))
        sites.append(MonitoredSiteConfig(name=name, url=url, interval_seconds=interval))
    return sites


def _coerce_scheduled_sub_agents(raw: object) -> list[ScheduledSubAgentConfig]:
    if raw is None:
        return _default_scheduled_sub_agents()
    if not isinstance(raw, list):
        raise ValueError("scheduled_sub_agents must be a list")
    jobs: list[ScheduledSubAgentConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each scheduled_sub_agents entry must be a mapping")
        interval_raw = entry.get("interval_seconds")
        interval_seconds = int(interval_raw) if interval_raw is not None else None
        daily_at_raw = entry.get("daily_at")
        daily_at = str(daily_at_raw).strip() if daily_at_raw else None
        timezone_raw = entry.get("timezone")
        timezone = str(timezone_raw).strip() if timezone_raw else None
        jobs.append(
            ScheduledSubAgentConfig(
                agent=str(entry.get("agent", "")).strip(),
                skill=str(entry.get("skill", "")).strip(),
                interval_seconds=interval_seconds,
                daily_at=daily_at,
                timezone=timezone,
                lane=str(entry.get("lane", "worker")).strip() or "worker",
            )
        )
    return jobs


def _normalize_notebooklm_backend(value: str | None) -> str:
    normalized = str(value or "cdp").strip().lower()
    if normalized in {"", "local"}:
        return "cdp"
    return normalized


@dataclass(slots=True)
class AppConfig:
    telegram_bot_token: str | None
    telegram_allowed_user_id: str | None
    web_chat_enabled: bool
    web_chat_host: str
    web_chat_port: int
    web_chat_token: str | None
    openai_api_key: str | None
    google_api_key: str | None
    linear_api_key: str | None
    claude_cli_path: str
    claude_auth_mode: str
    approval_secret: str
    approval_ttl_seconds: int
    brain_provider: str
    brain_model: str
    worker_provider: str
    worker_model: str
    worker_heavy_provider: str
    worker_heavy_model: str
    verifier_provider: str | None
    verifier_model: str | None
    research_provider: str | None
    research_model: str | None
    judge_provider: str | None
    judge_model: str | None
    worker_effort: str
    worker_heavy_effort: str
    brain_effort: str
    judge_effort: str
    max_budget_usd: float
    db_path: Path
    heartbeat_interval: int
    daily_token_budget: float
    workspace_root: Path
    agent_state_root: Path
    agent_definitions_root: Path
    eval_artifacts_root: Path
    eval_on_self_improve: bool
    self_improve_test_timeout_seconds: int
    autonomous_maintenance_enabled: bool
    use_compaction: bool
    brain_tooluse_verify: bool
    brain_tooluse_verify_timeout_seconds: float | None
    cache_prefix_ttl: int
    allowed_paths: list[Path]
    approvals_root: Path
    pipeline_repo_root: Path | None
    pipeline_label: str
    pipeline_max_retries: int
    pipeline_state_root: Path
    runtime_config_path: Path | None
    social_accounts_root: Path
    social_keychain_prefix: str
    allowed_read_paths: list[Path]
    extra_workspace_roots: list[Path]
    monitored_sites: list[MonitoredSiteConfig]
    scheduled_sub_agents: list[ScheduledSubAgentConfig]
    brain_context_window: int
    brain_max_output: int
    worker_context_window: int
    worker_max_output: int
    dev_browser_path: str
    dev_browser_browsers_path: str
    dev_browser_timeout: int
    browse_backend: str
    browserbase_api_key: str | None
    browserbase_project_id: str | None
    browserbase_api_url: str
    browserbase_region: str | None
    browserbase_keep_alive: bool
    sandbox_capability_profile: str
    sdk_bypass_permissions: bool
    daily_cost_limit: float | None
    tier_autoexec_max: int
    observability_telegram_chat_id: str | None
    observation_cost_per_hour_threshold: float
    observation_tool_calls_per_minute_threshold: int
    token_window_seconds: int
    token_window_cap: int
    token_soft_limit_ratio: float
    token_hard_limit_ratio: float
    command_isolation_mode: str
    enable_trivial_automerge: bool
    chrome_cdp_enabled: bool
    claw_chrome_port: int
    notebooklm_backend: str
    notebooklm_cli_path: str
    notebooklm_cli_profile: str | None
    notebooklm_cli_timeout_seconds: float
    notebooklm_cli_long_timeout_seconds: float
    computer_use_enabled: bool
    computer_use_required: bool
    computer_auto_approve: bool
    computer_display_width: int
    computer_display_height: int
    computer_browser_use_timeout_seconds: int
    ollama_host: str
    sensitive_urls: list[str]
    codex_cli_path: str
    codex_model: str
    computer_use_backend: str
    computer_browser_use_model: str
    morning_brief_enabled: bool
    morning_brief_hour: int
    morning_brief_timezone: str
    morning_brief_weather_location: str
    morning_brief_email_command: str | None
    morning_brief_calendar_command: str | None
    evening_brief_enabled: bool
    evening_brief_hour: int
    max_autonomous_workers: int
    telemetry_root: Path = field(default_factory=lambda: Path.home() / ".claw" / "telemetry")
    verifier_effort: str | None = None
    research_effort: str | None = None
    brain_thinking_tokens: int = 0
    worker_thinking_tokens: int = 0
    worker_heavy_thinking_tokens: int = 0
    verifier_thinking_tokens: int = 0
    research_thinking_tokens: int = 0
    judge_thinking_tokens: int = 0
    claw_worker_summary_limit: int = 16_000
    claw_phase_input_limit: int = 48_000
    control_judge_provider: str | None = None
    control_judge_model: str | None = None
    control_verifier_provider: str | None = None
    control_verifier_model: str | None = None
    critical_verifier_provider: str | None = None
    critical_verifier_model: str | None = None
    provider_timeout_brain_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS["brain"]
    provider_timeout_worker_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS["worker"]
    provider_timeout_heavy_coding_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS["heavy_coding"]
    provider_timeout_research_synthesis_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "research_synthesis"
    ]
    provider_timeout_control_judge_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS["control_judge"]
    provider_timeout_control_verifier_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "control_verifier"
    ]
    provider_timeout_critical_verifier_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "critical_verifier"
    ]
    provider_timeout_coordinator_worker_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "coordinator_worker"
    ]
    provider_timeout_coordinator_research_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "coordinator_research"
    ]
    provider_timeout_coordinator_verification_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "coordinator_verification"
    ]
    provider_timeout_coordinator_implementation_seconds: float = _PROVIDER_ROLE_TIMEOUT_DEFAULTS[
        "coordinator_implementation"
    ]
    maintenance_mode_enabled: bool = False
    no_job_claim_enabled: bool = False
    f2_durability_enabled: bool = False
    notebooklm_research_durable: bool = False

    @property
    def notebooklm_research_durable_active(self) -> bool:
        """The durable NotebookLM research lane requires BOTH F2 durability and
        its dedicated flag. Single source of truth for the conjunction used by
        build_runtime (runner registration) and lifecycle (start_research routing).
        """
        return self.f2_durability_enabled and self.notebooklm_research_durable

    @classmethod
    def from_env(cls) -> "AppConfig":
        home = Path.home()
        cwd = Path.cwd().resolve()
        runtime_config_path = (
            Path(rc).expanduser() if (rc := os.getenv("RUNTIME_CONFIG_PATH")) else None
        )
        if runtime_config_path is not None and not runtime_config_path.is_absolute():
            runtime_config_path = (cwd / runtime_config_path).resolve()
        runtime_config = _load_runtime_config_file(runtime_config_path)
        # Default read-root is the agent's own state dir, NOT all of $HOME
        # (2026-05-31 audit H2): the native Read tool must not reach arbitrary
        # private HOME files. Work dirs are opted in via EXTRA_WORKSPACE_ROOTS;
        # operators can broaden reads explicitly via ALLOWED_READ_PATHS.
        default_allowed_read_paths = [
            home / ".claw",
            Path("/private/tmp"),
        ]
        worker_provider = os.getenv("WORKER_PROVIDER", "anthropic")
        worker_model = os.getenv("WORKER_MODEL", "claude-sonnet-4-6")
        worker_heavy_provider = os.getenv("WORKER_HEAVY_PROVIDER", "codex")
        worker_heavy_model = os.getenv(
            "WORKER_HEAVY_MODEL"
        ) or _SECONDARY_PROVIDER_DEFAULT_MODELS.get(
            worker_heavy_provider,
            worker_model,
        )
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_allowed_user_id=os.getenv("TELEGRAM_ALLOWED_USER_ID"),
            web_chat_enabled=_env_bool("WEB_CHAT_ENABLED", True),
            web_chat_host=os.getenv("WEB_CHAT_HOST", "127.0.0.1"),
            web_chat_port=_env_int("WEB_CHAT_PORT", 8765),
            web_chat_token=os.getenv("WEB_CHAT_TOKEN"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            linear_api_key=os.getenv("LINEAR_API_KEY"),
            google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            claude_cli_path=os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude",
            claude_auth_mode=os.getenv("CLAUDE_AUTH_MODE", "subscription"),
            approval_secret=os.getenv("APPROVAL_SECRET") or _load_or_create_approval_secret(),
            approval_ttl_seconds=_env_int("APPROVAL_TTL_SECONDS", APPROVAL_TTL_SECONDS),
            brain_provider=os.getenv("BRAIN_PROVIDER", "anthropic"),
            brain_model=os.getenv("BRAIN_MODEL", "claude-opus-4-7"),
            worker_provider=worker_provider,
            worker_model=worker_model,
            worker_heavy_provider=worker_heavy_provider,
            worker_heavy_model=worker_heavy_model,
            verifier_provider=os.getenv("VERIFIER_PROVIDER"),
            verifier_model=os.getenv("VERIFIER_MODEL"),
            research_provider=os.getenv("RESEARCH_PROVIDER", "codex"),
            research_model=os.getenv("RESEARCH_MODEL"),
            judge_provider=os.getenv("JUDGE_PROVIDER", "codex"),
            judge_model=os.getenv("JUDGE_MODEL"),
            worker_effort=os.getenv("WORKER_EFFORT", "high"),
            worker_heavy_effort=os.getenv("WORKER_HEAVY_EFFORT", "high"),
            brain_effort=os.getenv("BRAIN_EFFORT", "high"),
            judge_effort=os.getenv("JUDGE_EFFORT", "medium"),
            verifier_effort=os.getenv("VERIFIER_EFFORT"),
            research_effort=os.getenv("RESEARCH_EFFORT"),
            brain_thinking_tokens=_env_int("BRAIN_THINKING_TOKENS", 0),
            worker_thinking_tokens=_env_int("WORKER_THINKING_TOKENS", 0),
            worker_heavy_thinking_tokens=_env_int("WORKER_HEAVY_THINKING_TOKENS", 0),
            verifier_thinking_tokens=_env_int("VERIFIER_THINKING_TOKENS", 0),
            research_thinking_tokens=_env_int("RESEARCH_THINKING_TOKENS", 0),
            judge_thinking_tokens=_env_int("JUDGE_THINKING_TOKENS", 0),
            max_budget_usd=_env_float("MAX_BUDGET_USD", 10.00),
            db_path=Path(os.getenv("DB_PATH", "data/claw.db")),
            heartbeat_interval=_env_int("HEARTBEAT_INTERVAL", 1800),
            daily_token_budget=_env_float("DAILY_TOKEN_BUDGET", 10.00),
            workspace_root=Path(os.getenv("WORKSPACE_ROOT", str(cwd))),
            agent_state_root=Path(os.getenv("AGENT_STATE_ROOT", str(home / ".claw" / "agents"))),
            agent_definitions_root=Path(os.getenv("AGENT_DEFINITIONS_ROOT", str(cwd / "agents"))),
            eval_artifacts_root=Path(
                os.getenv("EVAL_ARTIFACTS_ROOT", str(home / ".claw" / "evals"))
            ),
            eval_on_self_improve=_env_bool("EVAL_ON_SELF_IMPROVE", True),
            self_improve_test_timeout_seconds=_env_int("SELF_IMPROVE_TEST_TIMEOUT_SECONDS", 600),
            autonomous_maintenance_enabled=_autonomous_maintenance_from_env(),
            use_compaction=_env_bool("USE_COMPACTION", True),
            brain_tooluse_verify=_env_bool("BRAIN_TOOLUSE_VERIFY", False),
            brain_tooluse_verify_timeout_seconds=_brain_tooluse_verify_timeout_from_env(),
            cache_prefix_ttl=_env_int("CACHE_PREFIX_TTL", 3600),
            allowed_paths=[Path(p) for p in os.getenv("ALLOWED_PATHS", "").split(":") if p],
            approvals_root=Path(
                os.getenv("APPROVALS_ROOT", str(home / ".claw" / "pending_approvals"))
            ),
            pipeline_repo_root=Path(pr) if (pr := os.getenv("PIPELINE_REPO_ROOT")) else None,
            pipeline_label=os.getenv("PIPELINE_LABEL", "claw-auto"),
            pipeline_max_retries=_env_int("PIPELINE_MAX_RETRIES", 3),
            pipeline_state_root=Path(
                os.getenv("PIPELINE_STATE_ROOT", str(home / ".claw" / "pipeline"))
            ),
            runtime_config_path=runtime_config_path,
            social_accounts_root=Path(
                os.getenv(
                    "SOCIAL_ACCOUNTS_ROOT",
                    str(Path(__file__).parent / "agents" / "social" / "accounts"),
                )
            ),
            social_keychain_prefix=os.getenv("SOCIAL_KEYCHAIN_PREFIX", "com.pachano.claw.social"),
            allowed_read_paths=[
                Path(p)
                for p in os.getenv(
                    "ALLOWED_READ_PATHS",
                    ":".join(str(path) for path in default_allowed_read_paths),
                ).split(":")
                if p.strip()
            ],
            extra_workspace_roots=[
                Path(p) for p in os.getenv("EXTRA_WORKSPACE_ROOTS", "").split(":") if p.strip()
            ],
            monitored_sites=_coerce_monitored_sites(runtime_config.get("monitored_sites")),
            scheduled_sub_agents=_coerce_scheduled_sub_agents(
                runtime_config.get("scheduled_sub_agents")
            ),
            brain_context_window=_env_int("BRAIN_CONTEXT_WINDOW", 1000000),
            brain_max_output=_env_int("BRAIN_MAX_OUTPUT", 128000),
            worker_context_window=_env_int("WORKER_CONTEXT_WINDOW", 1000000),
            worker_max_output=_env_int("WORKER_MAX_OUTPUT", 64000),
            dev_browser_path=os.getenv("DEV_BROWSER_PATH", "dev-browser"),
            dev_browser_browsers_path=os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw-browsers"),
            dev_browser_timeout=_env_int("DEV_BROWSER_TIMEOUT", 30),
            browse_backend=os.getenv("BROWSE_BACKEND", "auto"),
            browserbase_api_key=os.getenv("BROWSERBASE_API_KEY"),
            browserbase_project_id=os.getenv("BROWSERBASE_PROJECT_ID"),
            browserbase_api_url=os.getenv("BROWSERBASE_API_URL", "https://api.browserbase.com"),
            browserbase_region=os.getenv("BROWSERBASE_REGION"),
            browserbase_keep_alive=_env_bool("BROWSERBASE_KEEP_ALIVE", False),
            sandbox_capability_profile=os.getenv("SANDBOX_CAPABILITY_PROFILE", "engineer"),
            sdk_bypass_permissions=_env_bool("SDK_BYPASS_PERMISSIONS", True),
            daily_cost_limit=_daily_cost_limit_from_env(),
            tier_autoexec_max=_env_tier("CLAW_TIER_AUTOEXEC_MAX", 2),
            observability_telegram_chat_id=os.getenv("CLAW_OBSERVABILITY_TELEGRAM_CHAT_ID") or None,
            observation_cost_per_hour_threshold=_env_float("CLAW_OBSERVATION_COST_PER_HOUR", 10.00),
            observation_tool_calls_per_minute_threshold=_env_int(
                "CLAW_OBSERVATION_TOOL_CALLS_PER_MINUTE", 10
            ),
            token_window_seconds=_env_int(
                "CLAW_TOKEN_WINDOW_SECONDS", _env_int("TOKEN_WINDOW_SECONDS", 18_000)
            ),
            token_window_cap=_env_int(
                "CLAW_TOKEN_WINDOW_CAP", _env_int("TOKEN_WINDOW_CAP", 1_000_000)
            ),
            token_soft_limit_ratio=_env_float(
                "CLAW_TOKEN_SOFT_LIMIT_RATIO", _env_float("TOKEN_SOFT_LIMIT_RATIO", 0.8)
            ),
            token_hard_limit_ratio=_env_float(
                "CLAW_TOKEN_HARD_LIMIT_RATIO", _env_float("TOKEN_HARD_LIMIT_RATIO", 1.0)
            ),
            command_isolation_mode=os.getenv(
                "CLAW_COMMAND_ISOLATION_MODE",
                os.getenv("COMMAND_ISOLATION_MODE", "docker_ephemeral"),
            ),
            enable_trivial_automerge=_env_bool(
                "CLAW_ENABLE_TRIVIAL_AUTOMERGE", _env_bool("ENABLE_TRIVIAL_AUTOMERGE", False)
            ),
            chrome_cdp_enabled=_env_bool("CHROME_CDP_ENABLED", True),
            claw_chrome_port=_env_int("CLAW_CHROME_PORT", 9250),
            notebooklm_backend=_normalize_notebooklm_backend(
                os.getenv("NOTEBOOKLM_BACKEND", "cdp")
            ),
            notebooklm_cli_path=os.getenv("NOTEBOOKLM_CLI_PATH") or shutil.which("nlm") or "nlm",
            notebooklm_cli_profile=os.getenv("NOTEBOOKLM_CLI_PROFILE") or None,
            notebooklm_cli_timeout_seconds=_env_float("NOTEBOOKLM_CLI_TIMEOUT_SECONDS", 120.0),
            notebooklm_cli_long_timeout_seconds=_env_float(
                "NOTEBOOKLM_CLI_LONG_TIMEOUT_SECONDS", 1200.0
            ),
            computer_use_enabled=_env_bool("COMPUTER_USE_ENABLED", True),
            computer_use_required=_env_bool("COMPUTER_USE_REQUIRED", False),
            # When true, browser_use_task and desktop/CDP actions auto-execute
            # without "te autorizo" EXCEPT for sensitive URLs (SENSITIVE_URLS),
            # destructive hotkeys, and CDP submit, which still require approval.
            computer_auto_approve=_env_bool("CLAW_COMPUTER_AUTO_APPROVE", False),
            computer_display_width=_env_int("COMPUTER_DISPLAY_WIDTH", 1280),
            computer_display_height=_env_int("COMPUTER_DISPLAY_HEIGHT", 800),
            # browser_use drives an authenticated browser (e.g. ChatGPT image
            # generation) one vision step at a time; the old hardcoded 180s
            # caps long renders mid-task. Configurable, default 7 min.
            computer_browser_use_timeout_seconds=_env_int("CLAW_BROWSER_USE_TIMEOUT", 420),
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            sensitive_urls=[
                u
                for u in os.getenv(
                    "SENSITIVE_URLS",
                    "ads.google.com:polymarket.com:robinhood.com:binance.com:stripe.com:paypal.com",
                ).split(":")
                if u.strip()
            ],
            codex_cli_path=os.getenv("CODEX_CLI_PATH") or shutil.which("codex") or "codex",
            codex_model=os.getenv("CODEX_MODEL", "codex-mini-latest"),
            computer_use_backend=os.getenv("COMPUTER_USE_BACKEND", "openai"),
            computer_browser_use_model=os.getenv("CLAW_BROWSER_USE_MODEL", "claude-sonnet-4-6"),
            morning_brief_enabled=_env_bool("MORNING_BRIEF_ENABLED", True),
            morning_brief_hour=_env_int("MORNING_BRIEF_HOUR", 5),
            morning_brief_timezone=os.getenv(
                "MORNING_BRIEF_TIMEZONE", os.getenv("TZ", "America/Chicago")
            ),
            morning_brief_weather_location=os.getenv(
                "MORNING_BRIEF_LOCATION", os.getenv("WEATHER_LOCATION", "")
            ),
            morning_brief_email_command=os.getenv("MORNING_BRIEF_EMAIL_COMMAND") or None,
            morning_brief_calendar_command=os.getenv("MORNING_BRIEF_CALENDAR_COMMAND") or None,
            evening_brief_enabled=_env_bool("EVENING_BRIEF_ENABLED", True),
            evening_brief_hour=_env_int("EVENING_BRIEF_HOUR", 21),
            max_autonomous_workers=_env_int("CLAW_MAX_AUTONOMOUS_WORKERS", 4),
            telemetry_root=Path(os.getenv("TELEMETRY_ROOT", str(home / ".claw" / "telemetry"))),
            claw_worker_summary_limit=_env_int("CLAW_WORKER_SUMMARY_LIMIT", 16_000),
            claw_phase_input_limit=_env_int("CLAW_PHASE_INPUT_LIMIT", 48_000),
            control_judge_provider=os.getenv("CLAW_CONTROL_JUDGE_PROVIDER")
            or os.getenv("CONTROL_JUDGE_PROVIDER"),
            control_judge_model=os.getenv("CLAW_CONTROL_JUDGE_MODEL")
            or os.getenv("CONTROL_JUDGE_MODEL"),
            control_verifier_provider=os.getenv("CLAW_CONTROL_VERIFIER_PROVIDER")
            or os.getenv("CONTROL_VERIFIER_PROVIDER"),
            control_verifier_model=os.getenv("CLAW_CONTROL_VERIFIER_MODEL")
            or os.getenv("CONTROL_VERIFIER_MODEL"),
            critical_verifier_provider=os.getenv("CLAW_CRITICAL_VERIFIER_PROVIDER")
            or os.getenv("CRITICAL_VERIFIER_PROVIDER"),
            critical_verifier_model=os.getenv("CLAW_CRITICAL_VERIFIER_MODEL")
            or os.getenv("CRITICAL_VERIFIER_MODEL"),
            provider_timeout_brain_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_BRAIN_SECONDS", _PROVIDER_ROLE_TIMEOUT_DEFAULTS["brain"]
            ),
            provider_timeout_worker_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_WORKER_SECONDS", _PROVIDER_ROLE_TIMEOUT_DEFAULTS["worker"]
            ),
            provider_timeout_heavy_coding_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_HEAVY_CODING_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["heavy_coding"],
            ),
            provider_timeout_research_synthesis_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_RESEARCH_SYNTHESIS_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["research_synthesis"],
            ),
            provider_timeout_control_judge_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_CONTROL_JUDGE_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["control_judge"],
            ),
            provider_timeout_control_verifier_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_CONTROL_VERIFIER_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["control_verifier"],
            ),
            provider_timeout_critical_verifier_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_CRITICAL_VERIFIER_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["critical_verifier"],
            ),
            provider_timeout_coordinator_worker_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_COORDINATOR_WORKER_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["coordinator_worker"],
            ),
            provider_timeout_coordinator_research_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_COORDINATOR_RESEARCH_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["coordinator_research"],
            ),
            provider_timeout_coordinator_verification_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_COORDINATOR_VERIFICATION_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["coordinator_verification"],
            ),
            provider_timeout_coordinator_implementation_seconds=_env_float(
                "CLAW_PROVIDER_TIMEOUT_COORDINATOR_IMPLEMENTATION_SECONDS",
                _PROVIDER_ROLE_TIMEOUT_DEFAULTS["coordinator_implementation"],
            ),
            maintenance_mode_enabled=maintenance_mode_enabled(),
            no_job_claim_enabled=no_job_claim_enabled(),
            f2_durability_enabled=_env_bool(
                "CLAW_F2_DURABILITY_ENABLED",
                _env_bool("F2_DURABILITY_ENABLED", False),
            ),
            notebooklm_research_durable=_env_bool("CLAW_NOTEBOOKLM_RESEARCH_DURABLE", False),
        )

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.agent_state_root.mkdir(parents=True, exist_ok=True)
        self.eval_artifacts_root.mkdir(parents=True, exist_ok=True)
        self.approvals_root.mkdir(parents=True, exist_ok=True)
        self.pipeline_state_root.mkdir(parents=True, exist_ok=True)
        self.telemetry_root.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.brain_provider != "anthropic":
            raise ValueError("brain_provider must be 'anthropic' in the current runtime design.")
        if self.worker_provider not in {"anthropic", "codex", "openai"}:
            raise ValueError("worker_provider must be 'anthropic', 'codex', or 'openai'.")
        if self.worker_heavy_provider not in {"anthropic", "codex", "openai"}:
            raise ValueError("worker_heavy_provider must be 'anthropic', 'codex', or 'openai'.")
        if self.claude_auth_mode not in {"subscription", "api_key", "auto"}:
            raise ValueError("claude_auth_mode must be one of: subscription, api_key, auto.")
        if self.approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive.")
        supported = {"anthropic", "openai", "google", "ollama", "codex"}
        supported_browse_backends = {"auto", "chrome_cdp", "playwright_local", "browserbase_cdp"}
        secondary = {
            "verifier_provider": self.verifier_provider,
            "research_provider": self.research_provider,
            "judge_provider": self.judge_provider,
        }
        for field_name, value in secondary.items():
            if value is not None and value not in supported:
                raise ValueError(f"{field_name} must be one of {sorted(supported)}.")
        for lane in ("brain", "worker", "worker_heavy", "verifier", "research", "judge"):
            _validate_provider_model_pair(
                self.provider_for_lane(lane), self.model_for_lane(lane), lane=lane
            )
        for role in _PROVIDER_ROLE_TIMEOUT_DEFAULTS:
            timeout = self.timeout_for_role(role)
            provider = self.provider_for_role(role)
            model = self.model_for_role(role)
            _validate_provider_model_pair(provider, model, lane=f"role:{role}")
            self.validate_provider_role_policy(role, provider, timeout=timeout)
        if self.browse_backend not in supported_browse_backends:
            raise ValueError(f"browse_backend must be one of {sorted(supported_browse_backends)}.")
        if self.sandbox_capability_profile not in {"surgical", "engineer", "admin"}:
            raise ValueError(
                "sandbox_capability_profile must be one of: surgical, engineer, admin."
            )
        if self.computer_use_backend not in {"openai", "codex"}:
            raise ValueError("computer_use_backend must be 'openai' or 'codex'.")
        if self.computer_browser_use_timeout_seconds <= 0:
            raise ValueError("computer_browser_use_timeout_seconds must be positive.")
        if not 0 <= self.morning_brief_hour <= 23:
            raise ValueError("morning_brief_hour must be between 0 and 23.")
        if not 0 <= self.evening_brief_hour <= 23:
            raise ValueError("evening_brief_hour must be between 0 and 23.")
        try:
            ZoneInfo(self.morning_brief_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"morning_brief_timezone is invalid: {self.morning_brief_timezone}"
            ) from exc
        if self.web_chat_port <= 0:
            raise ValueError("web_chat_port must be positive.")
        if self.tier_autoexec_max <= 0:
            raise ValueError("tier_autoexec_max must be positive.")
        if self.observation_cost_per_hour_threshold <= 0:
            raise ValueError("observation_cost_per_hour_threshold must be positive.")
        if self.observation_tool_calls_per_minute_threshold <= 0:
            raise ValueError("observation_tool_calls_per_minute_threshold must be positive.")
        if self.token_window_seconds <= 0:
            raise ValueError("token_window_seconds must be positive.")
        if self.token_window_cap <= 0:
            raise ValueError("token_window_cap must be positive.")
        if not 0 < self.token_soft_limit_ratio <= self.token_hard_limit_ratio:
            raise ValueError("token limit ratios must satisfy 0 < soft <= hard.")
        if self.command_isolation_mode not in {"host_sanitized", "docker_ephemeral"}:
            raise ValueError(
                "command_isolation_mode must be one of: host_sanitized, docker_ephemeral."
            )
        if self.notebooklm_backend not in {"cdp", "jacob"}:
            raise ValueError("notebooklm_backend must be one of: cdp, jacob.")
        if not self.notebooklm_cli_path:
            raise ValueError("notebooklm_cli_path must not be empty.")
        if self.notebooklm_cli_timeout_seconds <= 0:
            raise ValueError("notebooklm_cli_timeout_seconds must be positive.")
        if self.notebooklm_cli_long_timeout_seconds <= 0:
            raise ValueError("notebooklm_cli_long_timeout_seconds must be positive.")
        if self.max_autonomous_workers <= 0:
            raise ValueError("max_autonomous_workers must be positive.")
        if self.claw_worker_summary_limit <= 0:
            raise ValueError("claw_worker_summary_limit must be positive.")
        if self.claw_phase_input_limit <= 0:
            raise ValueError("claw_phase_input_limit must be positive.")
        for site in self.monitored_sites:
            if not site.name:
                raise ValueError("monitored_sites entries must include a name.")
            if not site.url.startswith(("http://", "https://")):
                raise ValueError(f"monitored site '{site.name}' must use http:// or https://.")
            if site.interval_seconds <= 0:
                raise ValueError(f"monitored site '{site.name}' interval_seconds must be positive.")
        for job in self.scheduled_sub_agents:
            if not job.agent or not job.skill:
                raise ValueError("scheduled_sub_agents entries must include agent and skill.")
            has_interval = job.interval_seconds is not None and job.interval_seconds > 0
            has_daily_at = bool(job.daily_at)
            if not has_interval and not has_daily_at:
                raise ValueError(
                    f"scheduled job '{job.agent}:{job.skill}' must declare interval_seconds or daily_at."
                )
            if has_daily_at:
                try:
                    parts = str(job.daily_at).split(":")
                    if len(parts) != 2:
                        raise ValueError("malformed")
                    hour = int(parts[0])
                    minute = int(parts[1])
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError("out_of_range")
                except Exception as exc:
                    raise ValueError(
                        f"scheduled job '{job.agent}:{job.skill}' daily_at must be HH:MM (00-23:00-59)."
                    ) from exc
                if not job.timezone:
                    raise ValueError(
                        f"scheduled job '{job.agent}:{job.skill}' daily_at requires timezone."
                    )
                try:
                    ZoneInfo(job.timezone)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError(
                        f"scheduled job '{job.agent}:{job.skill}' has invalid timezone: {job.timezone}"
                    ) from exc
            if not job.lane:
                raise ValueError(f"scheduled job '{job.agent}:{job.skill}' must include a lane.")

    def provider_for_lane(self, lane: Lane) -> str:
        critic_provider = "codex" if self.brain_provider == "anthropic" else "anthropic"
        mapping = {
            "brain": self.brain_provider,
            "worker": self.worker_provider,
            "worker_heavy": self.worker_heavy_provider,
            "verifier": self.verifier_provider or critic_provider,
            "research": self.research_provider or self.brain_provider,
            "judge": self.judge_provider or critic_provider,
        }
        return mapping[lane]

    def provider_for_role(self, role: ProviderRole) -> str:
        mapping: dict[ProviderRole, str] = {
            "brain": self.brain_provider,
            "worker": self.worker_provider,
            "heavy_coding": self.worker_heavy_provider,
            "research_synthesis": self.research_provider or self.brain_provider,
            "control_judge": self.control_judge_provider or self.brain_provider,
            "control_verifier": self.control_verifier_provider or self.brain_provider,
            "critical_verifier": self.critical_verifier_provider or self.brain_provider,
            "coordinator_worker": self.worker_provider,
            "coordinator_research": self.research_provider or self.brain_provider,
            "coordinator_verification": self.verifier_provider or self.brain_provider,
            "coordinator_implementation": self.worker_heavy_provider,
        }
        return mapping[role]

    def model_for_lane(self, lane: Lane) -> str:
        if lane == "brain":
            return self.brain_model
        if lane == "worker":
            return self.worker_model
        if lane == "worker_heavy":
            return self.worker_heavy_model
        explicit = {
            "verifier": self.verifier_model,
            "research": self.research_model,
            "judge": self.judge_model,
        }[lane]
        if explicit:
            return explicit
        return self.advisory_model_for_provider(self.provider_for_lane(lane))

    def model_for_role(self, role: ProviderRole) -> str:
        if role == "brain":
            return self.brain_model
        if role == "worker":
            return self.worker_model
        if role in {"heavy_coding", "coordinator_implementation"}:
            return self.worker_heavy_model
        if role == "coordinator_worker":
            return self.worker_model
        provider = self.provider_for_role(role)
        if role == "coordinator_verification":
            if self.verifier_provider and self.verifier_model:
                return self.verifier_model
            return self.advisory_model_for_provider(provider)
        explicit = {
            "research_synthesis": self.research_model,
            "control_judge": self.control_judge_model,
            "control_verifier": self.control_verifier_model,
            "critical_verifier": self.critical_verifier_model,
            "coordinator_research": self.research_model,
        }.get(role)
        if explicit:
            return explicit
        return self.advisory_model_for_provider(provider)

    def advisory_model_for_provider(self, provider: str) -> str:
        if provider == "anthropic":
            if self.worker_provider == "anthropic" and _is_anthropic_model(self.worker_model):
                return self.worker_model
            return _SECONDARY_PROVIDER_DEFAULT_MODELS["anthropic"]
        return _SECONDARY_PROVIDER_DEFAULT_MODELS.get(provider, self.worker_model)

    def effort_for_lane(self, lane: Lane) -> str:
        if lane == "brain":
            return self.brain_effort
        if lane == "worker":
            return self.worker_effort
        if lane == "worker_heavy":
            return self.worker_heavy_effort
        if lane == "verifier":
            return self.verifier_effort or self.judge_effort
        if lane == "research":
            return self.research_effort or self.judge_effort
        if lane == "judge":
            return self.judge_effort
        return "low"

    def thinking_tokens_for_lane(self, lane: Lane) -> int:
        mapping = {
            "brain": self.brain_thinking_tokens,
            "worker": self.worker_thinking_tokens,
            "worker_heavy": self.worker_heavy_thinking_tokens,
            "verifier": self.verifier_thinking_tokens,
            "research": self.research_thinking_tokens,
            "judge": self.judge_thinking_tokens,
        }
        return max(0, int(mapping.get(lane, 0)))

    def timeout_for_role(self, role: ProviderRole) -> float:
        mapping: dict[ProviderRole, float] = {
            "brain": self.provider_timeout_brain_seconds,
            "worker": self.provider_timeout_worker_seconds,
            "heavy_coding": self.provider_timeout_heavy_coding_seconds,
            "research_synthesis": self.provider_timeout_research_synthesis_seconds,
            "control_judge": self.provider_timeout_control_judge_seconds,
            "control_verifier": self.provider_timeout_control_verifier_seconds,
            "critical_verifier": self.provider_timeout_critical_verifier_seconds,
            "coordinator_worker": self.provider_timeout_coordinator_worker_seconds,
            "coordinator_research": self.provider_timeout_coordinator_research_seconds,
            "coordinator_verification": self.provider_timeout_coordinator_verification_seconds,
            "coordinator_implementation": self.provider_timeout_coordinator_implementation_seconds,
        }
        return float(mapping[role])

    def validate_provider_role_policy(
        self,
        role: ProviderRole,
        provider: str,
        *,
        timeout: float,
    ) -> None:
        if provider not in {"anthropic", "openai", "google", "ollama", "codex"}:
            raise ProviderRolePolicyError(f"{role}: unsupported provider {provider!r}.")
        if timeout <= 0:
            raise ProviderRolePolicyError(f"{role}: timeout must be positive.")
        if role in _CONTROL_PATH_PROVIDER_ROLES:
            if provider == "codex":
                raise ProviderRolePolicyError(f"{role}: codex is not allowed in the control path.")
            if timeout > _MAX_CONTROL_PATH_TIMEOUT_SECONDS:
                raise ProviderRolePolicyError(
                    f"{role}: timeout must be <= {_MAX_CONTROL_PATH_TIMEOUT_SECONDS:.0f}s."
                )

    def context_window_for_lane(self, lane: Lane) -> int:
        if lane == "brain":
            return self.brain_context_window
        return self.worker_context_window

    def max_output_for_lane(self, lane: Lane) -> int:
        if lane == "brain":
            return self.brain_max_output
        return self.worker_max_output

    def provider_billing_mode(self, provider: str) -> str:
        normalized = provider.strip().lower()
        if normalized == "codex":
            return "subscription"
        if normalized == "ollama":
            return "local"
        if normalized == "anthropic":
            if self.claude_auth_mode == "api_key":
                return "api"
            if self.claude_auth_mode == "subscription":
                return "subscription"
            return "api" if os.getenv("ANTHROPIC_API_KEY") else "subscription"
        if normalized in {"openai", "google"}:
            return "api"
        return "unknown"

    def billable_cost_providers(self) -> set[str]:
        providers = {"anthropic", "openai", "google"}
        return {provider for provider in providers if self.provider_billing_mode(provider) == "api"}

    def notional_cost_providers(self) -> set[str]:
        providers = {"anthropic", "codex", "ollama"}
        return {
            provider
            for provider in providers
            if self.provider_billing_mode(provider) in {"subscription", "local"}
        }

    def effective_max_budget_for_request(
        self,
        *,
        lane: Lane,
        provider: str,
        requested_budget: float,
    ) -> float:
        budget = float(requested_budget)
        if self.provider_billing_mode(provider) != "subscription":
            return budget
        floor = _SUBSCRIPTION_BUDGET_FLOORS.get(lane, 0.0)
        return max(budget, floor)
