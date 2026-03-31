from __future__ import annotations

import csv
import filecmp
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claw_v2.brain import BrainService
from claw_v2.llm import LLMRouter
from claw_v2.tools import default_allowed_tools_for, is_valid_agent_class

_UNSET = object()


@dataclass(slots=True)
class AgentDefinition:
    name: str
    agent_class: str
    instruction: str
    lane: str = "worker"
    allowed_tools: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExperimentRecord:
    experiment_number: int
    metric_value: float
    baseline_value: float
    status: str
    cost_usd: float = 0.0
    promotion_commit_sha: str | None = None
    promotion_branch_name: str | None = None


@dataclass(slots=True)
class ExperimentEvaluation:
    metric_value: float
    status: str
    output: str


@dataclass(slots=True)
class PromotionManifest:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def paths(self) -> list[str]:
        return [*self.added, *self.modified, *self.deleted]


@dataclass(slots=True)
class PromotionResult:
    manifest: PromotionManifest
    applied_files: int
    deleted_files: int
    commit_created: bool = False
    commit_sha: str | None = None
    commit_message: str | None = None
    branch_created: bool = False
    branch_name: str | None = None


@dataclass(slots=True)
class LoopResult:
    experiments_run: int
    paused: bool
    reason: str
    last_metric: float | None = None


@dataclass(slots=True)
class AgentStatus:
    trust_level: int
    experiments_today: int
    last_metric: float | None
    paused: bool


@dataclass(slots=True)
class StagnationDetector:
    no_improvement_streak: int = 10
    revert_ratio_max: float = 0.8
    baseline_min_experiments: int = 15

    def evaluate(self, history: list[ExperimentRecord]) -> str:
        if len(history) < self.baseline_min_experiments:
            return "cold_start"
        recent = history[-self.no_improvement_streak :]
        if all(item.metric_value <= item.baseline_value for item in recent):
            return "stagnating"
        reverted = sum(1 for item in history[-20:] if item.status == "regressed")
        if history[-20:] and reverted / len(history[-20:]) > self.revert_ratio_max:
            return "stagnating"
        return "healthy"


class FileAgentStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def state_path(self, agent_name: str) -> Path:
        return self.root / agent_name / "state.json"

    def results_path(self, agent_name: str) -> Path:
        return self.root / agent_name / "results.tsv"

    def list_agents(self) -> list[str]:
        return sorted(
            path.name
            for path in self.root.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        )

    def load_state(self, agent_name: str) -> dict:
        path = self.state_path(agent_name)
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, agent_name: str, state: dict) -> None:
        path = self.state_path(agent_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def append_result(self, agent_name: str, record: ExperimentRecord) -> None:
        path = self.results_path(agent_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            if write_header:
                writer.writerow(
                    [
                        "experiment_number",
                        "metric_value",
                        "baseline_value",
                        "status",
                        "cost_usd",
                        "promotion_commit_sha",
                        "promotion_branch_name",
                    ]
                )
            writer.writerow(
                [
                    record.experiment_number,
                    record.metric_value,
                    record.baseline_value,
                    record.status,
                    record.cost_usd,
                    record.promotion_commit_sha or "",
                    record.promotion_branch_name or "",
                ]
            )

    def load_history(self, agent_name: str) -> list[ExperimentRecord]:
        path = self.results_path(agent_name)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            return [
                ExperimentRecord(
                    experiment_number=int(row["experiment_number"]),
                    metric_value=float(row["metric_value"]),
                    baseline_value=float(row["baseline_value"]),
                    status=row["status"],
                    cost_usd=float(row["cost_usd"]),
                    promotion_commit_sha=row.get("promotion_commit_sha") or None,
                    promotion_branch_name=row.get("promotion_branch_name") or None,
                )
                for row in reader
            ]


class AutoResearchAgentService:
    def __init__(
        self,
        router: LLMRouter,
        store: FileAgentStore,
        experiment_runner: Callable[[str, int, dict], ExperimentRecord],
        detector: StagnationDetector | None = None,
    ) -> None:
        self.router = router
        self.store = store
        self.experiment_runner = experiment_runner
        self.detector = detector or StagnationDetector()

    def create_agent(
        self,
        definition: AgentDefinition,
        *,
        state: dict | None = None,
    ) -> dict:
        _validate_agent_name(definition.name)
        if not is_valid_agent_class(definition.agent_class):
            raise ValueError("agent_class must be one of: researcher, operator, deployer")
        if not definition.instruction.strip():
            raise ValueError("instruction must not be empty")
        if self.store.state_path(definition.name).exists():
            raise FileExistsError(f"agent already exists: {definition.name}")

        allowed_tools = self._normalize_allowed_tools(definition.agent_class, definition.allowed_tools)
        payload = {
            "name": definition.name,
            "agent_class": definition.agent_class,
            "instruction": definition.instruction,
            "lane": definition.lane,
            "allowed_tools": allowed_tools,
            "promote_on_improvement": False,
            "commit_on_promotion": False,
            "branch_on_promotion": False,
            "trust_level": 1,
            "experiments_today": 0,
            "paused": False,
            "last_verified_state": {"metric": None},
        }
        if state:
            payload.update(state)
        self.store.save_state(definition.name, payload)
        return payload

    def dispatch(self, agent_name: str, instruction: str) -> str:
        state = self.store.load_state(agent_name)
        response = self.router.ask(
            instruction,
            lane="worker",
            evidence_pack={"agent_name": agent_name, "state": state},
        )
        return response.content

    def run_loop(self, agent_name: str, max_experiments: int) -> LoopResult:
        state = self.store.load_state(agent_name)
        state["paused"] = False
        self.store.save_state(agent_name, state)
        history = self.store.load_history(agent_name)
        last_metric = state.get("last_verified_state", {}).get("metric")
        for experiment_number in range(1, max_experiments + 1):
            record = self.experiment_runner(agent_name, experiment_number, state)
            self.store.append_result(agent_name, record)
            history.append(record)
            last_metric = record.metric_value
            state["experiments_today"] = state.get("experiments_today", 0) + 1
            state["last_verified_state"] = {"metric": record.metric_value}
            self.store.save_state(agent_name, state)
            stagnation = self.detector.evaluate(history)
            if stagnation == "stagnating":
                state["paused"] = True
                self.store.save_state(agent_name, state)
                return LoopResult(experiment_number, True, "stagnating", record.metric_value)
        return LoopResult(max_experiments, False, "completed", last_metric)

    def run_until(self, agent_name: str, *, max_experiments: int, target_metric: float) -> LoopResult:
        state = self.store.load_state(agent_name)
        state["paused"] = False
        self.store.save_state(agent_name, state)
        history = self.store.load_history(agent_name)
        last_metric = state.get("last_verified_state", {}).get("metric")
        for experiment_number in range(1, max_experiments + 1):
            record = self.experiment_runner(agent_name, experiment_number, state)
            self.store.append_result(agent_name, record)
            history.append(record)
            last_metric = record.metric_value
            state["experiments_today"] = state.get("experiments_today", 0) + 1
            state["last_verified_state"] = {"metric": record.metric_value}
            self.store.save_state(agent_name, state)
            if record.metric_value >= target_metric:
                return LoopResult(experiment_number, False, "target_reached", record.metric_value)
            stagnation = self.detector.evaluate(history)
            if stagnation == "stagnating":
                state["paused"] = True
                self.store.save_state(agent_name, state)
                return LoopResult(experiment_number, True, "stagnating", record.metric_value)
        return LoopResult(max_experiments, False, "budget_exhausted", last_metric)

    def status(self, agent_name: str) -> AgentStatus:
        state = self.store.load_state(agent_name)
        metric = state.get("last_verified_state", {}).get("metric")
        return AgentStatus(
            trust_level=state.get("trust_level", 1),
            experiments_today=state.get("experiments_today", 0),
            last_metric=metric,
            paused=state.get("paused", False),
        )

    def list_agents(self) -> list[str]:
        return self.store.list_agents()

    def inspect(self, agent_name: str) -> dict:
        return self.store.load_state(agent_name)

    def history(self, agent_name: str, *, limit: int | None = None) -> list[ExperimentRecord]:
        history = self.store.load_history(agent_name)
        if limit is None:
            return history
        return history[-limit:]

    def latest_result(self, agent_name: str) -> ExperimentRecord | None:
        history = self.store.load_history(agent_name)
        if not history:
            return None
        return history[-1]

    def pause(self, agent_name: str) -> dict:
        state = self.store.load_state(agent_name)
        state["paused"] = True
        self.store.save_state(agent_name, state)
        return state

    def resume(self, agent_name: str) -> dict:
        state = self.store.load_state(agent_name)
        state["paused"] = False
        self.store.save_state(agent_name, state)
        return state

    def update_controls(
        self,
        agent_name: str,
        *,
        promote_on_improvement: bool | None = None,
        commit_on_promotion: bool | None = None,
        branch_on_promotion: bool | None = None,
        promotion_commit_message: str | object = _UNSET,
        promotion_branch_name: str | object = _UNSET,
    ) -> dict:
        state = self.store.load_state(agent_name)
        if promote_on_improvement is not None:
            state["promote_on_improvement"] = promote_on_improvement
        if commit_on_promotion is not None:
            state["commit_on_promotion"] = commit_on_promotion
        if branch_on_promotion is not None:
            state["branch_on_promotion"] = branch_on_promotion
        if promotion_commit_message is not _UNSET:
            if str(promotion_commit_message).strip():
                state["promotion_commit_message"] = str(promotion_commit_message).strip()
            else:
                state.pop("promotion_commit_message", None)
        if promotion_branch_name is not _UNSET:
            if str(promotion_branch_name).strip():
                branch_name = str(promotion_branch_name).strip()
                _validate_branch_name(branch_name)
                state["promotion_branch_name"] = branch_name
            else:
                state.pop("promotion_branch_name", None)
        self.store.save_state(agent_name, state)
        return state

    @staticmethod
    def _normalize_allowed_tools(agent_class: str, requested_tools: list[str]) -> list[str]:
        default_tools = default_allowed_tools_for(agent_class)
        if not requested_tools:
            return default_tools
        invalid = sorted(set(requested_tools) - set(default_tools))
        if invalid:
            raise ValueError(
                f"allowed_tools contain entries not permitted for {agent_class}: {', '.join(invalid)}"
            )
        return sorted(dict.fromkeys(requested_tools))


class DockerSandbox:
    """Wraps command execution in a Docker container with resource limits."""

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        memory_limit: str = "2g",
        pids_limit: int = 100,
        network: str = "none",
        timeout: int = 300,
    ) -> None:
        self.image = image
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.network = network
        self.timeout = timeout
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                result = subprocess.run(
                    ["docker", "info"], capture_output=True, text=True, check=False, timeout=5,
                )
                self._available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                self._available = False
        return self._available

    def run(self, command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        docker_cmd = [
            "docker", "run", "--rm",
            f"--memory={self.memory_limit}",
            f"--pids-limit={self.pids_limit}",
            f"--network={self.network}",
            "-v", f"{cwd}:/workspace",
            "-w", "/workspace",
            self.image,
            "sh", "-c", command,
        ]
        return subprocess.run(
            docker_cmd, capture_output=True, text=True, check=False, timeout=self.timeout,
        )


class GitWorktreeExperimentRunner:
    def __init__(
        self,
        *,
        repo_root: Path | str,
        worktree_root: Path | str,
        router: LLMRouter,
        brain: BrainService | None = None,
        evaluator: Callable[[Path, dict, str], ExperimentEvaluation] | None = None,
        promotion_executor: Callable[[Path, dict, str], Any] | None = None,
        docker_sandbox: DockerSandbox | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.worktree_root = Path(worktree_root)
        self.router = router
        self.brain = brain
        self.evaluator = evaluator
        self.promotion_executor = promotion_executor or WorkspacePromotionExecutor(self.repo_root)
        self.docker_sandbox = docker_sandbox or DockerSandbox()
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    def __call__(self, agent_name: str, experiment_number: int, state: dict) -> ExperimentRecord:
        worktree_path = self.worktree_root / agent_name / f"exp-{experiment_number}"
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        workspace_mode = self._prepare_workspace(worktree_path)
        try:
            response = self.router.ask(
                self._build_worker_prompt(agent_name, experiment_number, state),
                system_prompt="You are a careful coding worker operating inside a disposable git worktree.",
                lane="worker",
                allowed_tools=state.get("allowed_tools") or None,
                evidence_pack={
                    "agent_name": agent_name,
                    "experiment_number": experiment_number,
                    "state": state,
                    "worktree_path": str(worktree_path),
                },
                cwd=str(worktree_path),
            )
            diff = self._collect_diff(worktree_path, workspace_mode)
            evaluation = self._evaluate(worktree_path, state, diff)
            status = evaluation.status
            promotion_commit_sha: str | None = None
            promotion_branch_name: str | None = None

            if (
                self.brain is not None
                and self.promotion_executor is not None
                and state.get("promote_on_improvement")
                and evaluation.metric_value > self._baseline(state)
            ):
                promotion_state = {**state, "_workspace_mode": workspace_mode}
                execution = self.brain.execute_critical_action(
                    action=f"promote_{agent_name}",
                    plan=response.content,
                    diff=diff,
                    test_output=evaluation.output,
                    executor=lambda: self.promotion_executor(worktree_path, promotion_state, diff),
                )
                status = execution.status
                promotion_result = execution.result
                promotion_commit_sha = getattr(promotion_result, "commit_sha", None)
                promotion_branch_name = getattr(promotion_result, "branch_name", None)

            return ExperimentRecord(
                experiment_number=experiment_number,
                metric_value=evaluation.metric_value,
                baseline_value=self._baseline(state),
                status=status,
                cost_usd=response.cost_estimate,
                promotion_commit_sha=promotion_commit_sha,
                promotion_branch_name=promotion_branch_name,
            )
        finally:
            self._remove_workspace(worktree_path, workspace_mode)

    def _build_worker_prompt(self, agent_name: str, experiment_number: int, state: dict) -> str:
        instruction = state.get("instruction", "").strip()
        return (
            f"Run experiment {experiment_number} for agent '{agent_name}'.\n\n"
            f"Goal:\n{instruction or 'No instruction provided.'}\n\n"
            "Rules:\n"
            "- Work only inside the provided worktree path.\n"
            "- Make at most one incremental change.\n"
            "- Leave a clean, testable diff.\n"
            "- Summarize what changed and any residual risk."
        )

    def _evaluate(self, worktree_path: Path, state: dict, diff: str) -> ExperimentEvaluation:
        if self.evaluator is not None:
            return self.evaluator(worktree_path, state, diff)
        baseline = self._baseline(state)
        command = state.get("metric_command")
        if not command:
            status = "no_metric" if diff.strip() else "noop"
            return ExperimentEvaluation(metric_value=baseline, status=status, output="No metric command configured.")
        if self.docker_sandbox.is_available():
            try:
                completed = self.docker_sandbox.run(command, cwd=worktree_path)
            except subprocess.TimeoutExpired:
                return ExperimentEvaluation(metric_value=baseline, status="metric_failed", output="Docker timeout exceeded.")
        else:
            completed = subprocess.run(
                command, shell=True, cwd=worktree_path,
                capture_output=True, text=True, check=False, timeout=300,
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        metric_value = self._parse_metric(output, baseline)
        if completed.returncode != 0:
            return ExperimentEvaluation(metric_value=baseline, status="metric_failed", output=output)
        if metric_value > baseline:
            status = "improved"
        elif metric_value < baseline:
            status = "regressed"
        else:
            status = "no_change"
        return ExperimentEvaluation(metric_value=metric_value, status=status, output=output)

    def _prepare_workspace(self, worktree_path: Path) -> str:
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        if self._has_head_commit():
            subprocess.run(
                ["git", "-C", str(self.repo_root), "worktree", "add", "--detach", str(worktree_path), "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            return "git_worktree"
        shutil.copytree(
            self.repo_root,
            worktree_path,
            ignore=shutil.ignore_patterns(*IGNORED_WORKSPACE_NAMES),
        )
        return "snapshot"

    def _remove_workspace(self, worktree_path: Path, workspace_mode: str) -> None:
        if workspace_mode == "git_worktree":
            subprocess.run(
                ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(worktree_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    def _collect_diff(self, worktree_path: Path, workspace_mode: str) -> str:
        if workspace_mode == "git_worktree":
            return self._git(worktree_path, "diff", "--", ".")
        return self._snapshot_diff(worktree_path)

    @staticmethod
    def _parse_metric(output: str, fallback: float) -> float:
        match = re.search(r"-?\d+(?:\.\d+)?", output)
        if match is None:
            return fallback
        return float(match.group(0))

    @staticmethod
    def _baseline(state: dict) -> float:
        return float(state.get("last_verified_state", {}).get("metric") or 0.0)

    def _has_head_commit(self) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    def _snapshot_diff(self, worktree_path: Path) -> str:
        changed: list[str] = []
        original_files = {
            path.relative_to(self.repo_root): path
            for path in self.repo_root.rglob("*")
            if path.is_file() and not _should_ignore_path(path)
        }
        worktree_files = {
            path.relative_to(worktree_path): path
            for path in worktree_path.rglob("*")
            if path.is_file() and not _should_ignore_path(path)
        }
        for relative_path in sorted(set(original_files) | set(worktree_files)):
            original = original_files.get(relative_path)
            candidate = worktree_files.get(relative_path)
            if original is None:
                changed.append(f"ADDED {relative_path}")
                continue
            if candidate is None:
                changed.append(f"DELETED {relative_path}")
                continue
            if not filecmp.cmp(original, candidate, shallow=False):
                changed.append(f"MODIFIED {relative_path}")
        return "\n".join(changed)

    @staticmethod
    def _git(repo_path: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout


IGNORED_WORKSPACE_NAMES = (".git", ".venv", "__pycache__", ".pytest_cache")


class WorkspacePromotionExecutor:
    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)

    def __call__(self, worktree_path: Path, state: dict, diff: str) -> PromotionResult:
        manifest = self._select_manifest(worktree_path, state, diff)
        applied_files = 0
        deleted_files = 0

        for relative_path in [*manifest.added, *manifest.modified]:
            source = worktree_path / relative_path
            target = self.repo_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            applied_files += 1

        for relative_path in manifest.deleted:
            target = self.repo_root / relative_path
            if target.exists():
                target.unlink()
                deleted_files += 1

        return PromotionResult(
            manifest=manifest,
            applied_files=applied_files,
            deleted_files=deleted_files,
        )

    def _select_manifest(self, worktree_path: Path, state: dict, diff: str) -> PromotionManifest:
        workspace_mode = state.get("_workspace_mode")
        if workspace_mode == "git_worktree":
            return self.build_manifest_from_git_status(worktree_path)
        if workspace_mode == "snapshot":
            return self.build_manifest_from_snapshot_diff(diff)
        return self.build_manifest(self.repo_root, worktree_path)

    @classmethod
    def build_manifest(cls, repo_root: Path | str, worktree_path: Path | str) -> PromotionManifest:
        repo_root = Path(repo_root)
        worktree_path = Path(worktree_path)
        manifest = PromotionManifest()
        original_files = {
            path.relative_to(repo_root): path
            for path in repo_root.rglob("*")
            if path.is_file() and not _should_ignore_path(path)
        }
        worktree_files = {
            path.relative_to(worktree_path): path
            for path in worktree_path.rglob("*")
            if path.is_file() and not _should_ignore_path(path)
        }
        for relative_path in sorted(set(original_files) | set(worktree_files)):
            original = original_files.get(relative_path)
            candidate = worktree_files.get(relative_path)
            relative_text = str(relative_path)
            if original is None:
                manifest.added.append(relative_text)
                continue
            if candidate is None:
                manifest.deleted.append(relative_text)
                continue
            if not filecmp.cmp(original, candidate, shallow=False):
                manifest.modified.append(relative_text)
        return manifest

    @classmethod
    def build_manifest_from_snapshot_diff(cls, diff: str) -> PromotionManifest:
        manifest = PromotionManifest()
        for line in diff.splitlines():
            operation, _, relative_path = line.partition(" ")
            if not relative_path:
                continue
            if operation == "ADDED":
                manifest.added.append(relative_path)
            elif operation == "MODIFIED":
                manifest.modified.append(relative_path)
            elif operation == "DELETED":
                manifest.deleted.append(relative_path)
        return manifest

    @classmethod
    def build_manifest_from_git_status(cls, worktree_path: Path | str) -> PromotionManifest:
        worktree_path = Path(worktree_path)
        completed = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain", "--untracked-files=all", "--", "."],
            capture_output=True,
            text=True,
            check=True,
        )
        manifest = PromotionManifest()
        for raw_line in completed.stdout.splitlines():
            if not raw_line:
                continue
            status = raw_line[:2]
            relative_path = raw_line[3:]
            if status == "??":
                manifest.added.append(relative_path)
            elif "D" in status:
                manifest.deleted.append(relative_path)
            elif status.strip():
                manifest.modified.append(relative_path)
        return manifest


class GitCommitPromotionExecutor:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        apply_executor: WorkspacePromotionExecutor | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.apply_executor = apply_executor or WorkspacePromotionExecutor(self.repo_root)

    def __call__(self, worktree_path: Path, state: dict, diff: str) -> PromotionResult:
        result = self.apply_executor(worktree_path, state, diff)
        if not state.get("commit_on_promotion"):
            return result
        if not result.manifest.has_changes():
            return result

        paths = result.manifest.paths()
        message = self._build_commit_message(state)
        self._git("add", "--all", "--", *paths)
        try:
            if not self._has_staged_changes(paths):
                return result
            self._git("commit", "-m", message, "--no-verify", "--", *paths)
        except subprocess.CalledProcessError:
            self._unstage(paths)
            raise

        result.commit_created = True
        result.commit_message = message
        result.commit_sha = self._git("rev-parse", "HEAD").strip()
        return result

    def _build_commit_message(self, state: dict) -> str:
        explicit = str(state.get("promotion_commit_message") or "").strip()
        if explicit:
            return explicit
        agent_name = str(state.get("name") or "agent").strip() or "agent"
        return f"chore(claw): promote {agent_name}"

    def _has_staged_changes(self, paths: list[str]) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--cached", "--quiet", "--", *paths],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 1

    def _unstage(self, paths: list[str]) -> None:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            subprocess.run(
                ["git", "-C", str(self.repo_root), "reset", "--mixed", "HEAD", "--", *paths],
                capture_output=True,
                text=True,
                check=False,
            )

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout


class GitBranchPromotionExecutor:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        commit_executor: GitCommitPromotionExecutor | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.commit_executor = commit_executor or GitCommitPromotionExecutor(self.repo_root)

    def __call__(self, worktree_path: Path, state: dict, diff: str) -> PromotionResult:
        result = self.commit_executor(worktree_path, state, diff)
        if not state.get("branch_on_promotion"):
            return result
        if not result.commit_sha:
            return result

        branch_name = self._choose_branch_name(state, result.commit_sha)
        existing_sha = self._resolve_branch_sha(branch_name)
        if existing_sha is None:
            self._git("branch", "--no-track", branch_name, result.commit_sha)
            result.branch_created = True
        elif existing_sha != result.commit_sha:
            branch_name = self._allocate_unique_branch_name(branch_name, result.commit_sha)
            self._git("branch", "--no-track", branch_name, result.commit_sha)
            result.branch_created = True
        result.branch_name = branch_name
        return result

    def _choose_branch_name(self, state: dict, commit_sha: str) -> str:
        explicit = str(state.get("promotion_branch_name") or "").strip()
        if explicit:
            _validate_branch_name(explicit)
            return explicit
        agent_name = str(state.get("name") or "agent").strip() or "agent"
        return f"claw/{agent_name}/{commit_sha[:7]}"

    def _allocate_unique_branch_name(self, branch_name: str, commit_sha: str) -> str:
        candidate = f"{branch_name}-{commit_sha[:7]}"
        if self._resolve_branch_sha(candidate) is None:
            return candidate
        index = 2
        while True:
            numbered = f"{candidate}-{index}"
            if self._resolve_branch_sha(numbered) is None:
                return numbered
            index += 1

    def _resolve_branch_sha(self, branch_name: str) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "--verify", branch_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout


def _should_ignore_path(path: Path) -> bool:
    return any(part in IGNORED_WORKSPACE_NAMES for part in path.parts)


def _validate_agent_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("agent_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(slots=True)
class SubAgentDefinition:
    name: str
    display_name: str
    provider: str
    model: str
    soul: str
    heartbeat_config: str
    user_context: str
    skills: dict[str, str] = field(default_factory=dict)


class SubAgentService:
    """Manages named sub-agents (Alma, Hex, Lux, Rook) loaded from definition files."""

    def __init__(
        self,
        definitions_root: Path | str,
        router: LLMRouter,
        store: FileAgentStore,
    ) -> None:
        self.definitions_root = Path(definitions_root)
        self.router = router
        self.store = store
        self._agents: dict[str, SubAgentDefinition] = {}

    # -- loading ----------------------------------------------------------

    def discover(self) -> list[str]:
        """Scan definitions_root for agent folders containing SOUL.md."""
        found: list[str] = []
        if not self.definitions_root.is_dir():
            return found
        for child in sorted(self.definitions_root.iterdir()):
            if child.is_dir() and (child / "SOUL.md").exists():
                defn = self._load_definition(child)
                self._agents[defn.name] = defn
                found.append(defn.name)
        return found

    def _load_definition(self, agent_dir: Path) -> SubAgentDefinition:
        soul = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
        heartbeat = ""
        hb_path = agent_dir / "HEARTBEAT.md"
        if hb_path.exists():
            heartbeat = hb_path.read_text(encoding="utf-8")
        user_ctx = ""
        user_path = agent_dir / "USER.md"
        if user_path.exists():
            user_ctx = user_path.read_text(encoding="utf-8")
        skills = self._load_skills(agent_dir / "skills")
        provider, model = self._parse_model_from_soul(soul)
        display_name = self._parse_display_name(soul)
        return SubAgentDefinition(
            name=agent_dir.name,
            display_name=display_name,
            provider=provider,
            model=model,
            soul=soul,
            heartbeat_config=heartbeat,
            user_context=user_ctx,
            skills=skills,
        )

    @staticmethod
    def _load_skills(skills_dir: Path) -> dict[str, str]:
        skills: dict[str, str] = {}
        if not skills_dir.is_dir():
            return skills
        for child in sorted(skills_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.exists():
                skills[child.name] = skill_file.read_text(encoding="utf-8")
        return skills

    @staticmethod
    def _parse_model_from_soul(soul: str) -> tuple[str, str]:
        text = soul.lower()
        if "claude opus" in text:
            return ("anthropic", "claude-opus-4-6")
        if "claude sonnet" in text:
            return ("anthropic", "claude-sonnet-4-6")
        if "gemini" in text:
            return ("google", "gemini-2.5-pro")
        if "gpt" in text or "codex" in text:
            return ("openai", "gpt-4.1")
        return ("anthropic", "claude-sonnet-4-6")

    @staticmethod
    def _parse_display_name(soul: str) -> str:
        for line in soul.splitlines():
            if line.strip().startswith("- **Name:**"):
                return line.split("**Name:**")[-1].strip()
        return "Agent"

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, agent_name: str, instruction: str, *, lane: str = "research") -> str:
        """Send an instruction to a sub-agent and return its response.

        Uses ``lane="research"`` by default (no tools required).  Pass
        ``lane="worker"`` or ``lane="brain"`` when the sub-agent needs
        tool access through a capable provider adapter.
        """
        defn = self._agents.get(agent_name)
        if defn is None:
            raise KeyError(f"unknown sub-agent: {agent_name}")
        system_prompt = self._build_system_prompt(defn)
        try:
            response = self.router.ask(
                instruction,
                system_prompt=system_prompt,
                lane=lane,
                provider=defn.provider,
                model=defn.model,
                evidence_pack={"sub_agent": defn.name, "display_name": defn.display_name},
            )
        except (ValueError, Exception):
            # Fallback to default provider when the preferred one is unavailable.
            response = self.router.ask(
                instruction,
                system_prompt=system_prompt,
                lane=lane,
                evidence_pack={"sub_agent": defn.name, "display_name": defn.display_name},
            )
        return response.content

    def run_skill(self, agent_name: str, skill_name: str, context: str = "", *, lane: str = "research") -> str:
        """Execute a named skill for a sub-agent."""
        defn = self._agents.get(agent_name)
        if defn is None:
            raise KeyError(f"unknown sub-agent: {agent_name}")
        skill_prompt = defn.skills.get(skill_name)
        if skill_prompt is None:
            raise KeyError(f"unknown skill '{skill_name}' for agent '{agent_name}'")
        instruction = f"Execute skill: {skill_name}\n\n{skill_prompt}"
        if context:
            instruction += f"\n\nAdditional context:\n{context}"
        return self.dispatch(agent_name, instruction, lane=lane)

    def heartbeat(self, agent_name: str) -> str:
        """Run a heartbeat check for a sub-agent."""
        defn = self._agents.get(agent_name)
        if defn is None:
            raise KeyError(f"unknown sub-agent: {agent_name}")
        if not defn.heartbeat_config:
            return "HEARTBEAT_OK"
        return self.dispatch(agent_name, defn.heartbeat_config)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def get_agent(self, name: str) -> SubAgentDefinition | None:
        return self._agents.get(name)

    def list_skills(self, agent_name: str) -> list[str]:
        defn = self._agents.get(agent_name)
        if defn is None:
            return []
        return list(defn.skills.keys())

    @staticmethod
    def _build_system_prompt(defn: SubAgentDefinition) -> str:
        parts = [defn.soul]
        if defn.user_context:
            parts.append(f"\n\n## User Context\n\n{defn.user_context}")
        return "\n".join(parts)


def _validate_branch_name(name: str) -> None:
    if not name:
        raise ValueError("promotion_branch_name must not be empty")
    if name.startswith("/") or name.endswith("/") or name.endswith(".") or name.endswith(".lock"):
        raise ValueError("promotion_branch_name is not a valid git branch name")
    if any(fragment in name for fragment in ("..", "@{", "//")):
        raise ValueError("promotion_branch_name is not a valid git branch name")
    if any(char in name for char in " ~^:?*[\\"):
        raise ValueError("promotion_branch_name is not a valid git branch name")
