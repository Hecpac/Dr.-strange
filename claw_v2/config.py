from __future__ import annotations

import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .types import Lane


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


_SECONDARY_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.4-mini",
    "google": "gemini-2.5-pro",
    "ollama": "gemma4",
    "codex": "codex-mini-latest",
}


def _is_anthropic_model(model: str | None) -> bool:
    return bool(model and model.startswith("claude-"))


@dataclass(slots=True)
class MonitoredSiteConfig:
    name: str
    url: str
    interval_seconds: int = 3600


@dataclass(slots=True)
class ScheduledSubAgentConfig:
    agent: str
    skill: str
    interval_seconds: int
    lane: str = "worker"


def _default_monitored_sites() -> list[MonitoredSiteConfig]:
    return [
        MonitoredSiteConfig(name="premiumhome.design", url="https://premiumhome.design", interval_seconds=3600),
        MonitoredSiteConfig(name="pachanodesign.com", url="https://www.pachanodesign.com", interval_seconds=3600),
    ]


def _default_scheduled_sub_agents() -> list[ScheduledSubAgentConfig]:
    return [
        ScheduledSubAgentConfig(agent="alma", skill="daily-brief", interval_seconds=86400, lane="worker"),
        ScheduledSubAgentConfig(agent="alma", skill="content-radar", interval_seconds=43200, lane="worker"),
        ScheduledSubAgentConfig(agent="hex", skill="bug-triage", interval_seconds=86400, lane="worker"),
        ScheduledSubAgentConfig(agent="rook", skill="health-audit", interval_seconds=21600, lane="worker"),
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
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
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
        jobs.append(
            ScheduledSubAgentConfig(
                agent=str(entry.get("agent", "")).strip(),
                skill=str(entry.get("skill", "")).strip(),
                interval_seconds=int(entry.get("interval_seconds", 0)),
                lane=str(entry.get("lane", "worker")).strip() or "worker",
            )
        )
    return jobs


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
    use_compaction: bool
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
    daily_cost_limit: float
    chrome_cdp_enabled: bool
    claw_chrome_port: int
    computer_use_enabled: bool
    computer_display_width: int
    computer_display_height: int
    ollama_host: str
    sensitive_urls: list[str]
    codex_cli_path: str
    codex_model: str
    computer_use_backend: str
    morning_brief_enabled: bool
    morning_brief_hour: int
    morning_brief_timezone: str
    morning_brief_weather_location: str
    morning_brief_email_command: str | None
    morning_brief_calendar_command: str | None
    evening_brief_enabled: bool
    evening_brief_hour: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        home = Path.home()
        cwd = Path.cwd().resolve()
        runtime_config_path = Path(rc).expanduser() if (rc := os.getenv("RUNTIME_CONFIG_PATH")) else None
        if runtime_config_path is not None and not runtime_config_path.is_absolute():
            runtime_config_path = (cwd / runtime_config_path).resolve()
        runtime_config = _load_runtime_config_file(runtime_config_path)
        default_allowed_read_paths = [
            home,
            Path("/private/tmp"),
        ]
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
            brain_provider=os.getenv("BRAIN_PROVIDER", "anthropic"),
            brain_model=os.getenv("BRAIN_MODEL", "claude-opus-4-7"),
            worker_provider=os.getenv("WORKER_PROVIDER", "anthropic"),
            worker_model=os.getenv("WORKER_MODEL", "claude-sonnet-4-6"),
            verifier_provider=os.getenv("VERIFIER_PROVIDER"),
            verifier_model=os.getenv("VERIFIER_MODEL"),
            research_provider=os.getenv("RESEARCH_PROVIDER"),
            research_model=os.getenv("RESEARCH_MODEL"),
            judge_provider=os.getenv("JUDGE_PROVIDER"),
            judge_model=os.getenv("JUDGE_MODEL"),
            worker_effort=os.getenv("WORKER_EFFORT", "high"),
            brain_effort=os.getenv("BRAIN_EFFORT", "high"),
            judge_effort=os.getenv("JUDGE_EFFORT", "medium"),
            max_budget_usd=_env_float("MAX_BUDGET_USD", 0.50),
            db_path=Path(os.getenv("DB_PATH", "data/claw.db")),
            heartbeat_interval=_env_int("HEARTBEAT_INTERVAL", 1800),
            daily_token_budget=_env_float("DAILY_TOKEN_BUDGET", 10.00),
            workspace_root=Path(os.getenv("WORKSPACE_ROOT", str(cwd))),
            agent_state_root=Path(os.getenv("AGENT_STATE_ROOT", str(home / ".claw" / "agents"))),
            agent_definitions_root=Path(os.getenv("AGENT_DEFINITIONS_ROOT", str(cwd / "agents"))),
            eval_artifacts_root=Path(os.getenv("EVAL_ARTIFACTS_ROOT", str(home / ".claw" / "evals"))),
            eval_on_self_improve=_env_bool("EVAL_ON_SELF_IMPROVE", True),
            use_compaction=_env_bool("USE_COMPACTION", True),
            cache_prefix_ttl=_env_int("CACHE_PREFIX_TTL", 3600),
            allowed_paths=[Path(p) for p in os.getenv("ALLOWED_PATHS", "").split(":") if p],
            approvals_root=Path(os.getenv("APPROVALS_ROOT", str(home / ".claw" / "pending_approvals"))),
            pipeline_repo_root=Path(pr) if (pr := os.getenv("PIPELINE_REPO_ROOT")) else None,
            pipeline_label=os.getenv("PIPELINE_LABEL", "claw-auto"),
            pipeline_max_retries=_env_int("PIPELINE_MAX_RETRIES", 3),
            pipeline_state_root=Path(os.getenv("PIPELINE_STATE_ROOT", str(home / ".claw" / "pipeline"))),
            runtime_config_path=runtime_config_path,
            social_accounts_root=Path(os.getenv("SOCIAL_ACCOUNTS_ROOT", str(Path(__file__).parent / "agents" / "social" / "accounts"))),
            social_keychain_prefix=os.getenv("SOCIAL_KEYCHAIN_PREFIX", "com.pachano.claw.social"),
            allowed_read_paths=[
                Path(p)
                for p in os.getenv(
                    "ALLOWED_READ_PATHS",
                    ":".join(str(path) for path in default_allowed_read_paths),
                ).split(":")
                if p.strip()
            ],
            extra_workspace_roots=[Path(p) for p in os.getenv("EXTRA_WORKSPACE_ROOTS", "").split(":") if p.strip()],
            monitored_sites=_coerce_monitored_sites(runtime_config.get("monitored_sites")),
            scheduled_sub_agents=_coerce_scheduled_sub_agents(runtime_config.get("scheduled_sub_agents")),
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
            daily_cost_limit=_env_float("DAILY_COST_LIMIT", 0.0),
            chrome_cdp_enabled=_env_bool("CHROME_CDP_ENABLED", True),
            claw_chrome_port=_env_int("CLAW_CHROME_PORT", 9250),
            computer_use_enabled=_env_bool("COMPUTER_USE_ENABLED", True),
            computer_display_width=_env_int("COMPUTER_DISPLAY_WIDTH", 1280),
            computer_display_height=_env_int("COMPUTER_DISPLAY_HEIGHT", 800),
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            sensitive_urls=[u for u in os.getenv("SENSITIVE_URLS", "ads.google.com:polymarket.com:robinhood.com:binance.com:stripe.com:paypal.com").split(":") if u.strip()],
            codex_cli_path=os.getenv("CODEX_CLI_PATH") or shutil.which("codex") or "codex",
            codex_model=os.getenv("CODEX_MODEL", "codex-mini-latest"),
            computer_use_backend=os.getenv("COMPUTER_USE_BACKEND", "openai"),
            morning_brief_enabled=_env_bool("MORNING_BRIEF_ENABLED", True),
            morning_brief_hour=_env_int("MORNING_BRIEF_HOUR", 5),
            morning_brief_timezone=os.getenv("MORNING_BRIEF_TIMEZONE", os.getenv("TZ", "America/Chicago")),
            morning_brief_weather_location=os.getenv("MORNING_BRIEF_LOCATION", os.getenv("WEATHER_LOCATION", "")),
            morning_brief_email_command=os.getenv("MORNING_BRIEF_EMAIL_COMMAND") or None,
            morning_brief_calendar_command=os.getenv("MORNING_BRIEF_CALENDAR_COMMAND") or None,
            evening_brief_enabled=_env_bool("EVENING_BRIEF_ENABLED", True),
            evening_brief_hour=_env_int("EVENING_BRIEF_HOUR", 21),
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
        if self.worker_provider not in {"anthropic", "codex", "openai"}:
            raise ValueError("worker_provider must be 'anthropic', 'codex', or 'openai'.")
        if self.claude_auth_mode not in {"subscription", "api_key", "auto"}:
            raise ValueError("claude_auth_mode must be one of: subscription, api_key, auto.")
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
        if self.browse_backend not in supported_browse_backends:
            raise ValueError(f"browse_backend must be one of {sorted(supported_browse_backends)}.")
        if self.sandbox_capability_profile not in {"surgical", "engineer", "admin"}:
            raise ValueError("sandbox_capability_profile must be one of: surgical, engineer, admin.")
        if self.computer_use_backend not in {"openai", "codex"}:
            raise ValueError("computer_use_backend must be 'openai' or 'codex'.")
        if not 0 <= self.morning_brief_hour <= 23:
            raise ValueError("morning_brief_hour must be between 0 and 23.")
        if not 0 <= self.evening_brief_hour <= 23:
            raise ValueError("evening_brief_hour must be between 0 and 23.")
        try:
            ZoneInfo(self.morning_brief_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"morning_brief_timezone is invalid: {self.morning_brief_timezone}") from exc
        if self.web_chat_port <= 0:
            raise ValueError("web_chat_port must be positive.")
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
            if job.interval_seconds <= 0:
                raise ValueError(f"scheduled job '{job.agent}:{job.skill}' interval_seconds must be positive.")
            if not job.lane:
                raise ValueError(f"scheduled job '{job.agent}:{job.skill}' must include a lane.")

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
        if lane == "brain":
            return self.brain_model
        if lane == "worker":
            return self.worker_model
        explicit = {
            "verifier": self.verifier_model,
            "research": self.research_model,
            "judge": self.judge_model,
        }[lane]
        if explicit:
            return explicit
        return self.advisory_model_for_provider(self.provider_for_lane(lane))

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
        if lane in ("judge", "verifier", "research"):
            return self.judge_effort
        return "low"

    def context_window_for_lane(self, lane: Lane) -> int:
        if lane == "brain":
            return self.brain_context_window
        return self.worker_context_window

    def max_output_for_lane(self, lane: Lane) -> int:
        if lane == "brain":
            return self.brain_max_output
        return self.worker_max_output
