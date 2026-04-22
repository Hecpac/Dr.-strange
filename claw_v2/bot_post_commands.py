from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import _parse_positive_int

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PostCommandPlugin:
    bot: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bot, name)

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand("approvals", self._handle_approvals_command, exact=("/approvals",), prefixes=("/approval_status ", "/approve ", "/task_approve ", "/task_abort ")),
            BotCommand("traces", self._handle_traces_command, exact=("/traces",), prefixes=("/traces ", "/trace ")),
            BotCommand("feedback", self._handle_feedback_command, exact=("/feedback",), prefixes=("/feedback ",)),
            BotCommand("pipeline", self._handle_pipeline_command, exact=("/pipeline", "/pipeline_approve", "/pipeline_merge", "/pipeline_status"), prefixes=("/pipeline_approve ", "/pipeline_merge ", "/pipeline ")),
            BotCommand("social", self._handle_social_command, exact=("/social_preview", "/social_publish", "/social_status"), prefixes=("/social_preview ", "/social_publish ")),
        ]

    def _handle_approvals_command(self, context: CommandContext) -> str:
        stripped = context.stripped
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
        if stripped.startswith("/task_approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /task_approve <approval_id> <token>"
            return self._task_handler.task_approve_response(parts[1], parts[2])
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /task_abort <approval_id>"
        return self._task_handler.task_abort_response(parts[1])

    def _handle_traces_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/traces":
            return self._traces_response(limit=10)
        if stripped.startswith("/traces "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /traces [limit]"
            try:
                limit = _parse_positive_int(parts[1], field_name="limit")
            except ValueError as exc:
                return str(exc)
            return self._traces_response(limit=limit)
        parts = stripped.split(maxsplit=2)
        if len(parts) < 2:
            return "usage: /trace <trace_id> [limit]"
        limit = 100
        if len(parts) == 3:
            try:
                limit = _parse_positive_int(parts[2], field_name="limit")
            except ValueError as exc:
                return str(exc)
        return self._trace_replay_response(parts[1], limit=limit)

    def _handle_feedback_command(self, context: CommandContext) -> str:
        if self.learning is None:
            return "learning loop not available"
        parts = context.stripped.split(maxsplit=2)
        if len(parts) < 2:
            return "usage: /feedback <positive|negative|note> [outcome_id]"
        rating = parts[1]
        oid = int(parts[2]) if len(parts) == 3 else None
        return self.learning.feedback(oid, rating)

    def _handle_pipeline_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped.startswith("/pipeline_approve "):
            return self._pipeline_approve(stripped)
        if stripped.startswith("/pipeline_merge "):
            return self._pipeline_merge(stripped)
        if stripped.startswith("/pipeline_status"):
            return self._pipeline_status()
        if stripped.startswith("/pipeline "):
            return self._pipeline_start(stripped)
        return f"usage: {stripped} <argument>"

    def _pipeline_approve(self, stripped: str) -> str:
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

    def _pipeline_merge(self, stripped: str) -> str:
        if self.pipeline is None:
            return "pipeline service unavailable"
        parts = stripped.split(maxsplit=1)
        issue_id = parts[1].strip()
        try:
            run = self.pipeline.merge_and_close(issue_id)
            return json.dumps({"issue": run.issue_id, "status": run.status, "pr_url": run.pr_url}, indent=2)
        except Exception:
            logger.exception("pipeline merge error for %s", issue_id)
            return "merge error — check logs for details"

    def _pipeline_status(self) -> str:
        if self.pipeline is None:
            return "pipeline service unavailable"
        active = self.pipeline.list_active()
        if not active:
            return "no active pipeline runs"
        return json.dumps([{"issue": r.issue_id, "status": r.status, "branch": r.branch_name} for r in active], indent=2)

    def _pipeline_start(self, stripped: str) -> str:
        if self.pipeline is None:
            return "pipeline service unavailable"
        parts = stripped.split(maxsplit=2)
        issue_id = parts[1]
        repo_root = Path(parts[2]) if len(parts) == 3 else None
        try:
            run = self.pipeline.process_issue(issue_id, repo_root=repo_root)
            return json.dumps({"issue": run.issue_id, "status": run.status, "branch": run.branch_name, "approval_id": run.approval_id, "approve_command": f"/pipeline_approve {run.approval_id}"}, indent=2)
        except Exception:
            logger.exception("pipeline error for %s", issue_id)
            return "pipeline error — check logs for details"

    def _handle_social_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/social_status":
            return self._social_status()
        if stripped.startswith("/social_preview "):
            return self._social_preview(stripped)
        if stripped.startswith("/social_publish "):
            return self._social_publish(stripped)
        return f"usage: {stripped} <argument>"

    def _social_status(self) -> str:
        if self.content_engine is None:
            return "social content engine unavailable"
        accounts_root = self.content_engine.accounts_root
        accounts = sorted(p.name for p in accounts_root.iterdir() if p.is_dir())
        return json.dumps([{"account": account} for account in accounts], indent=2)

    def _social_preview(self, stripped: str) -> str:
        if self.content_engine is None:
            return "social content engine unavailable"
        account = stripped.split(maxsplit=1)[1]
        try:
            drafts = self.content_engine.generate_batch(account)
            return json.dumps([{"platform": d.platform, "text": d.text, "hashtags": d.hashtags} for d in drafts], indent=2)
        except FileNotFoundError:
            return f"account not found: {account}"
        except Exception:
            logger.exception("social_preview error for %s", account)
            return "error generating preview — check logs"

    def _social_publish(self, stripped: str) -> str:
        if self.content_engine is None or self.social_publisher is None:
            return "social services unavailable"
        account = stripped.split(maxsplit=1)[1]
        try:
            drafts = self.content_engine.generate_batch(account)
            results = [self.social_publisher.publish(draft) for draft in drafts]
            return json.dumps([{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results], indent=2)
        except Exception:
            logger.exception("social_publish error for %s", account)
            return "error publishing — check logs"
