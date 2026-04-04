from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
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
_NLM_CREATE_RE = re.compile(
    r"^\s*(?:por favor\s+)?(?:(?:cr[eé]a(?:me)?)|(?:genera(?:me)?)|(?:haz(?:me)?)|quiero|necesito)\s+"
    r"(?:un\s+)?(?:cuaderno|notebook)(?:\s+(?:sobre|de))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
_NLM_ARTIFACT_KINDS = {
    "podcast": "podcast",
    "infografia": "infographic",
    "infographic": "infographic",
    "video": "video",
}
_NLM_ACTION_TOKENS = (
    "crea",
    "creame",
    "genera",
    "generame",
    "haz",
    "hazme",
    "quiero",
    "necesito",
)
_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()]+)", re.IGNORECASE)
_HOST_URL_RE = re.compile(
    r"(?P<url>(?<!@)(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})(?::\d+)?(?:[/?#][^\s<>()]*)?)",
    re.IGNORECASE,
)
_DEFAULT_COMPUTER_MODEL = "gpt-5.4-mini"
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
        self.learning: Any | None = None
        self._state_lock = threading.Lock()
        self._computer_sessions: dict[str, Any] = {}
        self._computer_client: Any | None = None
        self.notebooklm: Any | None = None
        self._active_notebooks: dict[str, dict[str, str]] = {}
        self.managed_chrome: Any | None = None
        self._recent_browse_urls: dict[str, str] = {}

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        stripped = text.strip()
        if stripped == "/help":
            return self._help_response()
        if stripped.startswith("/help "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return self._help_response()
            return self._help_response(parts[1])
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
            return self._browse_response(parts[1], session_id=session_id)
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
            return self._chrome_browse_response(parts[1], session_id=session_id)
        if stripped.startswith("/chrome_shot"):
            return self._chrome_shot_response(stripped)
        if stripped == "/chrome_login":
            return self._chrome_login_response()
        if stripped == "/chrome_headless":
            return self._chrome_headless_response()
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
            # Store the exchange so the brain has context on subsequent messages.
            self.brain.memory.store_message(session_id, "user", stripped)
            self.brain.memory.store_message(session_id, "assistant", shortcut_response[:2000])
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
        if stripped.startswith("/feedback"):
            if self.learning is None:
                return "learning loop not available"
            parts = stripped.split(maxsplit=2)
            if len(parts) < 2:
                return "usage: /feedback <positive|negative|note> [outcome_id]"
            rating = parts[1]
            oid = int(parts[2]) if len(parts) == 3 else None
            return self.learning.feedback(oid, rating)
        if stripped.startswith("/pipeline_merge "):
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
        if stripped in ("/pipeline", "/pipeline_approve", "/pipeline_merge", "/social_preview", "/social_publish", "/feedback"):
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
        # --- NotebookLM commands ---
        if stripped.startswith("/nlm_"):
            return self._nlm_dispatch(session_id, stripped)

        nlm_response = self._nlm_natural_language_response(session_id, stripped)
        if nlm_response is not None:
            return nlm_response

        # Pre-fetch tweet content so the brain has it in context,
        # but store only the original message in memory (avoid polluting history).
        enriched = _enrich_tweet_urls(stripped)
        if enriched != stripped:
            response = self.brain.handle_message(
                session_id, enriched, memory_text=stripped,
            )
        else:
            response = self.brain.handle_message(session_id, stripped)

        raw_content = response.content or ""
        content = raw_content.strip()
        if not content or content == "(no result)":
            content = "Recibido. ¿Qué quieres que haga con esto?"
        if content != raw_content:
            self.brain.memory.replace_latest_assistant_message(session_id, raw_content, content)
        return content

    def _help_response(self, topic: str | None = None) -> str:
        if topic is None:
            return (
                "Comandos principales:\n"
                "/status - salud general del bot\n"
                "/approvals - aprobaciones pendientes\n"
                "/pipeline_status - pipelines activos\n"
                "/agents - estado de agentes\n"
                "/browse <url> - revisar una URL\n"
                "/screen - screenshot actual\n"
                "/computer <instruccion> - operar el escritorio\n"
                "/terminal_list - sesiones PTY abiertas\n"
                "/nlm_create <tema> - crear cuaderno de NotebookLM\n"
                "/nlm_list - listar cuadernos de NotebookLM\n"
                "\n"
                "Ayuda por tema:\n"
                "/help approvals\n"
                "/help pipeline\n"
                "/help agents\n"
                "/help terminal\n"
                "/help browser\n"
                "/help social\n"
                "/help notebooklm"
            )

        normalized = topic.strip().lower().replace("-", "").replace("_", "")
        if normalized in {"approval", "approvals"}:
            return (
                "Aprobaciones:\n"
                "/approvals - listar pendientes\n"
                "/approval_status <approval_id> - ver estado\n"
                "/approve <approval_id> <token> - aprobar manualmente\n"
                "/action_approve <approval_id> <token> - aprobar acción pendiente\n"
                "/action_abort <approval_id> - abortar acción pendiente"
            )
        if normalized in {"pipeline", "pipelines"}:
            return (
                "Pipeline:\n"
                "/pipeline <issue_id> [repo_root] - ejecutar pipeline\n"
                "/pipeline_status - ver runs activos\n"
                "/pipeline_approve <approval_id> <token> - aprobar y completar pipeline\n"
                "/pipeline_merge <issue_id> - merge y cierre"
            )
        if normalized in {"agent", "agents"}:
            return (
                "Agentes:\n"
                "/agents - listar agentes\n"
                "/agent_status <agent_name> - ver detalle\n"
                "/agent_create <name> <researcher|operator|deployer> <instruction> - crear agente\n"
                "/agent_run <agent_name> <max_experiments> - correr loop\n"
                "/agent_publish <agent_name> <max_experiments> - publicar cambios\n"
                "/agent_pr <agent_name> <max_experiments> - abrir draft PR\n"
                "/agent_pause <agent_name> - pausar\n"
                "/agent_resume <agent_name> - reanudar\n"
                "/agent_history <agent_name> [limit] - historial reciente"
            )
        if normalized in {"terminal", "terminals", "pty"}:
            return (
                "Terminal:\n"
                "/terminal_list - listar sesiones\n"
                "/terminal_open <claude|codex> [cwd] - abrir PTY\n"
                "/terminal_status <session_id> - ver estado\n"
                "/terminal_read <session_id> [offset] - leer salida\n"
                "/terminal_send <session_id> <text> - enviar texto\n"
                "/terminal_close <session_id> - cerrar sesión"
            )
        if normalized in {"browser", "browse", "chrome", "screen", "computer"}:
            return (
                "Navegación y escritorio:\n"
                "/browse <url> - revisar una URL\n"
                "/chrome_pages - listar tabs de Chrome\n"
                "/chrome_browse <url> - abrir URL en Chrome\n"
                "/chrome_shot - screenshot del tab actual\n"
                "/chrome_login - abrir Chrome visible para login\n"
                "/chrome_headless - volver a headless\n"
                "/screen - screenshot del escritorio\n"
                "/computer <instruccion> - controlar escritorio\n"
                "/computer_abort - cancelar sesión activa"
            )
        if normalized in {"social", "socialmedia"}:
            return (
                "Social:\n"
                "/social_status - listar cuentas\n"
                "/social_preview <cuenta> - previsualizar posts\n"
                "/social_publish <cuenta> - publicar batch"
            )
        if normalized in {"notebooklm", "nlm", "notebook"}:
            return (
                "NotebookLM:\n"
                "/nlm_list - listar cuadernos\n"
                "/nlm_create <tema> - crear cuaderno\n"
                "/nlm_status <notebook_id> - ver estado\n"
                "/nlm_sources <notebook_id> <url1,url2,...> - agregar fuentes\n"
                "/nlm_text <notebook_id> <titulo> | <contenido> - agregar nota\n"
                "/nlm_research <notebook_id> <consulta> - correr research\n"
                "/nlm_podcast [notebook_id] - generar podcast\n"
                "/nlm_chat <notebook_id> <pregunta> - chatear con el cuaderno"
            )
        return (
            f"Tema de ayuda no reconocido: {topic}\n"
            "Prueba con: approvals, pipeline, agents, terminal, browser, social, notebooklm."
        )

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

    def _browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self._remember_recent_browse_url(session_id, normalized_url)

        backend = self._browse_backend()
        playwright_available = backend in {"auto", "playwright_local"} and self.browser is not None
        browserbase_available = (
            backend == "browserbase_cdp"
            and self.browser is not None
            and self.config is not None
            and bool(getattr(self.config, "browserbase_api_key", None))
            and bool(getattr(self.config, "browserbase_project_id", None))
        )
        cdp_available = (
            backend in {"auto", "chrome_cdp"}
            and self.managed_chrome is not None
            and self.browser is not None
        )
        tweet_fallback = _tweet_fxtwitter_read(normalized_url) if _is_tweet_url(normalized_url) else ""
        auth_required = _needs_real_browser(normalized_url)
        started_at = time.perf_counter()

        if auth_required:
            response, outcome = self._browse_authenticated_response(
                normalized_url,
                tweet_fallback=tweet_fallback,
                configured_backend=backend,
                cdp_available=cdp_available,
                browserbase_available=browserbase_available,
                playwright_available=playwright_available,
            )
        else:
            response, outcome = self._browse_public_response(
                normalized_url,
                configured_backend=backend,
                cdp_available=cdp_available,
                browserbase_available=browserbase_available,
                playwright_available=playwright_available,
            )

        self._emit_browse_event(
            url=normalized_url,
            configured_backend=backend,
            strategy=outcome["strategy"],
            selected_backend=outcome["selected_backend"],
            status=outcome["status"],
            auth_required=auth_required,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            note=outcome.get("note"),
        )
        return response

    def _browse_backend(self) -> str:
        if self.config is None:
            return "auto"
        backend = getattr(self.config, "browse_backend", "auto")
        if not isinstance(backend, str) or not backend.strip():
            return "auto"
        return backend.strip().lower()

    def _browse_public_response(
        self,
        url: str,
        *,
        configured_backend: str,
        cdp_available: bool,
        browserbase_available: bool,
        playwright_available: bool,
    ) -> tuple[str, dict[str, str]]:
        content = _jina_read(url)
        if content:
            return content[:6000], {
                "strategy": "public",
                "selected_backend": "jina",
                "status": "success",
            }

        if playwright_available:
            content = self._playwright_browse_response(url)
            if content:
                return content, {
                    "strategy": "public",
                    "selected_backend": "playwright_local",
                    "status": "success",
                }

        if browserbase_available:
            content = self._browserbase_browse_response(url)
            if content:
                return content, {
                    "strategy": "public",
                    "selected_backend": "browserbase_cdp",
                    "status": "success",
                }

        if configured_backend == "chrome_cdp" and cdp_available:
            try:
                result = self.browser.chrome_navigate(
                    url,
                    cdp_url=self.managed_chrome.cdp_url,
                )
                if _is_usable_browse_content(url, result.content):
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}", {
                        "strategy": "public",
                        "selected_backend": "chrome_cdp",
                        "status": "success",
                    }
            except Exception as exc:
                return _format_chrome_cdp_error(exc, prefix="browse error"), {
                    "strategy": "public",
                    "selected_backend": "chrome_cdp",
                    "status": "error",
                    "note": "cdp_failed",
                }

        return f"browse error: no se pudo leer {url}", {
            "strategy": "public",
            "selected_backend": "none",
            "status": "error",
            "note": "all_backends_failed",
        }

    def _browse_authenticated_response(
        self,
        url: str,
        *,
        tweet_fallback: str,
        configured_backend: str,
        cdp_available: bool,
        browserbase_available: bool,
        playwright_available: bool,
    ) -> tuple[str, dict[str, str]]:
        if cdp_available:
            try:
                from urllib.parse import urlparse
                host = urlparse(url).netloc.lower()
                result = self.browser.chrome_navigate(
                    url,
                    cdp_url=self.managed_chrome.cdp_url,
                    page_url_pattern=host,
                )
                if _is_usable_browse_content(url, result.content):
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}", {
                        "strategy": "authenticated",
                        "selected_backend": "chrome_cdp",
                        "status": "success",
                    }
            except Exception as exc:
                if configured_backend == "chrome_cdp":
                    return _format_chrome_cdp_error(exc, prefix="browse error"), {
                        "strategy": "authenticated",
                        "selected_backend": "chrome_cdp",
                        "status": "error",
                        "note": "cdp_failed",
                    }

        if browserbase_available:
            content = self._browserbase_browse_response(url)
            if content:
                return f"Contenido parcial (sesión remota sin cookies locales):\n\n{content}", {
                    "strategy": "authenticated",
                    "selected_backend": "browserbase_cdp",
                    "status": "partial",
                    "note": "remote_session_no_local_cookies",
                }

        fallback_content, fallback_backend, note = self._browse_textual_fallback(
            url,
            tweet_fallback=tweet_fallback,
            playwright_available=playwright_available,
        )
        if fallback_content:
            return fallback_content, {
                "strategy": "authenticated",
                "selected_backend": fallback_backend,
                "status": "partial",
                "note": note,
            }

        return f"browse error: {url} requiere navegador autenticado y Chrome CDP no está disponible.", {
            "strategy": "authenticated",
            "selected_backend": "none",
            "status": "error",
            "note": "no_authenticated_backend",
        }

    def _browse_textual_fallback(
        self,
        url: str,
        *,
        tweet_fallback: str,
        playwright_available: bool,
    ) -> tuple[str, str, str]:
        if tweet_fallback:
            return tweet_fallback[:6000], "tweet_fallback", "tweet_reader"

        if playwright_available:
            content = self._playwright_browse_response(url)
            if content:
                return (
                    f"Contenido parcial (sin sesión autenticada):\n\n{content}",
                    "playwright_local",
                    "no_authenticated_session",
                )

        content = _jina_read(url)
        if content:
            return (
                f"Contenido parcial (CDP no disponible):\n\n{content[:6000]}",
                "jina",
                "best_effort_textual",
            )
        return "", "none", "all_textual_fallbacks_failed"

    def _playwright_browse_response(self, url: str) -> str:
        if self.browser is None or not hasattr(self.browser, "browse"):
            return ""
        try:
            result = self.browser.browse(url)
        except Exception:
            logger.debug("Playwright local browse failed for %s", url, exc_info=True)
            return ""
        if not _is_usable_browse_content(url, result.content):
            return ""
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _browserbase_browse_response(self, url: str) -> str:
        if self.browser is None or self.config is None or not hasattr(self.browser, "browserbase_browse"):
            return ""
        api_key = getattr(self.config, "browserbase_api_key", None)
        project_id = getattr(self.config, "browserbase_project_id", None)
        if not api_key or not project_id:
            return ""
        try:
            result = self.browser.browserbase_browse(
                url,
                api_key=api_key,
                project_id=project_id,
                api_url=getattr(self.config, "browserbase_api_url", "https://api.browserbase.com"),
                region=getattr(self.config, "browserbase_region", None),
                keep_alive=bool(getattr(self.config, "browserbase_keep_alive", False)),
            )
        except Exception:
            logger.debug("Browserbase browse failed for %s", url, exc_info=True)
            return ""
        if not _is_usable_browse_content(url, result.content):
            return ""
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _emit_browse_event(
        self,
        *,
        url: str,
        configured_backend: str,
        strategy: str,
        selected_backend: str,
        status: str,
        auth_required: bool,
        duration_ms: float,
        note: str | None = None,
    ) -> None:
        if self.observe is None:
            return
        payload = {
            "url": url,
            "configured_backend": configured_backend,
            "strategy": strategy,
            "selected_backend": selected_backend,
            "status": status,
            "auth_required": auth_required,
            "duration_ms": duration_ms,
        }
        if note:
            payload["note"] = note
        self.observe.emit("browse_result", payload=payload)

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
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            pages = self.browser.connect_to_chrome(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)

    def _chrome_browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self._remember_recent_browse_url(session_id, normalized_url)
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        tweet_fallback = _tweet_fxtwitter_read(normalized_url) if _is_tweet_url(normalized_url) else ""
        try:
            from urllib.parse import urlparse
            host = urlparse(normalized_url).netloc.lower()
            result = self.browser.chrome_navigate(
                normalized_url,
                cdp_url=self.managed_chrome.cdp_url,
                page_url_pattern=host,
            )
        except Exception as exc:
            if tweet_fallback:
                return tweet_fallback[:6000]
            return _format_chrome_cdp_error(exc, prefix="chrome browse error")
        if not _is_usable_browse_content(normalized_url, result.content) and tweet_fallback:
            return tweet_fallback[:6000]
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _chrome_shot_response(self, command: str) -> str:
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            result = self.browser.chrome_screenshot(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome screenshot error")
        return json.dumps({
            "url": result.url,
            "title": result.title,
            "screenshot_path": result.screenshot_path,
        }, indent=2)

    def _chrome_login_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=False)
            return "Chrome reiniciado en modo visible. Haz login en los sitios que necesites. Cuando termines: /chrome_headless"
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

    def _chrome_headless_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=True)
            return "Chrome reiniciado en modo headless."
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

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
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(asyncio.run, self.browser_use.run_task(instruction)).result(timeout=120)
                else:
                    result = asyncio.run(self.browser_use.run_task(instruction))
                return result
            except Exception as exc:
                logger.warning("browser_use fallback failed: %s", exc)

        from claw_v2.computer import ComputerSession

        with self._state_lock:
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
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is installed in runtime
            raise RuntimeError("openai SDK is not installed") from exc
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for computer use")
        self._computer_client = OpenAI(api_key=api_key)
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
                return self._chrome_browse_response(extracted_url, session_id=session_id)
            if any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_response(extracted_url, session_id=session_id)

        if _looks_like_tweet_followup_request(normalized):
            recent_tweet_url = self._recent_tweet_url(session_id)
            if recent_tweet_url is not None:
                return self._browse_response(recent_tweet_url, session_id=session_id)

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
                return self._chrome_browse_response("https://ads.google.com", session_id=session_id)

        return None

    def _remember_recent_browse_url(self, session_id: str | None, url: str) -> None:
        if session_id:
            self._recent_browse_urls[session_id] = url

    def _recent_tweet_url(self, session_id: str) -> str | None:
        url = self._recent_browse_urls.get(session_id)
        if url and _is_tweet_url(url):
            return url
        return None

    # -- NotebookLM handlers ----------------------------------------------------

    def _nlm_dispatch(self, session_id: str, command: str) -> str:
        if self.notebooklm is None:
            return "NotebookLM no disponible. El servicio no está configurado."
        try:
            if command == "/nlm_list":
                return self._nlm_list_response()
            if command.startswith("/nlm_create "):
                title = command.split(maxsplit=1)[1]
                return self._nlm_create_response(session_id, title)
            if command == "/nlm_create":
                return "usage: /nlm_create <titulo>"
            if command.startswith("/nlm_delete "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_delete_response(nb_id)
            if command == "/nlm_delete":
                return "usage: /nlm_delete <notebook_id>"
            if command.startswith("/nlm_status "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_status_response(nb_id)
            if command == "/nlm_status":
                return "usage: /nlm_status <notebook_id>"
            if command.startswith("/nlm_sources "):
                parts = command.split()
                if len(parts) < 3:
                    return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
                return self._nlm_sources_response(parts[1], parts[2:])
            if command == "/nlm_sources":
                return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
            if command.startswith("/nlm_text "):
                rest = command.split(maxsplit=2)
                if len(rest) < 3 or "|" not in rest[2]:
                    return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
                nb_id = rest[1]
                title_and_content = rest[2]
                pipe_idx = title_and_content.index("|")
                title = title_and_content[:pipe_idx].strip()
                content = title_and_content[pipe_idx + 1:].strip()
                return self._nlm_text_response(nb_id, title, content)
            if command == "/nlm_text":
                return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
            if command.startswith("/nlm_research "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_research <notebook_id> <query>"
                return self._nlm_research_response(parts[1], parts[2])
            if command == "/nlm_research":
                return "usage: /nlm_research <notebook_id> <query>"
            if command.startswith("/nlm_podcast "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_podcast_response(session_id, nb_id)
            if command == "/nlm_podcast":
                return self._nlm_podcast_response(session_id, None)
            if command.startswith("/nlm_chat "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_chat <notebook_id> <pregunta>"
                return self._nlm_chat_response(parts[1], parts[2])
            if command == "/nlm_chat":
                return "usage: /nlm_chat <notebook_id> <pregunta>"
            return "Comando NLM no reconocido. Disponibles: /nlm_list, /nlm_create, /nlm_delete, /nlm_status, /nlm_sources, /nlm_text, /nlm_research, /nlm_podcast, /nlm_chat"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("NLM command error")
            return f"Error en NotebookLM: {exc}"

    def _nlm_list_response(self) -> str:
        notebooks = self.notebooklm.list_notebooks()
        if not notebooks:
            return "No hay notebooks."
        lines = []
        for nb in notebooks:
            short_id = nb["id"][:8]
            date = nb.get("created_at") or "-"
            lines.append(f"{short_id}  {nb['title']}  {date}")
        return "\n".join(lines)

    def _nlm_create_response(self, session_id: str, title: str) -> str:
        result = self.notebooklm.create_notebook(title)
        self._set_active_notebook(session_id, result["id"], result["title"])
        research = self.notebooklm.start_research(result["id"], result["title"])
        return (
            f"Notebook creado: {result['id'][:8]} — {result['title']}\n"
            f"{research}\n"
            "Queda como cuaderno activo para esta conversación."
        )

    def _nlm_delete_response(self, notebook_id: str) -> str:
        self.notebooklm.delete_notebook(notebook_id)
        return "Notebook eliminado."

    def _nlm_status_response(self, notebook_id: str) -> str:
        info = self.notebooklm.status(notebook_id)
        nb = info["notebook"]
        lines = [f"Notebook: {nb['title']} ({nb['id'][:8]})", f"Sources: {nb['sources_count']}"]
        for src in info["sources"]:
            lines.append(f"  - {src['title']} [{src['kind']}]")
        return "\n".join(lines)

    def _nlm_sources_response(self, notebook_id: str, urls: list[str]) -> str:
        results = self.notebooklm.add_sources(notebook_id, urls)
        lines = [f"{len(results)} source(s) agregados:"]
        for src in results:
            lines.append(f"  - {src['title']}")
        return "\n".join(lines)

    def _nlm_text_response(self, notebook_id: str, title: str, content: str) -> str:
        if not title.strip() or not content.strip():
            return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
        result = self.notebooklm.add_text(notebook_id, title, content)
        return f"Source de texto agregado: {result['title']}"

    def _nlm_research_response(self, notebook_id: str, query: str) -> str:
        return self.notebooklm.start_research(notebook_id, query)

    def _nlm_podcast_response(self, session_id: str, notebook_id: str | None) -> str:
        target = notebook_id or self._active_notebook_id(session_id)
        if target is None:
            return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
        self._set_active_notebook(session_id, target)
        return self.notebooklm.start_podcast(target)

    def _nlm_chat_response(self, notebook_id: str, question: str) -> str:
        return self.notebooklm.chat(notebook_id, question)

    def _nlm_natural_language_response(self, session_id: str, text: str) -> str | None:
        if self.notebooklm is None or not text or text.startswith("/"):
            return None
        if topic := _extract_nlm_create_topic(text):
            return self._nlm_create_response(session_id, topic)
        if kind := _extract_nlm_artifact_kind(text):
            target = self._active_notebook_id(session_id)
            if target is None:
                return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
            self._set_active_notebook(session_id, target)
            return self.notebooklm.start_artifact(target, kind)
        return None

    def _set_active_notebook(self, session_id: str, notebook_id: str, title: str | None = None) -> None:
        current = self._active_notebooks.get(session_id, {})
        self._active_notebooks[session_id] = {
            "id": notebook_id,
            "title": title or current.get("title", notebook_id[:8]),
        }

    def _active_notebook_id(self, session_id: str) -> str | None:
        notebook = self._active_notebooks.get(session_id)
        if notebook is None:
            return None
        return notebook["id"]


def _parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise ValueError("toggle must be one of: on, off")


def _extract_nlm_create_topic(text: str) -> str | None:
    match = _NLM_CREATE_RE.match(text.strip())
    if not match:
        return None
    topic = match.group(1).strip()
    return topic or None


def _extract_nlm_artifact_kind(text: str) -> str | None:
    normalized = _normalize_command_text(text)
    if not any(token in normalized for token in _NLM_ACTION_TOKENS):
        return None
    for token, kind in _NLM_ARTIFACT_KINDS.items():
        if token in normalized:
            return kind
    return None


def _normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _looks_like_api_key(value: str) -> bool:
    """Reject values that look like env dumps or contain newlines."""
    if "\n" in value or "\r" in value:
        return False
    if "=" in value and not value.startswith("sk-"):
        return False
    if len(value) > 300:
        return False
    return True


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


# Domains that require real browser cookies (auth) or heavy JS rendering.
# Headless browsers always hit login walls on these.
_AUTH_DOMAINS = frozenset({
    "x.com", "twitter.com",
    "instagram.com",
    "facebook.com", "fb.com",
    "linkedin.com",
    "reddit.com",
    "tiktok.com",
    "threads.net",
    "mail.google.com",
    "web.whatsapp.com",
})


def _needs_real_browser(url: str) -> bool:
    """Check if URL needs Chrome CDP (auth cookies + JS) instead of headless."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    return any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS)


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
    if not content or not content.strip():
        return True
    lower = content.lower()
    return any(signal in lower for signal in _LOGIN_WALL_SIGNALS)


_RAW_MARKUP_SIGNALS = [
    "<meta",
    "<link rel",
    "dns-prefetch",
    "preconnect",
    "viewport-fit=cover",
    "user-scalable=0",
]


def _enrich_tweet_urls(text: str) -> str:
    """If text contains tweet URLs, pre-fetch content and append to message."""
    urls = _SCHEME_URL_RE.findall(text)
    tweet_urls = [_strip_url_punctuation(u) for u in urls if _is_tweet_url(_strip_url_punctuation(u))]
    if not tweet_urls:
        return text
    enriched = text
    for url in tweet_urls:
        content = _tweet_fxtwitter_read(url)
        if content:
            enriched += f"\n\n---\n[Contenido del tweet pre-cargado]:\n{content}"
    return enriched


def _is_tweet_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    host = parsed.netloc.lower()
    if not any(host == domain or host.endswith(f".{domain}") for domain in ("x.com", "twitter.com")):
        return False
    return "/status/" in parsed.path


def _looks_like_tweet_followup_request(normalized_text: str) -> bool:
    if not any(token in normalized_text for token in _BROWSE_SHORTCUT_TOKENS):
        return False
    return any(token in normalized_text for token in ("tweet", "tweets", "tuit", "tuits", "post"))


def _looks_like_raw_markup(content: str) -> bool:
    lower = content.lower()
    return any(signal in lower for signal in _RAW_MARKUP_SIGNALS)


def _is_usable_browse_content(url: str, content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if _is_login_wall(stripped):
        return False
    if _is_tweet_url(url) and _looks_like_raw_markup(stripped):
        return False
    return True


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


def _jina_read(url: str, *, timeout: float = 10) -> str:
    """Fetch URL content as markdown via Jina Reader."""
    import httpx
    try:
        response = httpx.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/markdown"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        content = response.text.strip()
        if len(content) < 20:
            return ""
        if _is_login_wall(content):
            return ""
        return content
    except Exception:
        return ""


def _tweet_fxtwitter_read(url: str, *, timeout: float = 15) -> str:
    """Fetch full tweet text via fxtwitter public API."""
    if not _is_tweet_url(url):
        return ""
    import httpx

    match = re.search(r"/status/(\d+)", url)
    if not match:
        return ""
    # Extract username from URL path
    parsed = urlsplit(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        return ""
    user, tweet_id = path_parts[0], match.group(1)

    try:
        response = httpx.get(
            f"https://api.fxtwitter.com/{user}/status/{tweet_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return _tweet_oembed_fallback(url, timeout=timeout)

    if data.get("code") != 200 or "tweet" not in data:
        return _tweet_oembed_fallback(url, timeout=timeout)

    tweet = data["tweet"]
    text = tweet.get("text", "").strip()
    if not text:
        return ""
    author = tweet.get("author", {}).get("name", "")
    handle = tweet.get("author", {}).get("screen_name", "")
    likes = tweet.get("likes", 0)
    retweets = tweet.get("retweets", 0)
    views = tweet.get("views", 0)
    title = f"{author} (@{handle}) on X" if author else "Tweet"
    stats = f"Likes: {likes:,} | RT: {retweets:,} | Views: {views:,}"
    return f"**{title}** ({url})\n\n{text}\n\n---\n{stats}"


def _tweet_oembed_fallback(url: str, *, timeout: float = 10) -> str:
    """Fallback: oEmbed (may truncate long tweets)."""
    import html as html_lib
    import httpx

    try:
        response = httpx.get(
            "https://publish.twitter.com/oembed",
            params={"url": url, "omit_script": "true", "dnt": "true"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return ""

    raw_html = str(data.get("html", "")).strip()
    if not raw_html:
        return ""
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text_parts: list[str] = []
    for paragraph in paragraphs:
        text = re.sub(r"<br\s*/?>", "\n", paragraph, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html_lib.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            text_parts.append(text)
    if not text_parts:
        return ""
    author_name = str(data.get("author_name", "")).strip()
    title = f"{author_name} on X" if author_name else "Tweet"
    content = "\n\n".join(text_parts)
    return f"**{title}** ({url})\n\n{content}"


def _format_chrome_cdp_error(exc: Exception, *, prefix: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("econnrefused", "connection refused", "connect_over_cdp", "browser_type.connect_over_cdp")):
        return "Chrome del bot no responde. Reinicia el bot o verifica que Chrome esté instalado."
    return f"{prefix}: {message}"


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
