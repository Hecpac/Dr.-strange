from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from claw_v2.agents import AgentDefinition, AutoResearchAgentService
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.pipeline import PipelineService


class BotService:
    def __init__(
        self,
        *,
        brain: BrainService,
        auto_research: AutoResearchAgentService,
        heartbeat: HeartbeatService,
        approvals: ApprovalManager,
        pull_requests: GitHubPullRequestService | None = None,
        allowed_user_id: str | None = None,
        pipeline: PipelineService | None = None,
    ) -> None:
        self.brain = brain
        self.auto_research = auto_research
        self.heartbeat = heartbeat
        self.approvals = approvals
        self.pull_requests = pull_requests
        self.allowed_user_id = allowed_user_id
        self.pipeline = pipeline

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is not None and user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        stripped = text.strip()
        if stripped == "/status":
            return json.dumps(asdict(self.heartbeat.collect()), indent=2, sort_keys=True)
        if stripped == "/agents":
            return json.dumps(self._list_agents_payload(), indent=2, sort_keys=True)
        if stripped.startswith("/agent_create "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_create <agent_name> <researcher|operator|deployer> <instruction>"
            return self._create_agent_response(parts[1], parts[2], parts[3])
        if stripped.startswith("/agent_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_status <agent_name>"
            return self._agent_state_response(parts[1])
        if stripped.startswith("/agent_pause "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_pause <agent_name>"
            return self._pause_agent_response(parts[1])
        if stripped.startswith("/agent_resume "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_resume <agent_name>"
            return self._resume_agent_response(parts[1])
        if stripped.startswith("/agent_history "):
            parts = stripped.split(maxsplit=2)
            if len(parts) == 2:
                limit = 10
            elif len(parts) == 3:
                try:
                    limit = _parse_positive_int(parts[2], field_name="limit")
                except ValueError as exc:
                    return str(exc)
            else:
                return "usage: /agent_history <agent_name> [limit]"
            return self._agent_history_response(parts[1], limit=limit)
        if stripped.startswith("/agent_promote "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_promote <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], promote_on_improvement=enabled)
        if stripped.startswith("/agent_branch "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_branch <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], branch_on_promotion=enabled)
        if stripped.startswith("/agent_branch_name "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_branch_name <agent_name> <name|-|clear>"
            branch_name = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_agent_response(parts[1], promotion_branch_name=branch_name)
        if stripped.startswith("/agent_commit "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_commit <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], commit_on_promotion=enabled)
        if stripped.startswith("/agent_commit_message "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_commit_message <agent_name> <message|-|clear>"
            message = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_agent_response(parts[1], promotion_commit_message=message)
        if stripped.startswith("/agent_run_until "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_run_until <agent_name> <target_metric> <max_experiments>"
            try:
                target_metric = _parse_float(parts[2], field_name="target_metric")
                max_experiments = _parse_positive_int(parts[3], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._run_agent_until_response(parts[1], target_metric=target_metric, max_experiments=max_experiments)
        if stripped.startswith("/agent_publish "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_publish <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._publish_agent_response(parts[1], max_experiments=max_experiments)
        if stripped.startswith("/agent_pr "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_pr <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._pull_request_response(parts[1], max_experiments=max_experiments)
        if stripped.startswith("/agent_run "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_run <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._run_agent_response(parts[1], max_experiments=max_experiments)
        if stripped == "/approvals":
            return json.dumps(self.approvals.list_pending(), indent=2, sort_keys=True)
        if stripped.startswith("/approval_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /approval_status <approval_id>"
            return self.approvals.status(parts[1])
        if stripped.startswith("/approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /approve <approval_id> <token>"
            approved = self.approvals.approve(parts[1], parts[2])
            return "approval recorded" if approved else "approval rejected"
        if stripped.startswith("/pipeline_approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /pipeline_approve <approval_id> <token>"
            approved = self.approvals.approve(parts[1], parts[2])
            if not approved:
                return "approval rejected"
            if self.pipeline is None:
                return "pipeline service unavailable"
            for run in self.pipeline.list_active():
                if run.approval_id == parts[1]:
                    result = self.pipeline.complete_pipeline(run.issue_id)
                    return json.dumps({"status": result.status, "pr_url": result.pr_url}, indent=2)
            return "approval recorded but no matching pipeline run found"
        if stripped == "/pipeline_status":
            if self.pipeline is None:
                return "pipeline service unavailable"
            active = self.pipeline.list_active()
            if not active:
                return "no active pipeline runs"
            return json.dumps([{"issue": r.issue_id, "status": r.status, "branch": r.branch_name} for r in active], indent=2)
        if stripped.startswith("/pipeline "):
            if self.pipeline is None:
                return "pipeline service unavailable"
            parts = stripped.split(maxsplit=2)
            issue_id = parts[1]
            repo_root = Path(parts[2]) if len(parts) == 3 else None
            try:
                run = self.pipeline.process_issue(issue_id, repo_root=repo_root)
                return json.dumps({"issue": run.issue_id, "status": run.status, "branch": run.branch_name, "approval_id": run.approval_id, "approval_token": run.approval_token}, indent=2)
            except Exception as exc:
                return f"pipeline error: {exc}"
        return self.brain.handle_message(session_id, stripped).content

    def _list_agents_payload(self) -> list[dict]:
        payload: list[dict] = []
        for agent_name in self.auto_research.list_agents():
            state = self.auto_research.inspect(agent_name)
            payload.append(_agent_summary(agent_name, state))
        return payload

    def _agent_state_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _create_agent_response(self, agent_name: str, agent_class: str, instruction: str) -> str:
        try:
            state = self.auto_research.create_agent(
                AgentDefinition(
                    name=agent_name,
                    agent_class=agent_class,
                    instruction=instruction,
                )
            )
        except FileExistsError:
            return f"agent already exists: {agent_name}"
        except ValueError as exc:
            return str(exc)
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _update_agent_response(self, agent_name: str, **changes: object) -> str:
        try:
            state = self.auto_research.update_controls(agent_name, **changes)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        except ValueError as exc:
            return str(exc)
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _pause_agent_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.pause(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _resume_agent_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.resume(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _agent_history_response(self, agent_name: str, *, limit: int) -> str:
        try:
            state = self.auto_research.inspect(agent_name)
            history = self.auto_research.history(agent_name, limit=limit)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(
            {
                **_agent_summary(agent_name, state, include_instruction=True),
                "history_limit": limit,
                "history_count": len(history),
                "history": [_record_summary(item) for item in history],
            },
            indent=2,
            sort_keys=True,
        )

    def _run_agent_response(self, agent_name: str, *, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments)
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _run_agent_until_response(self, agent_name: str, *, target_metric: float, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_until(
                agent_name,
                target_metric=target_metric,
                max_experiments=max_experiments,
            )
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _publish_agent_response(self, agent_name: str, *, max_experiments: int) -> str:
        payload = self._publish_agent_payload(agent_name, max_experiments=max_experiments)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, indent=2, sort_keys=True)

    def _publish_agent_payload(self, agent_name: str, *, max_experiments: int) -> dict | str:
        try:
            previous_state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"

        update_payload: dict[str, object] = {}
        if not previous_state.get("promote_on_improvement", False):
            update_payload["promote_on_improvement"] = True
        if not previous_state.get("commit_on_promotion", False):
            update_payload["commit_on_promotion"] = True
        if not previous_state.get("branch_on_promotion", False):
            update_payload["branch_on_promotion"] = True
        if update_payload:
            self.auto_research.update_controls(agent_name, **update_payload)

        result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments)
        state = self.auto_research.inspect(agent_name)
        latest = self.auto_research.latest_result(agent_name)
        payload = _run_summary(agent_name, state, result)
        payload["publish_mode_updated"] = bool(update_payload)
        if latest is not None:
            payload["published_commit_sha"] = latest.promotion_commit_sha
            payload["published_branch_name"] = latest.promotion_branch_name
            payload["published"] = bool(latest.promotion_commit_sha or latest.promotion_branch_name)
        else:
            payload["published_commit_sha"] = None
            payload["published_branch_name"] = None
            payload["published"] = False
        return payload

    def _pull_request_response(self, agent_name: str, *, max_experiments: int) -> str:
        payload = self._publish_agent_payload(agent_name, max_experiments=max_experiments)
        if isinstance(payload, str):
            return payload
        if self.pull_requests is None:
            payload["pull_request_created"] = False
            payload["pull_request_error"] = "pull request service unavailable"
            return json.dumps(payload, indent=2, sort_keys=True)
        branch_name = payload.get("published_branch_name")
        if not isinstance(branch_name, str) or not branch_name:
            payload["pull_request_created"] = False
            payload["pull_request_error"] = "no published branch available for pull request creation"
            return json.dumps(payload, indent=2, sort_keys=True)

        title = _default_pull_request_title(agent_name, payload)
        body = _default_pull_request_body(agent_name, payload)
        try:
            pr = self.pull_requests.create_pull_request(
                branch_name=branch_name,
                title=title,
                body=body,
                draft=True,
            )
        except Exception as exc:  # pragma: no cover - depends on local gh/git runtime
            payload["pull_request_created"] = False
            payload["pull_request_error"] = str(exc)
            return json.dumps(payload, indent=2, sort_keys=True)

        payload["pull_request_created"] = True
        payload["pull_request_url"] = pr.url
        payload["pull_request_number"] = pr.number
        payload["pull_request_title"] = pr.title
        payload["pull_request_draft"] = pr.draft
        return json.dumps(payload, indent=2, sort_keys=True)


def _parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise ValueError("toggle must be one of: on, off")


def _parse_positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return parsed


def _parse_float(value: str, *, field_name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _agent_summary(agent_name: str, state: dict, *, include_instruction: bool = False) -> dict:
    summary = {
        "name": agent_name,
        "agent_class": state.get("agent_class"),
        "allowed_tools": state.get("allowed_tools", []),
        "paused": state.get("paused", False),
        "trust_level": state.get("trust_level", 1),
        "experiments_today": state.get("experiments_today", 0),
        "last_metric": state.get("last_verified_state", {}).get("metric"),
        "promote_on_improvement": state.get("promote_on_improvement", False),
        "commit_on_promotion": state.get("commit_on_promotion", False),
        "branch_on_promotion": state.get("branch_on_promotion", False),
        "promotion_commit_message": state.get("promotion_commit_message"),
        "promotion_branch_name": state.get("promotion_branch_name"),
    }
    if include_instruction:
        summary["instruction"] = state.get("instruction")
    return summary


def _run_summary(agent_name: str, state: dict, result: object) -> dict:
    summary = _agent_summary(agent_name, state, include_instruction=True)
    summary.update(
        {
            "experiments_run": getattr(result, "experiments_run"),
            "run_reason": getattr(result, "reason"),
            "run_paused": getattr(result, "paused"),
            "run_last_metric": getattr(result, "last_metric"),
        }
    )
    return summary


def _record_summary(record: object) -> dict:
    return {
        "experiment_number": getattr(record, "experiment_number"),
        "metric_value": getattr(record, "metric_value"),
        "baseline_value": getattr(record, "baseline_value"),
        "status": getattr(record, "status"),
        "cost_usd": getattr(record, "cost_usd"),
        "promotion_commit_sha": getattr(record, "promotion_commit_sha"),
        "promotion_branch_name": getattr(record, "promotion_branch_name"),
    }


def _default_pull_request_title(agent_name: str, payload: dict) -> str:
    explicit = payload.get("promotion_commit_message")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return f"chore(claw): publish {agent_name}"


def _default_pull_request_body(agent_name: str, payload: dict) -> str:
    lines = [
        "Automated draft pull request created by Claw.",
        "",
        f"- Agent: {agent_name}",
        f"- Experiments run: {payload.get('experiments_run')}",
        f"- Run reason: {payload.get('run_reason')}",
        f"- Last metric: {payload.get('run_last_metric')}",
        f"- Commit: {payload.get('published_commit_sha')}",
        f"- Branch: {payload.get('published_branch_name')}",
    ]
    return "\n".join(lines)
