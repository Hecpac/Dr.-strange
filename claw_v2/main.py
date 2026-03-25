from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.adapters.base import LLMRequest
from claw_v2.agents import (
    AutoResearchAgentService,
    FileAgentStore,
    GitBranchPromotionExecutor,
    GitWorktreeExperimentRunner,
)
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.bot import BotService
from claw_v2.config import AppConfig
from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.metrics import MetricsTracker
from claw_v2.observe import ObserveStream
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

    router = LLMRouter.default(
        config,
        anthropic_executor=anthropic_executor,
        openai_transport=openai_transport,
        google_transport=google_transport,
        audit_sink=audit_sink,
    )
    brain = BrainService(
        router=router,
        memory=memory,
        system_prompt=system_prompt,
        approvals=approvals,
        observe=observe,
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
    heartbeat = HeartbeatService(metrics=metrics, approvals=approvals, agent_store=agent_store, observe=observe)
    scheduler = CronScheduler()
    scheduler.register(ScheduledJob(name="heartbeat", interval_seconds=config.heartbeat_interval, handler=heartbeat.emit))
    scheduler.register(
        ScheduledJob(
            name="daily_eval_gate",
            interval_seconds=86400,
            handler=lambda: observe.emit(
                "daily_eval_gate",
                payload={"enabled": config.eval_on_self_improve},
            ),
        )
    )
    daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
    bot = BotService(
        brain=brain,
        auto_research=auto_research,
        heartbeat=heartbeat,
        approvals=approvals,
        pull_requests=GitHubPullRequestService(config.workspace_root) if _is_git_repo(str(config.workspace_root)) else None,
        allowed_user_id=config.telegram_allowed_user_id,
    )
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
