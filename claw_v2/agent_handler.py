from __future__ import annotations

import json
from typing import Any

from claw_v2.agents import AgentDefinition
from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import (
    _agent_summary,
    _default_pull_request_body,
    _default_pull_request_title,
    _parse_float,
    _parse_positive_int,
    _parse_toggle,
    _record_summary,
    _run_summary,
)


class AgentHandler:
    def __init__(
        self,
        *,
        auto_research: Any | None = None,
        pull_requests: Any | None = None,
    ) -> None:
        self.auto_research = auto_research
        self.pull_requests = pull_requests

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "agents",
                self.handle_command,
                exact=("/agents",),
                prefixes=(
                    "/agent_create ",
                    "/agent_status ",
                    "/agent_pause ",
                    "/agent_resume ",
                    "/agent_history ",
                    "/agent_promote ",
                    "/agent_branch ",
                    "/agent_commit ",
                    "/agent_branch_name ",
                    "/agent_commit_message ",
                    "/agent_run_until ",
                    "/agent_publish ",
                    "/agent_pr ",
                    "/agent_run ",
                ),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/agents":
            return json.dumps(self._list_agents_payload(), indent=2, sort_keys=True)
        if stripped.startswith("/agent_create "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_create <agent_name> <researcher|operator|deployer> <instruction>"
            return self._create_response(parts[1], parts[2], parts[3])
        if stripped.startswith("/agent_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_status <agent_name>"
            return self._state_response(parts[1])
        if stripped.startswith("/agent_pause "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_pause <agent_name>"
            return self._pause_response(parts[1])
        if stripped.startswith("/agent_resume "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_resume <agent_name>"
            return self._resume_response(parts[1])
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
            return self._history_response(parts[1], limit=limit)
        if stripped.startswith("/agent_promote "):
            return self._toggle_update(stripped, "promote_on_improvement", "usage: /agent_promote <agent_name> <on|off>")
        if stripped.startswith("/agent_branch "):
            return self._toggle_update(stripped, "branch_on_promotion", "usage: /agent_branch <agent_name> <on|off>")
        if stripped.startswith("/agent_commit "):
            return self._toggle_update(stripped, "commit_on_promotion", "usage: /agent_commit <agent_name> <on|off>")
        if stripped.startswith("/agent_branch_name "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_branch_name <agent_name> <name|-|clear>"
            branch_name = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_response(parts[1], promotion_branch_name=branch_name)
        if stripped.startswith("/agent_commit_message "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_commit_message <agent_name> <message|-|clear>"
            message = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_response(parts[1], promotion_commit_message=message)
        if stripped.startswith("/agent_run_until "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_run_until <agent_name> <target_metric> <max_experiments>"
            try:
                target_metric = _parse_float(parts[2], field_name="target_metric")
                max_experiments = _parse_positive_int(parts[3], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._run_until_response(parts[1], target_metric=target_metric, max_experiments=max_experiments)
        if stripped.startswith("/agent_publish "):
            return self._run_command(stripped, "publish")
        if stripped.startswith("/agent_pr "):
            return self._run_command(stripped, "pr")
        if stripped.startswith("/agent_run "):
            return self._run_command(stripped, "run")
        return "usage: /agent_<command> ..."

    def _list_agents_payload(self) -> list[dict]:
        payload: list[dict] = []
        for agent_name in self.auto_research.list_agents():
            state = self.auto_research.inspect(agent_name)
            payload.append(_agent_summary(agent_name, state))
        return payload

    def _state_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _create_response(self, agent_name: str, agent_class: str, instruction: str) -> str:
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

    def _update_response(self, agent_name: str, **changes: object) -> str:
        try:
            state = self.auto_research.update_controls(agent_name, **changes)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        except ValueError as exc:
            return str(exc)
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _pause_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.pause(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _resume_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.resume(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _history_response(self, agent_name: str, *, limit: int) -> str:
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

    def _run_response(self, agent_name: str, *, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments, force=True)
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _run_until_response(self, agent_name: str, *, target_metric: float, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_until(
                agent_name,
                target_metric=target_metric,
                max_experiments=max_experiments,
                force=True,
            )
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _publish_response(self, agent_name: str, *, max_experiments: int) -> str:
        payload = self._publish_payload(agent_name, max_experiments=max_experiments)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, indent=2, sort_keys=True)

    def _publish_payload(self, agent_name: str, *, max_experiments: int) -> dict | str:
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

        result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments, force=True)
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
        payload = self._publish_payload(agent_name, max_experiments=max_experiments)
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
        except Exception as exc:
            payload["pull_request_created"] = False
            payload["pull_request_error"] = str(exc)
            return json.dumps(payload, indent=2, sort_keys=True)

        payload["pull_request_created"] = True
        payload["pull_request_url"] = pr.url
        payload["pull_request_number"] = pr.number
        payload["pull_request_title"] = pr.title
        payload["pull_request_draft"] = pr.draft
        return json.dumps(payload, indent=2, sort_keys=True)

    def _toggle_update(self, stripped: str, field: str, usage: str) -> str:
        parts = stripped.split(maxsplit=2)
        if len(parts) != 3:
            return usage
        try:
            enabled = _parse_toggle(parts[2])
        except ValueError as exc:
            return str(exc)
        return self._update_response(parts[1], **{field: enabled})

    def _run_command(self, stripped: str, command: str) -> str:
        parts = stripped.split(maxsplit=2)
        usages = {
            "publish": "usage: /agent_publish <agent_name> <max_experiments>",
            "pr": "usage: /agent_pr <agent_name> <max_experiments>",
            "run": "usage: /agent_run <agent_name> <max_experiments>",
        }
        if len(parts) != 3:
            return usages[command]
        try:
            max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
        except ValueError as exc:
            return str(exc)
        if command == "publish":
            return self._publish_response(parts[1], max_experiments=max_experiments)
        if command == "pr":
            return self._pull_request_response(parts[1], max_experiments=max_experiments)
        return self._run_response(parts[1], max_experiments=max_experiments)
