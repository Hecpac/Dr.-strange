from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

from claw_v2.agents import AgentDefinition, AutoResearchAgentService
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.content import ContentEngine
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.pipeline import PipelineService
from claw_v2.social import SocialPublisher

_BROWSE_SHORTCUT_TOKENS = (
    "abre",
    "abrir",
    "open",
    "revisa",
    "revisalo",
    "review",
    "check",
    "lee",
    "read",
    "analiza",
    "analyze",
    "visita",
    "visit",
    "navega",
    "browse",
)
_COMPUTER_ACTION_TOKENS = (
    "click",
    "clic",
    "scroll",
    "desplaza",
    "desplazate",
    "desplazarse",
    "sube",
    "baja",
    "escribe",
    "type",
    "press",
    "presiona",
    "selecciona",
    "drag",
    "arrastra",
    "abre el menu",
    "open the menu",
)
_COMPUTER_READ_TOKENS = (
    "revisa la pagina actual",
    "revisa la pantalla",
    "revisa esta pagina",
    "revisa la campana",
    "revisa la campaña",
    "que ves",
    "que hay en la pantalla",
    "dime que ves",
    "describe la pantalla",
)
_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()]+)", re.IGNORECASE)
_HOST_URL_RE = re.compile(
    r"(?P<url>(?<!@)(?:localhost(?::\d+)?|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?)(?:/[^\s<>()]*)?)",
    re.IGNORECASE,
)
_DEFAULT_COMPUTER_MODEL = "claude-opus-4-6"
_COMPUTER_SYSTEM_PROMPT = (
    "You control the user's Mac via the computer-use tool. "
    "Be careful, explicit, and incremental. "
    "Prefer reading the current screen before acting. "
    "When searching for a visible UI element, move/scroll as needed, then click only when confident. "
    "Stop and explain what you see when the task is complete."
)


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
        content_engine: ContentEngine | None = None,
        social_publisher: SocialPublisher | None = None,
        config: object | None = None,
        browser: object | None = None,
        terminal_bridge: object | None = None,
        computer: object | None = None,
        browser_use: object | None = None,
        computer_gate: object | None = None,
        computer_client_factory: Callable[[], Any] | None = None,
        computer_model: str = _DEFAULT_COMPUTER_MODEL,
        computer_system_prompt: str | None = None,
        observe: object | None = None,
    ) -> None:
        self.brain = brain
        self.auto_research = auto_research
        self.heartbeat = heartbeat
        self.approvals = approvals
        self.pull_requests = pull_requests
        self.allowed_user_id = allowed_user_id
        self.pipeline = pipeline
        self.content_engine = content_engine
        self.social_publisher = social_publisher
        self.config = config
        self.browser = browser
        self.terminal_bridge = terminal_bridge
        self.computer = computer
        self.browser_use = browser_use
        self.computer_gate = computer_gate
        self.computer_client_factory = computer_client_factory
        self.computer_model = computer_model
        self.computer_system_prompt = computer_system_prompt or _COMPUTER_SYSTEM_PROMPT
        self.observe = observe
        self._computer_sessions: dict[str, Any] = {}
        self._computer_client: Any | None = None

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        stripped = text.strip()
        if stripped == "/status":
            return json.dumps(asdict(self.heartbeat.collect()), indent=2, sort_keys=True)
        if stripped == "/config":
            if self.config is None:
                return "config not available"
            c = self.config
            lanes = {}
            for lane in ("brain", "worker", "verifier", "research", "judge"):
                lanes[lane] = {
                    "provider": c.provider_for_lane(lane),
                    "model": c.model_for_lane(lane),
                    "effort": c.effort_for_lane(lane),
                    "context_window": c.context_window_for_lane(lane),
                    "max_output": c.max_output_for_lane(lane),
                }
            return json.dumps({"lanes": lanes, "max_budget_usd": c.max_budget_usd, "daily_token_budget": c.daily_token_budget}, indent=2)
        if stripped == "/tokens":
            return self._tokens_info_response(session_id)
        if stripped.startswith("/browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /browse <url>"
            return self._browse_response(parts[1])
        if stripped == "/terminal_list":
            return self._terminal_list_response()
        if stripped == "/terminal_open":
            return "usage: /terminal_open <claude|codex> [cwd]"
        if stripped.startswith("/terminal_open "):
            parts = stripped.split(maxsplit=2)
            if len(parts) not in {2, 3}:
                return "usage: /terminal_open <claude|codex> [cwd]"
            cwd = parts[2] if len(parts) == 3 else None
            return self._terminal_open_response(parts[1], cwd=cwd)
        if stripped == "/terminal_status":
            return "usage: /terminal_status <session_id>"
        if stripped.startswith("/terminal_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /terminal_status <session_id>"
            return self._terminal_status_response(parts[1])
        if stripped == "/terminal_read":
            return "usage: /terminal_read <session_id> [offset]"
        if stripped.startswith("/terminal_read "):
            parts = stripped.split(maxsplit=2)
            if len(parts) == 2:
                offset = 0
            elif len(parts) == 3:
                try:
                    offset = _parse_non_negative_int(parts[2], field_name="offset")
                except ValueError as exc:
                    return str(exc)
            else:
                return "usage: /terminal_read <session_id> [offset]"
            return self._terminal_read_response(parts[1], offset=offset)
        if stripped == "/terminal_send":
            return "usage: /terminal_send <session_id> <text>"
        if stripped.startswith("/terminal_send "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /terminal_send <session_id> <text>"
            return self._terminal_send_response(parts[1], parts[2])
        if stripped == "/terminal_close":
            return "usage: /terminal_close <session_id>"
        if stripped.startswith("/terminal_close "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /terminal_close <session_id>"
            return self._terminal_close_response(parts[1])
        if stripped == "/chrome_pages":
            return self._chrome_pages_response()
        if stripped == "/chrome_browse":
            return "usage: /chrome_browse <url>"
        if stripped.startswith("/chrome_browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /chrome_browse <url>"
            return self._chrome_browse_response(parts[1])
        if stripped.startswith("/chrome_shot"):
            return self._chrome_shot_response(stripped)
        if stripped == "/screen":
            return self._screen_response()
        if stripped == "/computer":
            return "usage: /computer <instruction>"
        if stripped.startswith("/computer_abort"):
            return self._computer_abort_response(session_id)
        if stripped.startswith("/computer "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /computer <instruction>"
            return self._computer_response(parts[1], session_id)
        if stripped.startswith("/action_approve "):
            parts = stripped.split()
            if len(parts) != 3:
                return "usage: /action_approve <approval_id> <token>"
            return self._action_approve_response(parts[1], parts[2])
        if stripped.startswith("/action_abort "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /action_abort <approval_id>"
            return self._action_abort_response(parts[1])
        if stripped == "/buddy hatch":
            return self._buddy_hatch_response(user_id)
        if stripped == "/buddy stats":
            return self._buddy_stats_response(user_id)
        if stripped.startswith("/buddy rename "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /buddy rename <name>"
            return self._buddy_rename_response(user_id, parts[2])
        if stripped == "/buddy" or stripped == "/buddy card":
            return self._buddy_card_response(user_id)
        shortcut_response = self._maybe_handle_shortcut(stripped, session_id=session_id)
        if shortcut_response is not None:
            return shortcut_response
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
            except Exception:
                logger.exception("pipeline error for %s", issue_id)
                return "pipeline error — check logs for details"
        if stripped in ("/pipeline", "/pipeline_approve", "/social_preview", "/social_publish"):
            return f"usage: {stripped} <argument>"
        if stripped == "/social_status":
            if self.content_engine is None:
                return "social content engine unavailable"
            accounts_root = self.content_engine.accounts_root
            accounts = sorted(p.name for p in accounts_root.iterdir() if p.is_dir())
            return json.dumps([{"account": a} for a in accounts], indent=2)
        if stripped.startswith("/social_preview "):
            if self.content_engine is None:
                return "social content engine unavailable"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                return json.dumps([{"platform": d.platform, "text": d.text, "hashtags": d.hashtags} for d in drafts], indent=2)
            except FileNotFoundError:
                return f"account not found: {account}"
            except Exception:
                logger.exception("social_preview error for %s", account)
                return "error generating preview — check logs"
        if stripped.startswith("/social_publish "):
            if self.content_engine is None or self.social_publisher is None:
                return "social services unavailable"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                results = [self.social_publisher.publish(d) for d in drafts]
                return json.dumps([{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results], indent=2)
            except Exception:
                logger.exception("social_publish error for %s", account)
                return "error publishing — check logs"
        return self.brain.handle_message(session_id, stripped).content

    def handle_multimodal(
        self,
        *,
        user_id: str,
        session_id: str,
        content_blocks: list[dict[str, Any]],
        memory_text: str,
    ) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        return self.brain.handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
        ).content

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

    def _browse_response(self, url: str) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        # Strategy 1: Playwright (headless browser)
        if self.browser is not None:
            try:
                result = self.browser.browse(normalized_url)
                content = result.content[:4000] if result.content else ""
                if not _is_login_wall(content):
                    return json.dumps({
                        "url": result.url,
                        "title": result.title,
                        "content": content,
                    }, indent=2)
            except Exception:
                pass  # fall through to next strategy
        # Strategy 2: Firecrawl CLI (handles JS-rendered pages)
        try:
            import subprocess as _sp
            fc = _sp.run(
                ["firecrawl", "scrape", normalized_url],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if fc.returncode == 0 and fc.stdout.strip() and "do not support" not in fc.stdout:
                content = fc.stdout.strip()[:4000]
                return json.dumps({"url": normalized_url, "title": "(firecrawl)", "content": content}, indent=2)
        except (FileNotFoundError, _sp.TimeoutExpired):
            pass
        # Strategy 3: Chrome CDP (user's real browser session)
        if self.browser is not None:
            try:
                result = self.browser.chrome_navigate(normalized_url)
                return f"**{result.title}** ({result.url})\n\n{result.content[:4000]}"
            except Exception:
                pass
        return f"browse error: all strategies failed for {normalized_url}"

    def _tokens_info_response(self, session_id: str) -> str:
        message_count = self.brain.memory.count_messages(session_id)

        # Estimación aproximada: ~500 tokens por mensaje (muy conservador)
        estimated_tokens = message_count * 500
        context_window = self.config.brain_context_window if self.config else 1_000_000

        estimated_pct = (estimated_tokens / context_window) * 100

        if estimated_pct > 80:
            status = "critical"
            status_emoji = "🔴"
            recommendation = "Compacta ahora para evitar pérdida de contexto"
        elif estimated_pct > 60:
            status = "warning"
            status_emoji = "🟡"
            recommendation = "Considera compactar pronto"
        else:
            status = "healthy"
            status_emoji = "🟢"
            recommendation = "Espacio saludable"

        max_output = self.config.brain_max_output if self.config else 128_000
        return json.dumps({
            "session_id": session_id,
            "model": "Claude Opus 4.6 / Sonnet 4.6",
            "context_window": context_window,
            "max_output": max_output,
            "messages_count": message_count,
            "estimated_tokens": estimated_tokens,
            "estimated_percentage": round(estimated_pct, 1),
            "status": status,
            "status_display": f"{status_emoji} {status.title()}",
            "recommendation": recommendation,
        }, indent=2, sort_keys=True)

    def _terminal_list_response(self) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            sessions = self.terminal_bridge.list_sessions()
        except Exception as exc:
            return f"terminal list error: {exc}"
        return json.dumps({"sessions": sessions}, indent=2, sort_keys=True)

    def _terminal_open_response(self, tool: str, *, cwd: str | None) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.open(tool, cwd=cwd)
        except Exception as exc:
            return f"terminal open error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_status_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.status(session_id)
        except Exception as exc:
            return f"terminal status error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_read_response(self, session_id: str, *, offset: int) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.read(session_id, offset=offset, limit=3000)
        except Exception as exc:
            return f"terminal read error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_send_response(self, session_id: str, text: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.send(session_id, text)
        except Exception as exc:
            return f"terminal send error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_close_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.close(session_id)
        except Exception as exc:
            return f"terminal close error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _chrome_pages_response(self) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            pages = self.browser.connect_to_chrome()
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)

    def _chrome_browse_response(self, url: str) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        if self.browser is None:
            return "browser unavailable"
        try:
            result = self.browser.chrome_navigate(normalized_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome browse error")
        return f"**{result.title}** ({result.url})\n\n{result.content[:3000]}"

    def _chrome_shot_response(self, command: str) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            result = self.browser.chrome_screenshot()
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome screenshot error")
        return json.dumps({
            "url": result.url,
            "title": result.title,
            "screenshot_path": result.screenshot_path,
        }, indent=2)

    def _screen_response(self) -> str:
        if self.computer is None:
            return "computer use unavailable"
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"screenshot error: {exc}"
        return json.dumps({"screenshot_data": screenshot["data"][:100] + "...", "media_type": screenshot["media_type"]})

    def _computer_response(self, instruction: str, session_id: str) -> str:
        if self.computer is None:
            return "computer use unavailable"
        if _computer_instruction_requires_actions(instruction):
            return self._computer_action_response(instruction, session_id)
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"computer screenshot error: {exc}"

        content_blocks = [
            {"type": "text", "text": instruction},
            {"type": "image", "source": {"type": "base64", **screenshot}},
        ]
        memory_text = f"[Screenshot de escritorio]\n{instruction.strip()}"
        return self.brain.handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
        ).content

    def _computer_action_response(self, instruction: str, session_id: str) -> str:
        # Try browser_use (OpenAI) first — no Anthropic API credits needed
        if self.browser_use is not None:
            try:
                import asyncio
                result = asyncio.run(self.browser_use.run_task(instruction))
                return result
            except Exception as exc:
                logger.warning("browser_use fallback failed: %s", exc)

        from claw_v2.computer import ComputerSession

        active = self._computer_sessions.get(session_id)
        if active is not None:
            active.status = "aborted"

        session = ComputerSession(task=instruction)
        self._computer_sessions[session_id] = session
        return self._run_computer_session(session_id)

    def _run_computer_session(self, session_id: str) -> str:
        session = self._computer_sessions.get(session_id)
        if session is None:
            return "no active computer session"
        try:
            client = self._get_computer_client()
            gate = self._get_computer_gate()
            result = self.computer.run_agent_loop(
                session=session,
                client=client,
                gate=gate,
                model=self.computer_model,
                system_prompt=self.computer_system_prompt,
            )
        except Exception as exc:
            self._computer_sessions.pop(session_id, None)
            logger.exception("computer use failed for %s", session_id)
            if self.observe is not None:
                self.observe.emit("error", payload={"source": "computer_use", "error": str(exc)[:200]})
            return f"computer use error: {exc}"

        if session.status == "awaiting_approval":
            pending = dict(session.pending_action or {})
            pending_approval = self.approvals.create(
                action=pending.get("action", "computer_action"),
                summary=_format_computer_pending_summary(session.task, pending),
                metadata={
                    "kind": "computer_use",
                    "session_id": session_id,
                    "task": session.task,
                    "pending_action": pending,
                },
            )
            session.pending_action = {
                **pending,
                "approval_id": pending_approval.approval_id,
                "approval_token": pending_approval.token,
            }
            return (
                f"{result}\n\n"
                f"Approve via: `/action_approve {pending_approval.approval_id} {pending_approval.token}`\n"
                f"Abort via: `/action_abort {pending_approval.approval_id}`"
            )

        if session.status in {"done", "aborted"}:
            self._computer_sessions.pop(session_id, None)
        return result

    def _get_computer_client(self) -> Any:
        if self._computer_client is not None:
            return self._computer_client
        if self.computer_client_factory is not None:
            self._computer_client = self.computer_client_factory()
            return self._computer_client
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - dependency is installed in runtime
            raise RuntimeError("anthropic SDK is not installed") from exc
        api_key = os.getenv("ANTHROPIC_API_KEY") or _read_env_var_from_zsh("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        self._computer_client = Anthropic(api_key=api_key)
        return self._computer_client

    def _get_computer_gate(self) -> Any:
        if self.computer_gate is not None:
            return self.computer_gate
        from claw_v2.computer_gate import ActionGate

        sensitive_urls = getattr(self.config, "sensitive_urls", []) if self.config is not None else []
        self.computer_gate = ActionGate(sensitive_urls=sensitive_urls)
        return self.computer_gate

    def _action_approve_response(self, approval_id: str, token: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            valid = self.approvals.approve(approval_id, token)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        if not valid:
            return "invalid token"
        payload = self.approvals.read(approval_id)
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "computer_use":
            return "approved"
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str):
            return "approved, but no computer session metadata was found"
        session = self._computer_sessions.get(session_id)
        if session is None:
            return "approved, but the computer session is no longer active"
        if session.pending_action is not None and session.pending_action.get("approval_id") != approval_id:
            return "approved, but no matching pending computer action was found"
        session.status = "running"
        return self._run_computer_session(session_id)

    def _action_abort_response(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            payload = self.approvals.read(approval_id)
            self.approvals.reject(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        metadata = payload.get("metadata", {})
        if metadata.get("kind") == "computer_use":
            session_id = metadata.get("session_id")
            if isinstance(session_id, str):
                session = self._computer_sessions.pop(session_id, None)
                if session is not None:
                    session.status = "aborted"
            return "computer action rejected"
        return "action rejected"

    # -- Buddy handlers --------------------------------------------------------

    def _buddy_hatch_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        state = self.buddy.hatch(user_id)
        return f"{state.species_emoji} Hatched **{state.species_name}** ({state.rarity})!\n{self.buddy.show_card(user_id)}"

    def _buddy_card_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.show_card(user_id)

    def _buddy_stats_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.show_stats(user_id)

    def _buddy_rename_response(self, user_id: str, new_name: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.rename(user_id, new_name)

    def _computer_abort_response(self, session_id: str) -> str:
        session = self._computer_sessions.pop(session_id, None)
        if session is None:
            return "no active computer session"
        session.status = "aborted"
        return "computer session aborted"

    def _maybe_handle_shortcut(self, text: str, *, session_id: str) -> str | None:
        if not text or text.startswith("/"):
            return None

        normalized = _normalize_command_text(text)
        extracted_url = _extract_url_candidate(text)

        if extracted_url is not None:
            if "chrome" in normalized and (any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url)):
                return self._chrome_browse_response(extracted_url)
            if any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_response(extracted_url)

        if _computer_instruction_requires_actions(text):
            return self._computer_action_response(text, session_id)

        if _looks_like_computer_read_request(normalized):
            return self._computer_response(text, session_id)

        if any(token in normalized for token in ("abre", "abrir", "open", "inicia", "iniciar", "run", "corre")):
            if "terminal" in normalized and "claude" in normalized:
                return self._terminal_open_response("claude", cwd=None)
            if "terminal" in normalized and "codex" in normalized:
                return self._terminal_open_response("codex", cwd=None)

        if "google ads" in normalized or "ads.google.com" in normalized:
            if any(token in normalized for token in ("abre", "abrir", "open", "revisa", "revisa", "revisalo", "review", "check")):
                return self._chrome_browse_response("https://ads.google.com")

        return None


def _parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise ValueError("toggle must be one of: on, off")


def _normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _read_env_var_from_zsh(name: str) -> str | None:
    direct = _read_env_var_from_shell_files(name)
    if direct:
        return direct
    try:
        result = subprocess.run(
            ["/bin/zsh", "-ic", f"printf %s \"${name}\""],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    value = lines[-1]
    if "=" in value and not value.startswith("sk-"):
        value = value.split("=", 1)[1].strip()
    value = value.strip().strip("\"'")
    return value or None


def _read_env_var_from_shell_files(name: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}=(?P<value>.+?)\s*$")
    for path in (
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".zshenv",
        Path.home() / ".profile",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group("value").strip().strip("\"'")
            if value:
                return value
    return None


def _computer_instruction_requires_actions(text: str) -> bool:
    normalized = _normalize_command_text(text)
    return any(token in normalized for token in _COMPUTER_ACTION_TOKENS)


def _looks_like_computer_read_request(normalized: str) -> bool:
    return any(token in normalized for token in _COMPUTER_READ_TOKENS)


def _extract_url_candidate(text: str) -> str | None:
    for pattern in (_SCHEME_URL_RE, _HOST_URL_RE):
        match = pattern.search(text)
        if match is None:
            continue
        candidate = _strip_url_punctuation(match.group("url"))
        if candidate:
            return candidate
    return None


def _strip_url_punctuation(value: str) -> str:
    candidate = value.strip().strip("<>[]{}\"'")
    while candidate and candidate[-1] in ".,;:!?":
        candidate = candidate[:-1]
    return candidate


def _looks_like_standalone_url(text: str, url: str) -> bool:
    remainder = text.replace(url, " ", 1)
    remainder = re.sub(r"[\s`\"'“”‘’<>()\[\]{}.,;:!?-]+", "", remainder)
    return remainder == ""


_LOGIN_WALL_SIGNALS = [
    "log in\nsign up",
    "sign in\nsign up",
    "create an account",
    "don't miss what's happening",
    "join today",
    "this page is not available",
]


def _is_login_wall(content: str) -> bool:
    """Detect pages that returned a login/signup wall instead of real content."""
    if not content or len(content.strip()) < 80:
        return True
    lower = content.lower()
    return any(signal in lower for signal in _LOGIN_WALL_SIGNALS)


def _normalize_url(value: str) -> str:
    candidate = _strip_url_punctuation(value)
    if not candidate:
        raise ValueError("usage: /browse <url>")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid url")
    return candidate


def _format_computer_pending_summary(task: str, pending_action: dict[str, Any]) -> str:
    action = pending_action.get("action", "unknown")
    if "coordinate" in pending_action:
        return f"{task} — {action} at {pending_action['coordinate']}"
    if "text" in pending_action:
        return f"{task} — {action} {pending_action['text']!r}"
    return f"{task} — {action}"


def _format_chrome_cdp_error(exc: Exception, *, prefix: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("9222", "econnrefused", "connection refused", "connect_over_cdp", "browser_type.connect_over_cdp")):
        return (
            "Chrome no esta exponiendo CDP en `9222`.\n\n"
            "En Chrome 136+ la bandera `--remote-debugging-port` se ignora sobre el perfil normal si no pasas tambien `--user-data-dir`.\n\n"
            "Si quieres CDP, cierra Chrome completamente y abrelo desde Terminal con un perfil aparte:\n\n"
            "```bash\n"
            "/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
            "  --remote-debugging-port=9222 \\\n"
            "  --user-data-dir=/tmp/chrome-cdp-profile\n"
            "```\n\n"
            "Ese perfil NO reutiliza tu sesion autenticada actual. Si necesitas controlar tu Chrome ya logueado, usa `/computer`."
        )
    return f"{prefix}: {exc}"


def _parse_non_negative_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return parsed


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
