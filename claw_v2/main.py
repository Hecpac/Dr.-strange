from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claw_v2.a2a import A2AService
from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.adapters.base import LLMRequest
from claw_v2.agent_runtime import AgentRuntime
from claw_v2.agents import (
    AgentDefinition,
    AutoResearchAgentService,
    FileAgentStore,
    GitBranchPromotionExecutor,
    GitWorktreeExperimentRunner,
    SubAgentService,
)
from claw_v2.approval import ApprovalManager
from claw_v2.approval_gate import (
    build_system_auto_approve_gate,
    build_telegram_approval_gate,
    current_daemon_reason,
)
from claw_v2.bot import BotService
from claw_v2.brain import BrainService
from claw_v2.browser import DevBrowserService
from claw_v2.checkpoint import CheckpointService
from claw_v2.buddy import BuddyService
from claw_v2.bus import AgentBus
from claw_v2.computer import BrowserUseService, CodexComputerBackend, ComputerUseService
from claw_v2.config import AppConfig, MonitoredSiteConfig, ScheduledSubAgentConfig
from claw_v2.coordinator import CoordinatorService
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.dream import AutoDreamService
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.hooks import make_anti_distillation_hook, make_daily_cost_gate, make_decision_logger
from claw_v2.jobs import JobService
from claw_v2.kairos import KairosService
from claw_v2.learning import LearningLoop
from claw_v2.linear import LinearService, build_linear_api_caller
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.metrics import MetricsTracker
from claw_v2.model_registry import ModelRegistry
from claw_v2.observe import ObserveStream
from claw_v2.pipeline import PipelineService
from claw_v2.skills import SkillRegistry
from claw_v2.social import SocialPublisher
from claw_v2.content import ContentEngine
from claw_v2.task_board import TaskBoard
from claw_v2.task_ledger import TaskLedger
from claw_v2.terminal_bridge import TerminalBridgeService
from claw_v2.types import LLMResponse
from claw_v2.workspace import AgentWorkspace
from claw_v2.wiki import WikiService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthcheckResult:
    name: str
    status: str
    detail: str = ""
    capability: str | None = None


@dataclass(slots=True)
class StartupHealthReport:
    ok: list[HealthcheckResult] = field(default_factory=list)
    degraded: list[HealthcheckResult] = field(default_factory=list)
    failed: list[HealthcheckResult] = field(default_factory=list)

    def add_ok(self, name: str, detail: str = "", *, capability: str | None = None) -> None:
        self.ok.append(HealthcheckResult(name=name, status="ok", detail=detail, capability=capability))

    def add_degraded(self, name: str, detail: str, *, capability: str | None = None) -> None:
        self.degraded.append(HealthcheckResult(name=name, status="degraded", detail=detail, capability=capability))

    def add_failed(self, name: str, detail: str, *, capability: str | None = None) -> None:
        self.failed.append(HealthcheckResult(name=name, status="failed", detail=detail, capability=capability))

    def failed_summary(self) -> str:
        return "; ".join(f"{item.name}: {item.detail}" for item in self.failed)

    def degraded_capabilities(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.degraded:
            if item.capability:
                result[item.capability] = item.detail
        return result


@dataclass(slots=True)
class ClawRuntime:
    config: AppConfig
    memory: MemoryStore
    observe: ObserveStream
    metrics: MetricsTracker
    approvals: ApprovalManager
    bus: AgentBus
    agent_store: FileAgentStore
    router: LLMRouter
    brain: BrainService
    auto_research: AutoResearchAgentService
    sub_agents: SubAgentService
    coordinator: CoordinatorService
    task_board: TaskBoard
    kairos: KairosService
    buddy: BuddyService
    heartbeat: HeartbeatService
    scheduler: CronScheduler
    daemon: ClawDaemon
    bot: BotService
    agent_runtime: AgentRuntime
    agent_workspace: AgentWorkspace
    task_ledger: TaskLedger
    job_service: JobService
    model_registry: ModelRegistry
    skill_registry: SkillRegistry | None = None
    a2a: A2AService | None = None
    startup_health: StartupHealthReport | None = None
    tool_registry: object | None = None
    openai_tool_executor: object | None = None


def _noop_experiment_runner(agent_name: str, experiment_number: int, state: dict) -> object:
    from claw_v2.agents import ExperimentRecord

    baseline = state.get("last_verified_state", {}).get("metric") or 0.0
    return ExperimentRecord(
        experiment_number=experiment_number,
        metric_value=baseline,
        baseline_value=baseline,
        status="noop",
        cost_usd=0.0,
    )


def _is_git_repo(path: str | Path) -> bool:
    git_path = shutil.which("git")
    if git_path is None:
        return False
    try:
        completed = subprocess.run(
            [git_path, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _sanitize_job_name(value: str) -> str:
    import hashlib

    slug = "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_") or "job"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{digest}"


def _resolve_pytest_command(repo_root: Path) -> tuple[list[str], str | None]:
    venv_pytest = repo_root / ".venv" / "bin" / "pytest"
    if venv_pytest.exists():
        return [str(venv_pytest), "tests/", "-x", "-q", "--tb=no"], str(venv_pytest)
    system_pytest = shutil.which("pytest")
    if system_pytest:
        return [system_pytest, "tests/", "-x", "-q", "--tb=no"], system_pytest
    if importlib.util.find_spec("pytest") is not None:
        return [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--tb=no"], sys.executable
    return [], None


def _probe_writable(path: Path) -> None:
    import os
    import tempfile

    fd, probe_name = tempfile.mkstemp(prefix=".claw_write_probe_", dir=str(path), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("ok")
    finally:
        Path(probe_name).unlink(missing_ok=True)


def _find_local_chrome() -> str | None:
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _run_startup_healthchecks(config: AppConfig, observe: ObserveStream) -> StartupHealthReport:
    report = StartupHealthReport()

    for name, path in (
        ("workspace_root", config.workspace_root),
        ("agent_state_root", config.agent_state_root),
        ("approvals_root", config.approvals_root),
        ("pipeline_state_root", config.pipeline_state_root),
    ):
        try:
            _probe_writable(path)
        except Exception as exc:
            report.add_failed(name, f"write probe failed for {path}: {exc}")
        else:
            report.add_ok(name, f"writable: {path}")

    if config.runtime_config_path is not None:
        if config.runtime_config_path.exists():
            report.add_ok("runtime_config", f"loaded: {config.runtime_config_path}")
        else:
            report.add_failed("runtime_config", f"missing runtime config file: {config.runtime_config_path}")
    else:
        report.add_ok("runtime_config", "using built-in runtime scheduling defaults")

    needs_git = config.eval_on_self_improve or _is_git_repo(str(config.workspace_root))
    if needs_git:
        git_path = shutil.which("git")
        if git_path is None:
            report.add_degraded("git", "git is not installed; self-improve and git-backed flows will be skipped", capability="git")
        else:
            report.add_ok("git", git_path, capability="git")

    if config.eval_on_self_improve:
        _, pytest_path = _resolve_pytest_command(config.pipeline_repo_root or config.workspace_root)
        if pytest_path is None:
            report.add_degraded("pytest", "pytest is unavailable; self-improve will be skipped", capability="pytest")
        else:
            report.add_ok("pytest", pytest_path, capability="pytest")

    if config.chrome_cdp_enabled and config.browse_backend in {"auto", "chrome_cdp"}:
        chrome_path = _find_local_chrome()
        if chrome_path is None:
            report.add_degraded(
                "chrome_cdp",
                "Chrome no está instalado o no es accesible; la navegación autenticada por CDP quedará degradada",
                capability="chrome_cdp",
            )
        else:
            report.add_ok("chrome_cdp", chrome_path, capability="chrome_cdp")
    elif not config.chrome_cdp_enabled:
        report.add_degraded(
            "chrome_cdp",
            "Chrome CDP está desactivado en la configuración",
            capability="chrome_cdp",
        )

    if config.computer_use_enabled:
        report.add_ok("computer_use", "desktop control enabled", capability="computer_use")
    else:
        report.add_degraded(
            "computer_use",
            "Computer Use está desactivado en la configuración",
            capability="computer_use",
        )

    if config.computer_use_backend == "codex":
        codex_path = shutil.which(config.codex_cli_path) if config.codex_cli_path else None
        if codex_path is None:
            report.add_degraded(
                "codex_cli",
                f"Codex CLI no está disponible en '{config.codex_cli_path}'",
                capability="computer_control",
            )
        else:
            report.add_ok("codex_cli", codex_path, capability="computer_control")
    elif config.computer_use_enabled and not sys.modules.get("openai") and importlib.util.find_spec("openai") is None:
        report.add_degraded(
            "openai_sdk",
            "OpenAI SDK no está instalado; el control de escritorio asistido quedará degradado",
            capability="computer_control",
        )
    else:
        report.add_ok("computer_control", config.computer_use_backend, capability="computer_control")

    if importlib.util.find_spec("browser_use") is None:
        report.add_degraded(
            "browser_use",
            "El paquete browser_use no está instalado; la automatización web guiada quedará degradada",
            capability="browser_use",
        )
    else:
        report.add_ok("browser_use", "browser_use available", capability="browser_use")

    for item in report.ok:
        observe.emit("startup_healthcheck_ok", payload={"name": item.name, "detail": item.detail})
    for item in report.degraded:
        observe.emit("startup_healthcheck_degraded", payload={"name": item.name, "detail": item.detail, "capability": item.capability})
    if report.failed:
        for item in report.failed:
            observe.emit("startup_healthcheck_failed", payload={"name": item.name, "detail": item.detail})
        raise RuntimeError(f"startup healthchecks failed: {report.failed_summary()}")
    return report


def _wrap_job_handler(
    *,
    name: str,
    observe: ObserveStream,
    handler: Callable[[], object],
    skip_if: Callable[[], str | None] | None = None,
) -> Callable[[], object | None]:
    def wrapped() -> object | None:
        if skip_if is not None:
            reason = skip_if()
            if reason:
                observe.emit("scheduled_job_skipped", payload={"job": name, "reason": reason})
                logger.warning("scheduled job %s skipped: %s", name, reason)
                return None
        try:
            return handler()
        except Exception as exc:
            observe.emit("scheduled_job_error", payload={"job": name, "error": str(exc)[:500]})
            logger.exception("scheduled job %s failed", name)
            return None

    return wrapped


def _setup_core_state(config: AppConfig) -> tuple[MemoryStore, ObserveStream, MetricsTracker, ApprovalManager, AgentBus, FileAgentStore]:
    memory = MemoryStore(config.db_path)
    observe = ObserveStream(config.db_path)
    metrics = MetricsTracker()
    approvals = ApprovalManager(config.approvals_root, config.approval_secret)
    bus = AgentBus(config.agent_state_root / "_bus")
    agent_store = FileAgentStore(config.agent_state_root)
    return memory, observe, metrics, approvals, bus, agent_store


def _setup_llm_stack(
    *,
    config: AppConfig,
    memory: MemoryStore,
    observe: ObserveStream,
    metrics: MetricsTracker,
    approvals: ApprovalManager,
    system_prompt: str,
    anthropic_executor: Callable[[LLMRequest], LLMResponse] | None,
    openai_transport: Callable[[LLMRequest], LLMResponse] | None,
    google_transport: Callable[[LLMRequest], LLMResponse] | None,
    ollama_transport: Callable[[LLMRequest], LLMResponse] | None,
    codex_transport: Callable[[LLMRequest], LLMResponse] | None,
) -> tuple[LLMRouter, LearningLoop, BrainService, "ToolRegistry", Callable[[str, dict], dict]]:
    def audit_sink(event: dict) -> None:
        observe.emit(
            event["action"],
            lane=event["lane"],
            provider=event["provider"],
            model=event["model"],
            trace_id=event["metadata"].get("trace_id"),
            root_trace_id=event["metadata"].get("root_trace_id"),
            span_id=event["metadata"].get("span_id"),
            parent_span_id=event["metadata"].get("parent_span_id"),
            job_id=event["metadata"].get("job_id"),
            artifact_id=event["metadata"].get("artifact_id"),
            payload={
                "confidence": event["confidence"],
                "cost_estimate": event["cost_estimate"],
                "degraded_mode": event["degraded_mode"],
                **event["metadata"],
            },
        )
        metrics.record(
            lane=event["lane"],
            provider=event["provider"],
            model=event["model"],
            cost=event["cost_estimate"],
            degraded_mode=event["degraded_mode"],
        )

    if anthropic_executor is None:
        anthropic_executor = create_claude_sdk_executor(config, observe=observe, approvals=approvals)

    auth_mode = getattr(config, "claude_auth_mode", "auto")
    if config.daily_cost_limit is None:
        pre_hooks: list = [make_daily_cost_gate(observe, 10.0, auth_mode=auth_mode)]
    elif config.daily_cost_limit > 0:
        pre_hooks = [
            make_daily_cost_gate(observe, config.daily_cost_limit, auth_mode=auth_mode)
        ]
    else:
        raise ValueError("daily_cost_limit must be positive or None")
    pre_hooks.append(make_anti_distillation_hook())
    post_hooks = [make_decision_logger(observe)]

    # Build OpenAI tool executor from ToolRegistry
    from claw_v2.tools import ToolRegistry
    from claw_v2.tool_policy import daemon_can_auto_approve

    tool_registry = ToolRegistry.default(workspace_root=config.workspace_root, memory=memory)
    openai_tool_schemas = tool_registry.openai_tool_schemas()

    # Paso 4 (HEC-14): wire ApprovalManager into the dispatcher via a gate.
    # The shared closure picks the gate based on the active context:
    #   - Telegram/interactive (default) -> build_telegram_approval_gate raises
    #     ApprovalPending so the bot can surface `/approve <id> <token>`.
    #   - Daemon/Kairos (inside `system_approval_mode(reason=...)`) -> the
    #     system auto-approve gate records an audit entry with the scheduler's
    #     reason and proceeds without blocking.
    telegram_gate = build_telegram_approval_gate(approvals)

    def openai_tool_executor(name: str, args: dict) -> dict:
        registry_tool_name = tool_registry.original_tool_name_from_openai(name)
        daemon_reason = current_daemon_reason()
        if daemon_reason is not None and daemon_can_auto_approve(registry_tool_name):
            gate = build_system_auto_approve_gate(approvals, reason=daemon_reason)
        else:
            gate = telegram_gate
        return tool_registry.execute(
            registry_tool_name,
            args,
            agent_class="operator",
            approval_gate=gate,
        )

    router = LLMRouter.default(
        config,
        anthropic_executor=anthropic_executor,
        openai_transport=openai_transport,
        google_transport=google_transport,
        ollama_transport=ollama_transport,
        codex_transport=codex_transport,
        audit_sink=audit_sink,
        pre_hooks=pre_hooks,
        post_hooks=post_hooks,
        openai_tool_executor=openai_tool_executor,
        openai_tool_schemas=openai_tool_schemas,
    )
    learning = LearningLoop(memory=memory, router=router)
    checkpoint = CheckpointService(
        memory=memory,
        snapshots_dir=config.db_path.parent / "snapshots",
    )
    brain = BrainService(
        router=router,
        memory=memory,
        system_prompt=system_prompt,
        approvals=approvals,
        observe=observe,
        learning=learning,
        checkpoint=checkpoint,
    )
    return router, learning, brain, tool_registry, openai_tool_executor


def _setup_agent_services(
    *,
    config: AppConfig,
    router: LLMRouter,
    memory: MemoryStore,
    observe: ObserveStream,
    approvals: ApprovalManager,
    bus: AgentBus,
    agent_store: FileAgentStore,
    metrics: MetricsTracker,
    brain: BrainService,
) -> tuple[AutoResearchAgentService, SubAgentService, CoordinatorService, TaskBoard, HeartbeatService, KairosService, BuddyService]:
    experiment_runner: Callable[[str, int, dict], object] = _noop_experiment_runner
    if _is_git_repo(str(config.workspace_root)):
        experiment_runner = GitWorktreeExperimentRunner(
            repo_root=config.workspace_root,
            worktree_root=config.agent_state_root / "_worktrees",
            router=router,
            brain=brain,
            promotion_executor=GitBranchPromotionExecutor(config.workspace_root),
        )

    auto_research = AutoResearchAgentService(
        router=router,
        store=agent_store,
        experiment_runner=experiment_runner,
        observe=observe,
    )
    sub_agents = SubAgentService(config.agent_definitions_root, router, agent_store)
    discovered = sub_agents.discover()
    if discovered:
        observe.emit("sub_agents_discovered", payload={"agents": discovered})
    coordinator = CoordinatorService(
        router=router,
        observe=observe,
        scratch_root=config.agent_state_root / "_scratch",
        agent_registry=sub_agents.registry(),
    )
    task_board = TaskBoard(board_root=config.agent_state_root / "_board")
    registry_path = config.agent_state_root / "AGENTS.md"
    heartbeat = HeartbeatService(
        metrics=metrics,
        approvals=approvals,
        agent_store=agent_store,
        observe=observe,
        registry_path=registry_path,
        sub_agents=sub_agents,
        default_agent_model=config.worker_model,
        default_daily_budget=(config.daily_cost_limit if config.daily_cost_limit is not None else 10.0),
    )
    kairos = KairosService(
        router=router,
        heartbeat=heartbeat,
        observe=observe,
        bus=bus,
        approvals=approvals,
        sub_agents=sub_agents,
        auto_research=auto_research,
        task_board=task_board,
        monitored_sites=config.monitored_sites,
    )
    buddy = BuddyService(config.db_path.parent / "buddy.db")
    return auto_research, sub_agents, coordinator, task_board, heartbeat, kairos, buddy


def _create_pull_request_service(workspace_root: Path) -> GitHubPullRequestService | None:
    return GitHubPullRequestService(workspace_root) if _is_git_repo(str(workspace_root)) else None


def _setup_operational_services(
    *,
    config: AppConfig,
    router: LLMRouter,
    memory: MemoryStore,
    observe: ObserveStream,
    approvals: ApprovalManager,
    heartbeat: HeartbeatService,
    brain: BrainService,
    auto_research: AutoResearchAgentService,
    sub_agents: SubAgentService,
    coordinator: CoordinatorService,
    task_board: TaskBoard,
    task_ledger: TaskLedger,
    job_service: JobService,
    model_registry: ModelRegistry,
    buddy: BuddyService,
    learning: LearningLoop,
    kairos: KairosService,
    startup_health: StartupHealthReport,
) -> tuple[ClawDaemon, BotService, PipelineService, DevBrowserService, BrowserUseService, ComputerUseService]:
    daemon = ClawDaemon(
        scheduler=CronScheduler(),
        heartbeat=heartbeat,
        observe=observe,
        task_ledger=task_ledger,
        job_service=job_service,
    )
    browser = DevBrowserService(
        dev_browser_path=config.dev_browser_path,
        browsers_path=config.dev_browser_browsers_path,
        timeout=config.dev_browser_timeout,
    )
    terminal_bridge = TerminalBridgeService()
    codex_computer_backend: CodexComputerBackend | None = None
    if config.computer_use_backend == "codex":
        codex_computer_backend = CodexComputerBackend(
            cli_path=config.codex_cli_path,
            model=config.codex_model,
        )
    computer = ComputerUseService(
        display_width=config.computer_display_width,
        display_height=config.computer_display_height,
        codex_backend=codex_computer_backend,
    )
    browser_use = BrowserUseService(cdp_url=f"http://localhost:{config.claw_chrome_port}")
    bot = BotService(
        brain=brain,
        auto_research=auto_research,
        heartbeat=heartbeat,
        approvals=approvals,
        pull_requests=_create_pull_request_service(config.workspace_root),
        allowed_user_id=config.telegram_allowed_user_id,
        config=config,
        coordinator=coordinator,
        browser=browser,
        terminal_bridge=terminal_bridge,
        computer=computer,
        browser_use=browser_use,
        observe=observe,
        task_ledger=task_ledger,
        job_service=job_service,
        model_registry=model_registry,
    )
    for capability, reason in startup_health.degraded_capabilities().items():
        bot.set_capability_status(capability, available=False, reason=reason)
    if not config.chrome_cdp_enabled:
        bot.set_capability_status("chrome_cdp", available=False, reason="Chrome CDP está desactivado en la configuración.")
    if not config.computer_use_enabled:
        bot.set_capability_status("computer_use", available=False, reason="Computer Use está desactivado en la configuración.")

    if config.linear_api_key:
        linear = LinearService(mcp_caller=build_linear_api_caller(config.linear_api_key))
    else:
        linear = LinearService(mcp_caller=lambda action, **kw: None)
    pipeline = PipelineService(
        linear=linear,
        router=router,
        approvals=approvals,
        pull_requests=_create_pull_request_service(config.workspace_root),
        observe=observe,
        default_repo_root=config.pipeline_repo_root or config.workspace_root,
        max_retries=config.pipeline_max_retries,
        state_root=config.pipeline_state_root,
        memory=memory,
        learning=learning,
    )
    bot.pipeline = pipeline
    bot.learning = learning
    bot.sub_agents = sub_agents
    bot.buddy = buddy

    content_engine = ContentEngine(router=router, accounts_root=config.social_accounts_root)
    bot.content_engine = content_engine
    bot.social_publisher = SocialPublisher(adapters={})

    return daemon, bot, pipeline, browser, browser_use, computer


def _register_site_monitor_jobs(
    *,
    scheduler: CronScheduler,
    observe: ObserveStream,
    sites: list[MonitoredSiteConfig],
) -> None:
    import httpx

    def _site_monitor_handler(site: MonitoredSiteConfig) -> None:
        try:
            response = httpx.get(site.url, timeout=15, follow_redirects=True)
            payload = {"site": site.name, "url": site.url, "status": response.status_code, "ok": response.status_code < 400}
            if response.status_code < 400:
                observe.emit("site_monitor_ok", payload=payload)
                logger.info("site monitor %s ok (%s)", site.name, response.status_code)
            else:
                observe.emit("site_down", payload=payload)
                logger.warning("site monitor %s failed (%s)", site.name, response.status_code)
        except Exception as exc:
            observe.emit("site_down", payload={"site": site.name, "url": site.url, "status": 0, "ok": False, "error": str(exc)})
            logger.warning("site monitor %s exception: %s", site.name, exc)

    for site in sites:
        job_name = f"site_monitor_{_sanitize_job_name(site.name)}"
        scheduler.register(
            ScheduledJob(
                name=job_name,
                interval_seconds=site.interval_seconds,
                handler=_wrap_job_handler(name=job_name, observe=observe, handler=lambda s=site: _site_monitor_handler(s)),
            )
        )


def _register_sub_agent_jobs(
    *,
    scheduler: CronScheduler,
    observe: ObserveStream,
    sub_agents: SubAgentService,
    scheduled_jobs: list[ScheduledSubAgentConfig],
) -> None:
    def _sub_agent_handler(agent: str, skill: str, lane: str) -> None:
        result = sub_agents.run_skill(agent, skill, lane=lane)
        observe.emit("sub_agent_skill", payload={"agent": agent, "skill": skill, "lane": lane, "result": result})

    for job in scheduled_jobs:
        job_name = f"{_sanitize_job_name(job.agent)}_{_sanitize_job_name(job.skill)}"

        def _skip_reason(agent: str = job.agent, skill: str = job.skill) -> str | None:
            definition = sub_agents.get_agent(agent)
            if definition is None:
                return f"sub-agent '{agent}' not found"
            if skill not in definition.skills:
                return f"skill '{skill}' is not available for sub-agent '{agent}'"
            return None

        scheduler.register(
            ScheduledJob(
                name=job_name,
                interval_seconds=job.interval_seconds,
                daily_at=job.daily_at,
                timezone=job.timezone,
                handler=_wrap_job_handler(
                    name=job_name,
                    observe=observe,
                    handler=lambda a=job.agent, s=job.skill, l=job.lane: _sub_agent_handler(a, s, l),
                    skip_if=_skip_reason,
                ),
            )
        )


def _setup_scheduler(
    *,
    config: AppConfig,
    system_prompt: str,
    memory: MemoryStore,
    observe: ObserveStream,
    metrics: MetricsTracker,
    heartbeat: HeartbeatService,
    kairos: KairosService,
    buddy: BuddyService,
    auto_research: AutoResearchAgentService,
    agent_store: FileAgentStore,
    learning: LearningLoop,
    router: LLMRouter,
    task_board: TaskBoard,
    sub_agents: SubAgentService,
    bot: BotService,
    pipeline: PipelineService,
    startup_health: StartupHealthReport,
) -> tuple[CronScheduler, AutoDreamService, WikiService, SkillRegistry, A2AService]:
    def _cron_error_sink(job: ScheduledJob, exc: BaseException) -> None:
        observe.emit(
            "scheduled_job_error",
            payload={
                "job": job.name,
                "error": str(exc)[:500],
                "metadata": job.metadata,
            },
        )

    scheduler = CronScheduler(persistence=memory, error_sink=_cron_error_sink)
    skipped_capabilities = startup_health.degraded_capabilities()

    def _skip_for(*capabilities: str) -> Callable[[], str | None]:
        def inner() -> str | None:
            for capability in capabilities:
                reason = skipped_capabilities.get(capability)
                if reason:
                    return reason
            return None

        return inner

    def _maintenance_skip() -> str | None:
        if not config.autonomous_maintenance_enabled:
            return "autonomous_maintenance_disabled"
        return None

    def _skip_maintenance_or(*capabilities: str) -> Callable[[], str | None]:
        capability_check = _skip_for(*capabilities)

        def inner() -> str | None:
            return _maintenance_skip() or capability_check()

        return inner

    def _self_improve_handler() -> None:
        observe.emit("self_improve_start", payload={})
        repo_root = config.pipeline_repo_root or config.workspace_root
        pytest_args, _ = _resolve_pytest_command(repo_root)
        if not pytest_args:
            observe.emit("self_improve_blocked", payload={"reason": "pytest_unavailable"})
            return

        try:
            test_result = subprocess.run(
                pytest_args,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(repo_root),
                timeout=config.self_improve_test_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            observe.emit(
                "self_improve_blocked",
                payload={"reason": "tests_timeout", "timeout": exc.timeout},
            )
            return
        if test_result.returncode != 0:
            observe.emit("self_improve_blocked", payload={"reason": "tests_failed", "output": (test_result.stdout or "")[-500:]})
            return

        default_name = "self-improve"
        if not agent_store.state_path(default_name).exists():
            auto_research.create_agent(
                AgentDefinition(
                    name=default_name,
                    agent_class="operator",
                    instruction=(
                        "Improve the Claw codebase: optimize prompts, fix edge cases, "
                        "add missing error handling, improve test coverage. "
                        "Make one small, safe, incremental change per experiment."
                    ),
                    lane="worker",
                ),
                state={
                    "promote_on_improvement": True,
                    "commit_on_promotion": True,
                    "metric_command": f"PYTHONPATH=. {sys.executable} -m pytest tests/ -x -q --tb=no",
                },
            )

        agents = auto_research.list_agents()
        for agent_name in agents:
            state = auto_research.inspect(agent_name)
            if state.get("paused"):
                continue
            result = auto_research.run_loop(agent_name, max_experiments=5)
            observe.emit(
                "self_improve_agent_done",
                payload={
                    "agent": agent_name,
                    "experiments_run": result.experiments_run,
                    "paused": result.paused,
                    "reason": result.reason,
                    "last_metric": result.last_metric,
                },
            )
        observe.emit("self_improve_complete", payload={"agents_run": len(agents)})

    def _morning_brief_handler() -> None:
        agents = auto_research.list_agents()
        agent_summaries = []
        for name in agents:
            try:
                state = auto_research.inspect(name)
            except FileNotFoundError:
                continue
            agent_summaries.append(
                {
                    "name": name,
                    "paused": state.get("paused", False),
                    "experiments_today": state.get("experiments_today", 0),
                    "last_metric": state.get("last_verified_state", {}).get("metric"),
                }
            )
        observe.emit(
            "morning_brief",
            payload={
                "metrics_summary": metrics.snapshot(),
                "agents": agent_summaries,
                "total_agents": len(agents),
            },
        )

    def _daily_metrics_handler() -> None:
        observe.emit("daily_metrics", payload={"metrics": metrics.snapshot()})
        for name in auto_research.list_agents():
            try:
                state = auto_research.inspect(name)
            except FileNotFoundError:
                continue
            state["experiments_today"] = 0
            agent_store.save_state(name, state)

    def _perf_optimizer_handler() -> None:
        agent_name = "perf-optimizer"
        if not agent_store.state_path(agent_name).exists():
            auto_research.create_agent(
                AgentDefinition(
                    name=agent_name,
                    agent_class="operator",
                    instruction=(
                        "Optimize Claw's wiki and response quality. "
                        "Measure wiki search hit rate, embedding coverage, "
                        "and confidence distribution. Experiment with decay rates, "
                        "confidence thresholds, and category coverage."
                    ),
                    lane="worker",
                ),
                state={
                    "promote_on_improvement": True,
                    "commit_on_promotion": True,
                    "metric_command": (
                        f"PYTHONPATH=. {sys.executable} -c \""
                        "from claw_v2.wiki import WikiService; from claw_v2.llm import LLMRouter; "
                        "w=WikiService(router=LLMRouter.__new__(LLMRouter)); "
                        "e=w._embeddings; print(len(e))\""
                    ),
                },
            )
        state = auto_research.inspect(agent_name)
        if state.get("paused"):
            observe.emit(
                "perf_optimizer_skipped",
                payload={
                    "agent": agent_name,
                    "reason": state.get("pause_reason") or state.get("last_action") or "paused",
                    "last_error": state.get("last_error", ""),
                    "consecutive_failures": state.get("consecutive_failures", 0),
                },
            )
            return
        result = auto_research.run_loop(agent_name, max_experiments=3)
        if result.paused:
            observe.emit(
                "perf_optimizer_paused",
                payload={
                    "experiments": result.experiments_run,
                    "reason": result.reason,
                    "metric": result.last_metric,
                },
            )
            return
        observe.emit(
            "perf_optimizer_done",
            payload={
                "experiments": result.experiments_run,
                "metric": result.last_metric,
                "reason": result.reason,
            },
        )

    scheduler.register(ScheduledJob(name="heartbeat", interval_seconds=config.heartbeat_interval, handler=heartbeat.emit))
    scheduler.register(ScheduledJob(name="kairos_tick", interval_seconds=600, handler=kairos.tick))
    scheduler.register(ScheduledJob(name="buddy_tick", interval_seconds=600, handler=lambda: buddy.tick(observe)))
    if config.eval_on_self_improve:
        scheduler.register(
            ScheduledJob(
                name="self_improve",
                interval_seconds=86400,
                handler=_wrap_job_handler(
                    name="self_improve",
                    observe=observe,
                    handler=_self_improve_handler,
                    skip_if=_skip_maintenance_or("git", "pytest"),
                ),
            )
        )
    scheduler.register(ScheduledJob(name="morning_brief", interval_seconds=86400, handler=_morning_brief_handler))
    scheduler.register(ScheduledJob(name="daily_metrics", interval_seconds=86400, handler=_daily_metrics_handler))

    dream = AutoDreamService(memory=memory, observe=observe, router=router)
    scheduler.register(
        ScheduledJob(
            name="auto_dream",
            interval_seconds=86400,
            handler=_wrap_job_handler(
                name="auto_dream",
                observe=observe,
                handler=dream.run,
                skip_if=_maintenance_skip,
            ),
        )
    )
    scheduler.register(ScheduledJob(name="learning_consolidate", interval_seconds=86400, handler=learning.consolidate))
    scheduler.register(
        ScheduledJob(
            name="learning_soul_suggestions",
            interval_seconds=86400,
            handler=_wrap_job_handler(
                name="learning_soul_suggestions",
                observe=observe,
                handler=lambda: learning.suggest_soul_updates(observe=observe, soul_text=system_prompt),
                skip_if=_maintenance_skip,
            ),
        )
    )

    wiki = WikiService(router=router, observe=observe)
    bot.wiki = wiki
    kairos.wiki = wiki
    scheduler.register(ScheduledJob(name="wiki_lint", interval_seconds=86400, handler=wiki.lint))
    scheduler.register(ScheduledJob(name="wiki_confidence", interval_seconds=604800, handler=wiki.recompute_confidence))
    scheduler.register(
        ScheduledJob(
            name="wiki_research",
            interval_seconds=43200,
            handler=_wrap_job_handler(
                name="wiki_research", observe=observe, handler=wiki.auto_research, skip_if=_maintenance_skip,
            ),
        )
    )
    scheduler.register(
        ScheduledJob(
            name="wiki_scrape",
            interval_seconds=43200,
            handler=_wrap_job_handler(
                name="wiki_scrape", observe=observe, handler=wiki.auto_scrape_sources, skip_if=_maintenance_skip,
            ),
        )
    )

    _register_site_monitor_jobs(scheduler=scheduler, observe=observe, sites=config.monitored_sites)

    skill_registry = SkillRegistry(router=router)
    a2a = A2AService(router=router)
    kairos.skill_registry = skill_registry
    kairos.a2a = a2a

    scheduler.register(
        ScheduledJob(
            name="skill_expand",
            interval_seconds=86400,
            handler=_wrap_job_handler(
                name="skill_expand",
                observe=observe,
                handler=skill_registry.auto_expand,
                skip_if=_maintenance_skip,
            ),
        )
    )
    scheduler.register(
        ScheduledJob(
            name="a2a_process_inbox",
            interval_seconds=600,
            handler=_wrap_job_handler(name="a2a_process_inbox", observe=observe, handler=a2a.process_inbox),
        )
    )

    scheduler.register(
        ScheduledJob(
            name="perf_optimizer",
            interval_seconds=86400,
            handler=_wrap_job_handler(
                name="perf_optimizer",
                observe=observe,
                handler=_perf_optimizer_handler,
                skip_if=_skip_maintenance_or("git"),
            ),
        )
    )
    scheduler.register(ScheduledJob(name="pipeline_poll", interval_seconds=300, handler=pipeline.poll_actionable))
    scheduler.register(ScheduledJob(name="pipeline_poll_merges", interval_seconds=300, handler=pipeline.poll_merges))
    _register_sub_agent_jobs(
        scheduler=scheduler,
        observe=observe,
        sub_agents=sub_agents,
        scheduled_jobs=config.scheduled_sub_agents,
    )
    scheduler.register(
        ScheduledJob(
            name="task_board_cleanup",
            interval_seconds=86400,
            handler=lambda: task_board.cleanup(max_age_seconds=86400 * 7),
        )
    )
    scheduler.restore()
    return scheduler, dream, wiki, skill_registry, a2a


def build_runtime(
    system_prompt: str = "You are Claw.",
    *,
    anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None,
    openai_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    google_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    ollama_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    codex_transport: Callable[[LLMRequest], LLMResponse] | None = None,
) -> ClawRuntime:
    config = AppConfig.from_env()
    config.validate()
    config.ensure_directories()

    memory, observe, metrics, approvals, bus, agent_store = _setup_core_state(config)
    task_ledger = TaskLedger(config.db_path, observe=observe)
    task_ledger.reconcile_false_successes()
    job_service = JobService(config.db_path, observe=observe)
    model_registry = ModelRegistry.default()
    startup_health = _run_startup_healthchecks(config, observe)
    agent_workspace = AgentWorkspace(config.workspace_root, template_root=Path(__file__).parent)
    workspace_bootstrap = agent_workspace.ensure()
    observe.emit("agent_workspace_bootstrap", payload=workspace_bootstrap.to_dict())
    system_prompt = agent_workspace.system_prompt(fallback=system_prompt)
    router, learning, brain, tool_registry, openai_tool_executor = _setup_llm_stack(
        config=config,
        memory=memory,
        observe=observe,
        metrics=metrics,
        approvals=approvals,
        system_prompt=system_prompt,
        anthropic_executor=anthropic_executor,
        openai_transport=openai_transport,
        google_transport=google_transport,
        ollama_transport=ollama_transport,
        codex_transport=codex_transport,
    )
    auto_research, sub_agents, coordinator, task_board, heartbeat, kairos, buddy = _setup_agent_services(
        config=config,
        router=router,
        memory=memory,
        observe=observe,
        approvals=approvals,
        bus=bus,
        agent_store=agent_store,
        metrics=metrics,
        brain=brain,
    )
    daemon, bot, pipeline, _, _, _ = _setup_operational_services(
        config=config,
        router=router,
        memory=memory,
        observe=observe,
        approvals=approvals,
        heartbeat=heartbeat,
        brain=brain,
        auto_research=auto_research,
        sub_agents=sub_agents,
        coordinator=coordinator,
        task_board=task_board,
        task_ledger=task_ledger,
        job_service=job_service,
        model_registry=model_registry,
        buddy=buddy,
        learning=learning,
        kairos=kairos,
        startup_health=startup_health,
    )
    scheduler, _, wiki, skill_registry, a2a = _setup_scheduler(
        config=config,
        system_prompt=system_prompt,
        memory=memory,
        observe=observe,
        metrics=metrics,
        heartbeat=heartbeat,
        kairos=kairos,
        buddy=buddy,
        auto_research=auto_research,
        agent_store=agent_store,
        learning=learning,
        router=router,
        task_board=task_board,
        sub_agents=sub_agents,
        bot=bot,
        pipeline=pipeline,
        startup_health=startup_health,
    )
    daemon.scheduler = scheduler
    brain.wiki = wiki
    agent_runtime = AgentRuntime(bot_service=bot, memory=memory, observe=observe)
    resumed_tasks = bot.resume_interrupted_tasks()
    if resumed_tasks:
        observe.emit("autonomous_task_recovery_bootstrap", payload={"resumed_tasks": resumed_tasks})

    return ClawRuntime(
        config=config,
        memory=memory,
        observe=observe,
        metrics=metrics,
        approvals=approvals,
        bus=bus,
        agent_store=agent_store,
        router=router,
        brain=brain,
        auto_research=auto_research,
        sub_agents=sub_agents,
        coordinator=coordinator,
        task_board=task_board,
        kairos=kairos,
        buddy=buddy,
        heartbeat=heartbeat,
        scheduler=scheduler,
        daemon=daemon,
        bot=bot,
        agent_runtime=agent_runtime,
        agent_workspace=agent_workspace,
        task_ledger=task_ledger,
        job_service=job_service,
        model_registry=model_registry,
        skill_registry=skill_registry,
        a2a=a2a,
        startup_health=startup_health,
        tool_registry=tool_registry,
        openai_tool_executor=openai_tool_executor,
    )


def main() -> int:
    import asyncio

    from claw_v2.lifecycle import run

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
