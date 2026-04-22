from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext


@dataclass(slots=True)
class JobCommandPlugin:
    bot: Any

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "jobs",
                self._handle_jobs_command,
                exact=("/jobs", "/job_status", "/job_cancel"),
                prefixes=("/jobs ", "/job_status ", "/job_cancel "),
            )
        ]

    def _handle_jobs_command(self, context: CommandContext) -> str:
        if self.bot.job_service is None:
            return "job service unavailable"
        stripped = context.stripped
        if stripped.startswith("/job_status "):
            return self._status(stripped.split(maxsplit=1)[1])
        if stripped == "/job_status":
            return "usage: /job_status <job_id>"
        if stripped.startswith("/job_cancel "):
            return self._cancel(stripped.split(maxsplit=1)[1])
        if stripped == "/job_cancel":
            return "usage: /job_cancel <job_id>"
        parts = stripped.split()
        state = parts[1] if len(parts) > 1 else "active"
        include_terminal = state in {"all", "completed", "failed", "cancelled"}
        jobs = self.bot.job_service.list_jobs(limit=20, include_terminal=include_terminal)
        if state not in {"all", "active"}:
            jobs = [job for job in jobs if job.state == state]
        if not jobs:
            return "no jobs"
        return json.dumps(
            [
                {
                    "id": job.job_id,
                    "kind": job.kind,
                    "state": job.state,
                    "updated_at": job.updated_at,
                    "payload": job.payload,
                }
                for job in jobs
            ],
            indent=2,
            sort_keys=True,
        )

    def _status(self, job_id: str) -> str:
        job = self.bot.job_service.get(job_id)
        if job is None:
            return f"job not found: {job_id}"
        steps = self.bot.job_service.steps(job_id)
        return json.dumps(
            {
                "job": {
                    "id": job.job_id,
                    "kind": job.kind,
                    "state": job.state,
                    "version": job.version,
                    "updated_at": job.updated_at,
                    "payload": job.payload,
                },
                "steps": [asdict(step) for step in steps],
            },
            indent=2,
            sort_keys=True,
        )

    def _cancel(self, job_id: str) -> str:
        try:
            job = self.bot.job_service.cancel(job_id, reason="telegram_command")
        except KeyError:
            return f"job not found: {job_id}"
        return json.dumps({"id": job.job_id, "state": job.state}, indent=2, sort_keys=True)
