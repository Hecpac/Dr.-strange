from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.adapters.base import LLMRequest
from claw_v2.agents import (
    AgentDefinition,
    AutoResearchAgentService,
    FileAgentStore,
    GitBranchPromotionExecutor,
    GitWorktreeExperimentRunner,
    SubAgentService,
)
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.bot import BotService
from claw_v2.browser import DevBrowserService
from claw_v2.config import AppConfig
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.hooks import make_anti_distillation_hook, make_daily_cost_gate, make_decision_logger
from claw_v2.linear import LinearService
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.metrics import MetricsTracker
from claw_v2.observe import ObserveStream
from claw_v2.pipeline import PipelineService
from claw_v2.computer import ComputerUseService, BrowserUseService, CodexComputerBackend
from claw_v2.coordinator import CoordinatorService
from claw_v2.dream import AutoDreamService
from claw_v2.buddy import BuddyService
from claw_v2.kairos import KairosService
from claw_v2.terminal_bridge import TerminalBridgeService
from claw_v2.types import LLMResponse


@dataclass(slots=True)
class ClawRuntime:
    config: AppConfig
    memory: MemoryStore
    observe: ObserveStream
    metrics: MetricsTracker
    approvals: ApprovalManager
    agent_store: FileAgentStore
    router: LLMRouter
    brain: BrainService
    auto_research: AutoResearchAgentService
    sub_agents: SubAgentService
    coordinator: CoordinatorService
    kairos: KairosService
    buddy: BuddyService
    heartbeat: HeartbeatService
    scheduler: CronScheduler
    daemon: ClawDaemon
    bot: BotService


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


def _is_git_repo(path: str) -> bool:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


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
    memory = MemoryStore(config.db_path)
    observe = ObserveStream(config.db_path)
    metrics = MetricsTracker()
    approvals = ApprovalManager(config.approvals_root, config.approval_secret)
    agent_store = FileAgentStore(config.agent_state_root)

    def audit_sink(event: dict) -> None:
        observe.emit(
            event["action"],
            lane=event["lane"],
            provider=event["provider"],
            model=event["model"],
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

    pre_hooks: list = [make_daily_cost_gate(observe, config.daily_cost_limit)] if config.daily_cost_limit > 0 else []
    pre_hooks.append(make_anti_distillation_hook())
    post_hooks = [make_decision_logger(observe)]

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
    )
    from claw_v2.learning import LearningLoop
    learning = LearningLoop(memory=memory, router=router)
    brain = BrainService(
        router=router,
        memory=memory,
        system_prompt=system_prompt,
        approvals=approvals,
        observe=observe,
        learning=learning,
    )
    experiment_runner = _noop_experiment_runner
    if _is_git_repo(str(config.workspace_root)):
        experiment_runner = GitWorktreeExperimentRunner(
            repo_root=config.workspace_root,
            worktree_root=config.agent_state_root / "_worktrees",
            router=router,
            brain=brain,
            promotion_executor=GitBranchPromotionExecutor(config.workspace_root),
        )
    auto_research = AutoResearchAgentService(router=router, store=agent_store, experiment_runner=experiment_runner)
    sub_agents = SubAgentService(config.agent_definitions_root, router, agent_store)
    discovered = sub_agents.discover()
    if discovered:
        observe.emit("sub_agents_discovered", payload={"agents": discovered})
    coordinator = CoordinatorService(
        router=router,
        observe=observe,
        scratch_root=config.agent_state_root / "_scratch",
    )
    heartbeat = HeartbeatService(metrics=metrics, approvals=approvals, agent_store=agent_store, observe=observe)
    kairos = KairosService(router=router, heartbeat=heartbeat, observe=observe)
    buddy = BuddyService(config.db_path.parent / "buddy.db")

    def _self_improve_handler() -> None:
        """Daily self-improvement: run tests, then AutoResearch loop on all active agents."""
        import subprocess as _sp

        import shutil as _sh
        import sys as _sys

        observe.emit("self_improve_start", payload={})
        # Gate: run tests first
        repo_root = config.pipeline_repo_root or config.workspace_root
        pytest_bin = str(repo_root / ".venv" / "bin" / "pytest")
        from pathlib import Path as _Path
        if not _sh.which(pytest_bin) and not _Path(pytest_bin).exists():
            pytest_bin = _sys.executable
            pytest_args = [pytest_bin, "-m", "pytest", "tests/", "-x", "-q", "--tb=no"]
        else:
            pytest_args = [pytest_bin, "tests/", "-x", "-q", "--tb=no"]
        test_result = _sp.run(
            pytest_args,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(repo_root),
        )
        if test_result.returncode != 0:
            observe.emit("self_improve_blocked", payload={"reason": "tests_failed", "output": (test_result.stdout or "")[-500:]})
            return
        # Ensure default agent exists
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
                    "metric_command": f"PYTHONPATH=. {_sys.executable} -m pytest tests/ -x -q --tb=no 2>&1 | tail -1 | awk '{{print $1}}'",
                },
            )
        # Run loop on all non-paused agents
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
        """Daily morning brief: summarize overnight metrics and agent status."""
        agents = auto_research.list_agents()
        agent_summaries = []
        for name in agents:
            try:
                state = auto_research.inspect(name)
                agent_summaries.append({
                    "name": name,
                    "paused": state.get("paused", False),
                    "experiments_today": state.get("experiments_today", 0),
                    "last_metric": state.get("last_verified_state", {}).get("metric"),
                })
            except FileNotFoundError:
                continue
        observe.emit(
            "morning_brief",
            payload={
                "metrics_summary": metrics.snapshot(),
                "agents": agent_summaries,
                "total_agents": len(agents),
            },
        )

    def _daily_metrics_handler() -> None:
        """Daily metrics: persist current metrics snapshot."""
        observe.emit("daily_metrics", payload={"metrics": metrics.snapshot()})
        # Reset daily counters on agents
        for name in auto_research.list_agents():
            try:
                state = auto_research.inspect(name)
                state["experiments_today"] = 0
                agent_store.save_state(name, state)
            except FileNotFoundError:
                continue

    scheduler = CronScheduler(persistence=memory)
    scheduler.register(ScheduledJob(name="heartbeat", interval_seconds=config.heartbeat_interval, handler=heartbeat.emit))
    scheduler.register(ScheduledJob(name="kairos_tick", interval_seconds=600, handler=kairos.tick))
    scheduler.register(ScheduledJob(name="buddy_tick", interval_seconds=600, handler=lambda: buddy.tick(observe)))
    if config.eval_on_self_improve:
        scheduler.register(ScheduledJob(name="self_improve", interval_seconds=86400, handler=_self_improve_handler))
    scheduler.register(ScheduledJob(name="morning_brief", interval_seconds=86400, handler=_morning_brief_handler))
    scheduler.register(ScheduledJob(name="daily_metrics", interval_seconds=86400, handler=_daily_metrics_handler))
    dream = AutoDreamService(memory=memory, observe=observe, router=router)
    scheduler.register(ScheduledJob(name="auto_dream", interval_seconds=86400, handler=dream.run))
    scheduler.register(ScheduledJob(name="learning_consolidate", interval_seconds=86400, handler=learning.consolidate))
    from claw_v2.wiki import WikiService
    wiki = WikiService(router=router)
    scheduler.register(ScheduledJob(name="wiki_lint", interval_seconds=86400, handler=wiki.lint))
    daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
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
        pull_requests=GitHubPullRequestService(config.workspace_root) if _is_git_repo(str(config.workspace_root)) else None,
        allowed_user_id=config.telegram_allowed_user_id,
        config=config,
        browser=browser,
        terminal_bridge=terminal_bridge,
        computer=computer,
        browser_use=browser_use,
        observe=observe,
    )
    bot.wiki = wiki
    brain.wiki = wiki
    if config.linear_api_key:
        from claw_v2.linear import build_linear_api_caller
        linear = LinearService(mcp_caller=build_linear_api_caller(config.linear_api_key))
    else:
        linear = LinearService(mcp_caller=lambda action, **kw: None)
    pipeline = PipelineService(
        linear=linear,
        router=router,
        approvals=approvals,
        pull_requests=GitHubPullRequestService(config.workspace_root) if _is_git_repo(str(config.workspace_root)) else None,
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
    scheduler.register(
        ScheduledJob(name="pipeline_poll", interval_seconds=300, handler=pipeline.poll_actionable)
    )
    scheduler.register(
        ScheduledJob(name="pipeline_poll_merges", interval_seconds=300, handler=pipeline.poll_merges)
    )
    # Sub-agent scheduled jobs
    def _sub_agent_handler(agent: str, skill: str) -> None:
        result = sub_agents.run_skill(agent, skill, lane="worker")
        observe.emit("sub_agent_skill", payload={"agent": agent, "skill": skill, "result": result})

    if sub_agents.get_agent("alma"):
        scheduler.register(ScheduledJob(
            name="alma_morning_brief", interval_seconds=86400,
            handler=lambda: _sub_agent_handler("alma", "daily-brief"),
        ))
    if sub_agents.get_agent("lux"):
        scheduler.register(ScheduledJob(
            name="lux_content_radar", interval_seconds=43200,
            handler=lambda: _sub_agent_handler("lux", "content-radar"),
        ))
    if sub_agents.get_agent("hex"):
        scheduler.register(ScheduledJob(
            name="hex_bug_triage", interval_seconds=86400,
            handler=lambda: _sub_agent_handler("hex", "bug-triage"),
        ))
    if sub_agents.get_agent("rook"):
        scheduler.register(ScheduledJob(
            name="rook_health_audit", interval_seconds=21600,
            handler=lambda: _sub_agent_handler("rook", "health-audit"),
        ))
    scheduler.restore()
    from claw_v2.content import ContentEngine
    from claw_v2.social import SocialPublisher

    content_engine = ContentEngine(router=router, accounts_root=config.social_accounts_root)
    bot.content_engine = content_engine
    bot.social_publisher = SocialPublisher(adapters={})
    return ClawRuntime(
        config=config,
        memory=memory,
        observe=observe,
        metrics=metrics,
        approvals=approvals,
        agent_store=agent_store,
        router=router,
        brain=brain,
        auto_research=auto_research,
        sub_agents=sub_agents,
        coordinator=coordinator,
        kairos=kairos,
        buddy=buddy,
        heartbeat=heartbeat,
        scheduler=scheduler,
        daemon=daemon,
        bot=bot,
    )


def main() -> int:
    import asyncio

    from claw_v2.lifecycle import run

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
