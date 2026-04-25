"""Module-level helper functions and constants extracted from bot.py."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from claw_v2.coordinator import CoordinatorResult, WorkerTask

__all__ = [
    "_AUTH_DOMAINS",
    "_AUTONOMY_ACTION_PATTERNS",
    "_AUTONOMY_MODES",
    "_AUTONOMY_POLICY_MATRIX",
    "_AUTONOMY_TASK_ACTION_PATTERNS",
    "_BROWSE_SHORTCUT_TOKENS",
    "_COMPUTER_ACTION_TOKENS",
    "_COMPUTER_READ_TOKENS",
    "_HOST_URL_RE",
    "_JS_RENDERED_DOMAINS",
    "_LINK_ANALYSIS_SECTION_TITLES",
    "_LINK_ANALYSIS_SHORTCUT_TOKENS",
    "_LOGIN_WALL_SIGNALS",
    "_NLM_ACTION_TOKENS",
    "_NLM_ARTIFACT_KINDS",
    "_NLM_CREATE_RE",
    "_OPTION_ORDINALS",
    "_PROCEED_TOKENS",
    "_RAW_MARKUP_SIGNALS",
    "_RUNTIME_CAPABILITY_SECTION_TITLES",
    "_SCHEME_URL_RE",
    "_TWEET_ANALYSIS_SHORTCUT_TOKENS",
    "_agent_summary",
    "_autonomy_policy_payload",
    "_build_checkpoint",
    "_build_coordinator_tasks",
    "_bulletize_link_analysis_body",
    "_bulletize_runtime_capability_body",
    "_classify_task_actions",
    "_compact_summary",
    "_computer_instruction_requires_actions",
    "_coordinator_checkpoint",
    "_default_pull_request_body",
    "_default_pull_request_title",
    "_default_step_budget",
    "_enforce_link_analysis_sections",
    "_enforce_runtime_capability_sections",
    "_enrich_tweet_urls",
    "_evaluate_autonomy_policy",
    "_extract_link_analysis_context",
    "_extract_nlm_artifact_kind",
    "_extract_nlm_create_topic",
    "_extract_numbered_options",
    "_extract_option_reference",
    "_extract_pending_action_from_reply",
    "_extract_title_from_url",
    "_extract_url_candidate",
    "_extract_verification_status",
    "_filter_task_queue_by_mode",
    "_format_autonomy_policy_block",
    "_format_chrome_cdp_error",
    "_format_computer_pending_summary",
    "_format_coordinator_response",
    "_format_link_analysis_prompt",
    "_format_runtime_capability_prompt",
    "_format_task_approval_response",
    "_format_tweet_analysis_prompt",
    "_format_worker_results",
    "_has_link_analysis_sections",
    "_has_runtime_capability_sections",
    "_has_url_query",
    "_infer_session_mode",
    "_is_local_url",
    "_is_login_wall",
    "_is_tweet_url",
    "_is_url_echo",
    "_is_usable_browse_content",
    "_jina_read",
    "_looks_like_api_key",
    "_looks_like_autonomy_grant",
    "_looks_like_computer_read_request",
    "_looks_like_proceed_request",
    "_looks_like_raw_markup",
    "_looks_like_runtime_capability_question",
    "_looks_like_standalone_url",
    "_looks_like_tweet_followup_request",
    "_matched_named_actions",
    "_matched_policy_actions",
    "_needs_real_browser",
    "_normalize_command_text",
    "_normalize_url",
    "_parse_autonomy_mode",
    "_parse_float",
    "_parse_non_negative_int",
    "_parse_positive_int",
    "_parse_toggle",
    "_policy_for_mode",
    "_read_env_var_from_shell_files",
    "_record_summary",
    "_run_summary",
    "_select_navigation_strategy",
    "_select_next_task_queue_item",
    "_stable_task_id",
    "_strip_url_punctuation",
    "_summarize_prefetched_link_content",
    "_task_approval_summary",
    "_task_queue_item_ready",
    "_tweet_fxtwitter_read",
    "_tweet_oembed_fallback",
    "_help_response",
]


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
_TWEET_ANALYSIS_SHORTCUT_TOKENS = (
    "revisa",
    "revisalo",
    "review",
    "check",
    "lee",
    "read",
    "analiza",
    "analyze",
)
_LINK_ANALYSIS_SHORTCUT_TOKENS = _TWEET_ANALYSIS_SHORTCUT_TOKENS
_COMPUTER_ACTION_TOKENS = (
    "haz click",
    "haz clic",
    "dale click",
    "dale clic",
    "click on",
    "click en",
    "clic en",
    "scroll down",
    "scroll up",
    "scroll to",
    "desplaza hacia",
    "desplazate hacia",
    "type in",
    "type into",
    "escribe en",
    "press enter",
    "press tab",
    "presiona enter",
    "presiona tab",
    "selecciona el",
    "selecciona la",
    "selecciona un",
    "drag and drop",
    "arrastra el",
    "arrastra la",
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
    r"(?:un\s+)?(?:cuaderno|notebook)(?:\s+(?:en\s+notebooklm))?"
    r"(?:\s+(?:sobre|de(?:l)?))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
_NLM_ARTIFACT_KINDS = {
    "podcast": "podcast",
    "infografia": "infographic",
    "infographic": "infographic",
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
_RUNTIME_CAPABILITY_SECTION_TITLES = (
    "Implementado hoy",
    "Parcial",
    "Sugerencia",
)
_LINK_ANALYSIS_SECTION_TITLES = (
    "Fuente",
    "Aplicación sugerida",
)
_AUTONOMY_MODES = ("manual", "assisted", "autonomous")
_PROCEED_TOKENS = (
    "procede",
    "continue",
    "continua",
    "continuar",
    "sigue",
    "seguir",
    "dale",
    "hazlo",
    "asi",
)
_OPTION_ORDINALS = {
    "primera": 1,
    "first": 1,
    "segunda": 2,
    "second": 2,
    "tercera": 3,
    "third": 3,
    "cuarta": 4,
    "fourth": 4,
    "quinta": 5,
    "fifth": 5,
}
_AUTONOMY_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "deploy": (r"\bdeploy\b", r"\bdespliega\b", r"\bproduction\b", r"\bprod\b"),
    "publish": (r"\bpublica\b", r"\bpublish\b", r"\btweet\b", r"\bpost\b"),
    "destructive": (r"\bdelete\b", r"\bborra\b", r"\belimina\b", r"\brm\s+-", r"\bdrop\s+table\b", r"\btruncate\b"),
}
_AUTONOMY_TASK_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "inspect": (r"\brevisa\b", r"\binspect\b", r"\banaliza\b", r"\bdebug\b", r"\bcheck\b"),
    "edit": (r"\bcorrige\b", r"\barregla\b", r"\bfix\b", r"\bimplementa\b", r"\bedit\b", r"\bpatch\b"),
    "test": (r"\btest\b", r"\bpytest\b", r"\bverifica\b", r"\bverify\b"),
    "commit": (r"\bcommit\b", r"\bcommitea\b"),
    "push": (r"\bgit\s+push\b", r"\bpush\b", r"\bpushea\b", r"\bempuja\b"),
    "research": (r"\binvestiga\b", r"\bresearch\b", r"\bfind\b", r"\bgather\b"),
    "summarize": (r"\bresume\b", r"\bsummariza\b", r"\bsummary\b", r"\bsintetiza\b"),

}
_AUTONOMY_POLICY_MATRIX: dict[str, dict[str, Any]] = {
    "manual": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": ("commit", "push"),
        "allowed_task_actions": (),
        "notes": [
            "No coordinated execution runs automatically in manual mode.",
            "Use this mode when you want explicit confirmation before non-trivial work.",
        ],
    },
    "assisted": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": ("commit", "push"),
        "allowed_task_actions": ("inspect", "edit", "test", "research", "summarize"),
        "notes": [
            "Coordinated runs require explicit `/task_run` in assisted mode.",
            "Commit and git push require confirmation. Publish, deploy, and destructive actions remain blocked.",
        ],
    },
    "autonomous": {
        "automatic_coordinator_modes": ["coding", "research"],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": (),
        "allowed_task_actions": ("inspect", "edit", "test", "commit", "push", "research", "summarize"),
        "notes": [
            "Autonomous coordinator runs are limited to coding and research.",
            "Operational/browser/authenticated flows stay outside the autonomous coordinator path.",
            "Commit and git push are allowed for development tasks after local verification. Publish, deploy, and destructive actions remain blocked.",
        ],
    },
}


def _parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise ValueError("toggle must be one of: on, off")


def _parse_autonomy_mode(value: str) -> str:
    normalized = _normalize_command_text(value).strip()
    if normalized not in _AUTONOMY_MODES:
        raise ValueError("autonomy mode must be one of: manual, assisted, autonomous")
    return normalized


def _default_step_budget(autonomy_mode: str) -> int:
    if autonomy_mode == "manual":
        return 1
    if autonomy_mode == "autonomous":
        return 4
    return 2


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


def _stable_task_id(summary: str, *, mode: str, source: str) -> str:
    normalized = _normalize_command_text(summary)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:48] or "task"
    return f"{mode}:{source}:{slug}"


def _extract_option_reference(text: str) -> int | None:
    normalized = _normalize_command_text(text).strip()
    match = re.fullmatch(r"(?:opcion|option)\s+(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"la\s+(\w+)", normalized)
    if match:
        return _OPTION_ORDINALS.get(match.group(1))
    return _OPTION_ORDINALS.get(normalized)


def _looks_like_proceed_request(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if not normalized:
        return False
    if normalized in _PROCEED_TOKENS:
        return True
    return any(normalized.startswith(f"{token} ") for token in _PROCEED_TOKENS)


def _looks_like_autonomy_grant(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if "autonom" not in normalized and "permiso" not in normalized and "autoriz" not in normalized:
        return False
    grant_markers = (
        "tienes toda la autonomia",
        "tienes autonomia",
        "autonomia completa",
        "modo autonomo",
        "full autonomy",
        "complete autonomy",
        "no tienes que pedirme autorizacion",
        "no me pidas autorizacion",
        "no me pidas permiso",
        "no tienes que pedirme permiso",
        "sin pedir autorizacion",
        "sin pedir permiso",
    )
    return any(marker in normalized for marker in grant_markers)


def _extract_numbered_options(text: str) -> list[str]:
    options: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if not match:
            continue
        options.append(match.group(2).strip())
    return options[:9]


def _compact_summary(text: str, *, limit: int = 240) -> str | None:
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _extract_pending_action_from_reply(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^\s*(?:siguiente paso|next step|pendiente)\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _filter_task_queue_by_mode(task_queue: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    normalized_mode = _normalize_command_text(mode).strip()
    if not normalized_mode:
        return task_queue
    return [item for item in task_queue if _normalize_command_text(str(item.get("mode", ""))) == normalized_mode]


def _select_next_task_queue_item(task_queue: list[dict[str, Any]], *, preferred_mode: str) -> dict[str, Any] | None:
    normalized_mode = _normalize_command_text(preferred_mode).strip()
    preferred = [
        item for item in task_queue
        if item.get("status") == "pending"
        and item.get("summary")
        and _task_queue_item_ready(task_queue, item)
        and _normalize_command_text(str(item.get("mode", ""))) == normalized_mode
    ]
    if preferred:
        return preferred[0]
    for item in task_queue:
        if item.get("status") == "pending" and item.get("summary") and _task_queue_item_ready(task_queue, item):
            return item
    return None


def _task_queue_item_ready(task_queue: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    dependency_ids = [str(dep) for dep in (item.get("depends_on") or []) if dep]
    if not dependency_ids:
        return True
    statuses = {
        str(entry.get("task_id")): str(entry.get("status"))
        for entry in task_queue
        if entry.get("task_id")
    }
    return all(statuses.get(dep_id) == "done" for dep_id in dependency_ids)


def _extract_verification_status(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(
            r"^\s*(?:verificado|verified|verification status)\s*:\s*(ok|passed|pending|failed|none|unknown)\s*$",
            line,
            re.IGNORECASE,
        )
        if match:
            normalized = match.group(1).strip().lower()
            if normalized in {"ok", "passed"}:
                return "passed"
            if normalized == "failed":
                return "failed"
            if normalized == "pending":
                return "pending"
            return "unknown"
    return None


def _build_checkpoint(text: str, *, pending_action: str | None, verification_status: str) -> dict[str, str]:
    checkpoint = {
        "summary": _compact_summary(text, limit=180) or "",
        "verification_status": verification_status,
    }
    if pending_action:
        checkpoint["pending_action"] = pending_action
    return checkpoint


def _build_coordinator_tasks(
    mode: str,
    objective: str,
) -> tuple[list[WorkerTask], list[WorkerTask] | None, list[WorkerTask] | None]:
    if mode == "coding":
        research = [
            WorkerTask(
                name="scope_and_risks",
                lane="research",
                instruction=(
                    "Inspect the request, identify the relevant files, likely risks, and the smallest viable implementation. "
                    f"Objective: {objective}"
                ),
            )
        ]
        implementation = [
            WorkerTask(
                name="implement_change",
                lane="worker",
                instruction=(
                    "Implement the requested change in the workspace. Keep edits minimal and explicit. "
                    "Summarize files touched and what changed. "
                    f"Objective: {objective}"
                ),
            )
        ]
        verification = [
            WorkerTask(
                name="verify_change",
                lane="verifier",
                instruction=(
                    "Verify the implementation from the available evidence. "
                    "Return a concise operational review and include a line `Verification Status: passed|pending|failed`. "
                    "If more work is needed, include `Siguiente paso: ...`. "
                    f"Objective: {objective}"
                ),
            )
        ]
        return research, implementation, verification

    research = [
        WorkerTask(
            name="gather_findings",
            lane="research",
            instruction=(
                "Gather the key facts, claims, and open questions for this request. "
                f"Objective: {objective}"
            ),
        ),
        WorkerTask(
            name="challenge_findings",
            lane="research",
            instruction=(
                "Challenge the assumptions, identify weak claims, and note what still needs verification. "
                f"Objective: {objective}"
            ),
        ),
    ]
    verification = [
        WorkerTask(
            name="verify_findings",
            lane="verifier",
            instruction=(
                "Review the synthesized findings and include `Verification Status: passed|pending|failed`. "
                "If follow-up work is needed, include `Siguiente paso: ...`."
            ),
        )
    ]
    return research, None, verification


def _coordinator_checkpoint(result: CoordinatorResult, *, objective: str) -> dict[str, str]:
    verification_results = result.phase_results.get("verification", [])
    implementation_results = result.phase_results.get("implementation", [])
    verification_text = "\n".join(item.content for item in verification_results if item.content)
    implementation_text = "\n".join(item.content for item in implementation_results if item.content)
    pending_action = _extract_pending_action_from_reply(verification_text) or _extract_pending_action_from_reply(implementation_text)
    verification_status = (
        _extract_verification_status(verification_text)
        or ("failed" if result.error else None)
        or ("pending" if verification_results else "unknown")
    )
    summary = _compact_summary(result.synthesis or objective, limit=180) or objective
    checkpoint = {
        "summary": summary,
        "verification_status": verification_status,
    }
    if pending_action:
        checkpoint["pending_action"] = pending_action
    if result.error:
        checkpoint["error"] = result.error
    return checkpoint


def _format_worker_results(results: list[Any]) -> str:
    lines: list[str] = []
    for item in results:
        if item.error:
            lines.append(f"- {item.task_name}: ERROR {item.error}")
        else:
            lines.append(f"- {item.task_name}: {_compact_summary(item.content, limit=240) or '(no content)'}")
    return "\n".join(lines) if lines else "- none"


def _format_coordinator_response(result: CoordinatorResult, *, checkpoint: dict[str, str], forced: bool) -> str:
    lines = [
        f"Coordinator task: {result.task_id}",
        f"Dispatch: {'manual' if forced else 'autonomous'}",
    ]
    if result.error:
        lines.append(f"Error: {result.error}")
    if result.synthesis:
        lines.extend(["", "Plan:", result.synthesis])
    if "implementation" in result.phase_results:
        lines.extend(["", "Implementation:", _format_worker_results(result.phase_results["implementation"])])
    if "verification" in result.phase_results:
        lines.extend(["", "Verification:", _format_worker_results(result.phase_results["verification"])])
    lines.extend(
        [
            "",
            f"Verification Status: {checkpoint.get('verification_status', 'unknown')}",
        ]
    )
    if checkpoint.get("pending_action"):
        lines.append(f"Siguiente paso: {checkpoint['pending_action']}")
    if checkpoint.get("summary"):
        lines.append(f"Checkpoint: {checkpoint['summary']}")
    return "\n".join(lines)


def _autonomy_policy_payload(state: dict[str, Any]) -> dict[str, Any]:
    autonomy_mode = state.get("autonomy_mode", "assisted")
    policy = _policy_for_mode(autonomy_mode)
    return {
        "autonomy_mode": autonomy_mode,
        "automatic_coordinator_modes": list(policy["automatic_coordinator_modes"]),
        "allowed_task_actions": list(policy["allowed_task_actions"]),
        "approval_required_actions": list(policy["approval_required_actions"]),
        "step_budget": state.get("step_budget"),
        "verification_status": state.get("verification_status"),
        "blocked_actions": list(policy["blocked_actions"]),
        "action_patterns": {name: list(patterns) for name, patterns in _AUTONOMY_ACTION_PATTERNS.items()},
        "task_action_patterns": {name: list(patterns) for name, patterns in _AUTONOMY_TASK_ACTION_PATTERNS.items()},
        "notes": list(policy["notes"]),
    }


def _evaluate_autonomy_policy(
    text: str,
    *,
    mode: str,
    forced: bool,
    autonomy_mode: str | None = None,
    approved_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    normalized = _normalize_command_text(text)
    effective_autonomy_mode = autonomy_mode or ("autonomous" if not forced else "assisted")
    policy = _policy_for_mode(effective_autonomy_mode)
    matched_actions = _matched_policy_actions(normalized, blocked_actions=policy["blocked_actions"])
    if matched_actions:
        labels = ", ".join(matched_actions)
        return {
            "allowed": False,
            "reason": "sensitive_action",
            "summary": f"La tarea incluye acciones sensibles bloqueadas por política: {labels}.",
        }
    approval_actions = _matched_named_actions(
        normalized,
        names=policy["approval_required_actions"],
        pattern_map=_AUTONOMY_TASK_ACTION_PATTERNS,
    )
    approval_actions = [action for action in approval_actions if action not in approved_actions]
    if approval_actions:
        labels = ", ".join(approval_actions)
        return {
            "allowed": False,
            "reason": "approval_required_action",
            "summary": f"La tarea requiere aprobación para estas acciones: {labels}.",
            "matched_approval_actions": approval_actions,
        }
    if mode not in {"coding", "research"}:
        return {
            "allowed": False,
            "reason": "unsupported_mode",
            "summary": f"Modo {mode} fuera del coordinador autónomo.",
        }
    if not forced and mode not in policy["automatic_coordinator_modes"]:
        return {
            "allowed": False,
            "reason": "mode_not_auto_enabled",
            "summary": f"El modo {mode} no está habilitado para ejecución automática en {effective_autonomy_mode}.",
        }
    requested_actions = _classify_task_actions(normalized, mode=mode)
    effective_allowed_actions = set(policy["allowed_task_actions"]) | set(approved_actions)
    disallowed_actions = sorted(set(requested_actions) - effective_allowed_actions)
    if disallowed_actions:
        labels = ", ".join(disallowed_actions)
        return {
            "allowed": False,
            "reason": "action_not_allowed",
            "summary": f"La tarea pide acciones fuera del scope permitido para {effective_autonomy_mode}: {labels}.",
        }
    return {
        "allowed": True,
        "reason": "allowed",
        "summary": "La tarea entra dentro de la política de autonomía.",
    }


def _format_autonomy_policy_block(policy: dict[str, str | bool]) -> str:
    summary = str(policy.get("summary", "autonomy policy blocked this task"))
    reason = str(policy.get("reason", "blocked"))
    return (
        "autonomy policy blocked coordinated execution.\n"
        f"Reason: {reason}\n"
        f"Summary: {summary}\n"
        "Allowed automatic scopes: coding, research.\n"
        "Blocked automatic scopes: publish, deploy, destructive actions."
    )


def _format_task_approval_response(policy: dict[str, Any], pending: Any) -> str:
    summary = str(policy.get("summary", "task approval required"))
    reason = str(policy.get("reason", "approval_required_action"))
    return (
        "autonomy policy requires approval before coordinated execution.\n"
        f"Reason: {reason}\n"
        f"Summary: {summary}\n"
        f"Approve via: `/task_approve {pending.approval_id} {pending.token}`\n"
        f"Abort via: `/task_abort {pending.approval_id}`"
    )


def _task_approval_summary(objective: str, *, approval_actions: tuple[str, ...]) -> str:
    action_text = ", ".join(approval_actions) if approval_actions else "manual approval"
    compact = objective.strip()
    if len(compact) > 120:
        compact = compact[:117] + "..."
    return f"Approve coordinated task ({action_text}): {compact}"


def _policy_for_mode(autonomy_mode: str) -> dict[str, Any]:
    return _AUTONOMY_POLICY_MATRIX.get(autonomy_mode, _AUTONOMY_POLICY_MATRIX["assisted"])


def _matched_policy_actions(normalized_text: str, *, blocked_actions: tuple[str, ...] | list[str]) -> list[str]:
    return _matched_named_actions(normalized_text, names=blocked_actions, pattern_map=_AUTONOMY_ACTION_PATTERNS)


def _matched_named_actions(
    normalized_text: str,
    *,
    names: tuple[str, ...] | list[str],
    pattern_map: dict[str, tuple[str, ...]],
) -> list[str]:
    matches: list[str] = []
    for name in names:
        patterns = pattern_map.get(name, ())
        if any(re.search(pattern, normalized_text, re.IGNORECASE) for pattern in patterns):
            matches.append(name)
    return matches


def _classify_task_actions(normalized_text: str, *, mode: str) -> list[str]:
    matched = _matched_named_actions(
        normalized_text,
        names=tuple(_AUTONOMY_TASK_ACTION_PATTERNS.keys()),
        pattern_map=_AUTONOMY_TASK_ACTION_PATTERNS,
    )
    if matched:
        return matched
    if mode == "coding":
        return ["inspect", "edit", "test"]
    if mode == "research":
        return ["research", "summarize"]
    return []


def _infer_session_mode(user_text: str, reply_text: str | None = None) -> str:
    normalized = _normalize_command_text(f"{user_text}\n{reply_text or ''}")
    if any(token in normalized for token in ("browse", "http://", "https://", "www.")):
        return "browse"
    if any(token in normalized for token in ("terminal", "chrome", "screen", "computer", "click", "scroll", "sesion")):
        return "ops"
    if any(token in normalized for token in (
        "commit",
        "push",
        "git push",
        "test",
        "pytest",
        "fix",
        "corrige",
        "arregla",
        "bug",
        "repo",
        "codigo",
        "code",
        "patch",
        "completa",
        "termina",
        "implementa",
        "coloca",
        "colócala",
        "colocalo",
        "colócalo",
        "sube",
        "ponlo vivo",
        "ponla vivo",
        "produccion",
        "producción",
        "branding",
        "pagina",
        "página",
        "ga4",
    )):
        return "coding"
    if any(token in normalized for token in ("tweet", "post", "publica", "publish", "x.com", "social")):
        return "publish"
    if any(token in normalized for token in ("investiga", "research", "analiza", "notebook", "cuaderno")):
        return "research"
    return "chat"


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
    # Long texts are pasted content, not computer commands.
    if len(text) > 300:
        return False
    normalized = _normalize_command_text(text)
    return any(token in normalized for token in _COMPUTER_ACTION_TOKENS)


def _looks_like_computer_read_request(normalized: str) -> bool:
    if len(normalized) > 300:
        return False
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

_JS_RENDERED_DOMAINS = frozenset({
    "notion.so",
    "airtable.com",
    "figma.com",
    "linear.app",
    "github.com",
    "vercel.app",
})


def _needs_real_browser(url: str) -> bool:
    """Check if URL needs Chrome CDP (auth cookies + JS) instead of headless."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    return any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS)


def _select_navigation_strategy(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return "static"
    if any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS):
        return "authenticated"
    if any(host == d or host.endswith(f".{d}") for d in _JS_RENDERED_DOMAINS):
        return "js_rendered"
    return "static"


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


def _format_tweet_analysis_prompt(original_text: str, enriched_text: str) -> str:
    return (
        f"{original_text}\n\n"
        "[Instrucción de formato]\n"
        "Si respondes sobre este tweet o sobre enlaces relacionados que el tweet incluye, separa SIEMPRE la respuesta en dos secciones exactas:\n"
        "## Fuente\n"
        "- Resume únicamente lo que está en el tweet y, si aplica, en el contenido enlazado que sí fue leído.\n"
        "- Distingue con claridad qué viene del tweet y qué viene del enlace.\n"
        "- No presentes inferencias o recomendaciones como si fueran parte de la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones o ideas prácticas tuyas.\n"
        "- Si no hay una recomendación útil, escribe: Ninguna por ahora.\n\n"
        f"{enriched_text}"
    )


def _format_link_analysis_prompt(original_text: str, url: str, fetched_content: str) -> str:
    return (
        f"{original_text}\n\n"
        "[Instrucción de formato]\n"
        "Si respondes sobre este enlace, separa SIEMPRE la respuesta en dos secciones exactas:\n"
        "## Fuente\n"
        "- Resume únicamente lo que sí fue leído del enlace.\n"
        "- Si el contenido vino incompleto, estaba detrás de login o hubo limitaciones de lectura, dilo explícitamente.\n"
        "- No mezcles inferencias, recomendaciones o juicio propio con la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones, riesgos o siguientes pasos tuyos.\n"
        "- Si no hay una sugerencia útil, escribe: Ninguna por ahora.\n\n"
        f"[URL analizada]: {url}\n\n"
        f"[Contenido del enlace pre-cargado]:\n{fetched_content}"
    )


def _extract_link_analysis_context(text: str) -> dict[str, str] | None:
    url_match = re.search(r"(?m)^\[URL analizada\]:\s*(\S+)\s*$", text)
    content_match = re.search(r"(?ms)^\[Contenido del enlace pre-cargado\]:\n(.*)$", text)
    if url_match is None or content_match is None:
        return None
    return {
        "url": url_match.group(1).strip(),
        "fetched_content": content_match.group(1).strip(),
    }


def _looks_like_runtime_capability_question(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not any(token in normalized for token in ("bot", "claw", "sistema", "runtime")):
        return False
    return any(
        token in normalized
        for token in (
            "que aporta",
            "que aportara",
            "que tiene",
            "que le falta",
            "que puede",
            "que ya tiene",
            "que ganaria",
            "como esta",
            "como funciona hoy",
        )
    )


def _format_runtime_capability_prompt(text: str) -> str:
    return (
        "[Instrucción de rigor sobre el sistema]\n"
        "Si hablas del estado actual del bot/sistema, no sobreafirmes.\n"
        "Estructura SIEMPRE la respuesta con estas tres secciones exactas y en este orden:\n"
        "## Implementado hoy\n"
        "## Parcial\n"
        "## Sugerencia\n"
        "En 'Implementado hoy' incluye solo capacidades verificables hoy.\n"
        "En 'Parcial' incluye capacidades incompletas, limitadas o no plenamente confiables.\n"
        "En 'Sugerencia' incluye inferencias, recomendaciones o próximos pasos.\n"
        "Usa lenguaje conservador cuando no tengas evidencia directa del código o del comportamiento observado.\n"
        "No atribuyas capacidades internas no verificadas.\n\n"
        f"{text}"
    )


def _has_runtime_capability_sections(text: str) -> bool:
    return all(re.search(rf"(?m)^##\s+{re.escape(title)}\s*$", text) for title in _RUNTIME_CAPABILITY_SECTION_TITLES)


def _has_link_analysis_sections(text: str) -> bool:
    return all(re.search(rf"(?m)^##\s+{re.escape(title)}\s*$", text) for title in _LINK_ANALYSIS_SECTION_TITLES)


def _bulletize_runtime_capability_body(text: str) -> str:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
            if not line:
                continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
    if not bullets:
        bullets.append("- La respuesta original no separó hechos verificados de inferencias.")
    return "\n".join(bullets[:8])


def _bulletize_link_analysis_body(text: str) -> str:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
            if not line:
                continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
    return "\n".join(bullets[:6]) if bullets else ""


def _summarize_prefetched_link_content(fetched_content: str) -> str:
    bullets: list[str] = []
    for raw_line in fetched_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if line.startswith("**") and line.endswith("**"):
            line = line.strip("*").strip()
        line = re.sub(r"^#+\s*", "", line).strip()
        if not line:
            continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
        if len(bullets) >= 4:
            break
    if not bullets:
        bullets.append("- El enlace fue leído, pero no se pudo extraer un resumen claro del contenido pre-cargado.")
    return "\n".join(bullets)


def _enforce_runtime_capability_sections(text: str) -> str:
    if _has_runtime_capability_sections(text):
        return text
    partial_body = _bulletize_runtime_capability_body(text)
    return (
        "## Implementado hoy\n"
        "- La respuesta original no identificó con suficiente rigor qué capacidades están verificadas hoy.\n\n"
        "## Parcial\n"
        f"{partial_body}\n\n"
        "## Sugerencia\n"
        "- Reescribe la respuesta separando hechos verificados, límites actuales e inferencias.\n"
        "- Antes de afirmar capacidades internas, apóyate en código, tests o telemetría reciente."
    )


def _is_url_echo(text: str, url: str) -> bool:
    normalized = text.strip().strip("<>[]{}\"'")
    return normalized == url or normalized == f"- {url}"


def _enforce_link_analysis_sections(text: str, *, url: str, fetched_content: str) -> str:
    if _has_link_analysis_sections(text):
        return text
    source_body = _bulletize_link_analysis_body(text)
    if not source_body or _is_url_echo(text, url):
        source_body = _summarize_prefetched_link_content(fetched_content)
    return (
        "## Fuente\n"
        f"{source_body}\n\n"
        "## Aplicación sugerida\n"
        "- Propón una acción, riesgo o siguiente paso usando lo anterior.\n"
        "- Si no aplica nada más, Ninguna por ahora."
    )


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
    if not any(token in normalized_text for token in ("tweet", "tweets", "tuit", "tuits", "post")):
        return False
    # Only reuse the prior tweet when the user clearly points back to it.
    return any(
        token in normalized_text
        for token in (
            "ultimo tweet",
            "ultimo tuit",
            "ultimos tweets",
            "ultimos tuits",
            "tweet anterior",
            "tuit anterior",
            "ese tweet",
            "ese tuit",
            "este tweet",
            "este tuit",
            "tweet de arriba",
            "tuit de arriba",
            "tweet pasado",
            "tuit pasado",
        )
    )


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


def _has_url_query(url: str) -> bool:
    try:
        return bool(urlsplit(url).query)
    except Exception:
        return False


def _is_local_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local")


def _extract_title_from_url(url: str) -> str:
    """Derive a short title from a URL for wiki ingest."""
    parsed = urlsplit(url)
    path = parsed.path.strip("/").split("/")
    if _is_tweet_url(url):
        return f"Tweet {path[-1][:12]}" if path else "Tweet"
    slug = path[-1] if path and path[-1] else parsed.netloc
    return slug.replace("-", " ").replace("_", " ")[:60] or parsed.netloc


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
    from urllib.parse import quote
    try:
        response = httpx.get(
            f"https://r.jina.ai/{quote(url, safe=':/')}",
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


def _help_response(topic: str | None = None) -> str:
    if topic is None:
        return (
            "Comandos principales:\n"
            "/status - salud general del bot\n"
            "/approvals - aprobaciones pendientes\n"
            "/traces [limit] - traces recientes\n"
            "/trace <trace_id> [limit] - replay de una traza\n"
            "/spending - gasto LLM del día por lane/proveedor/modelo\n"
            "/models - listar modelos registrados y su origen/billing\n"
            "/model status - ver modelos efectivos por lane\n"
            "/model set <lane> <provider:model> [effort=...] - override de modelo por sesión\n"
            "/pipeline_status - pipelines activos\n"
            "/agents - estado de agentes\n"
            "/browse <url> - revisar una URL\n"
            "/screen - screenshot actual\n"
            "/computer <instruccion> - operar el escritorio\n"
            "/terminal_list - sesiones PTY abiertas\n"
            "/nlm_create <tema> - crear cuaderno de NotebookLM\n"
            "/nlm_list - listar cuadernos de NotebookLM\n"
            "/autonomy [mode] - ver o ajustar autonomía de la sesión\n"
            "/autonomy_policy - ver política efectiva de autonomía\n"
            "/jobs - listar trabajos autónomos persistidos de la sesión\n"
            "/job_status <task_id> - ver detalle de un trabajo\n"
            "/job_trace <task_id> [limit] - replay de eventos de un trabajo\n"
            "/job_cancel <job_id> - cancelar un job genérico/background\n"
            "/task_resume <task_id> - reanudar un trabajo interrumpido\n"
            "/task_cancel <task_id> - cancelar un trabajo autónomo\n"
            "/task_run <objetivo> - ejecutar ciclo coordinado\n"
            "/task_loop - inspeccionar presupuesto y checkpoint actual\n"
            "/task_queue [mode] - listar siguientes pasos pendientes de la sesión\n"
            "/task_done <task_id> - marcar paso de la cola como completado\n"
            "/task_defer <task_id> - posponer paso de la cola\n"
            "/task_pending - listar aprobaciones/tareas pendientes de la sesión\n"
            "/session_state - inspeccionar estado persistente de la sesión\n"
            "/playbooks - listar playbooks disponibles\n"
            "/playbook <nombre> - ver detalle de un playbook\n"
            "/backtest <instrucción> - correr backtesting con contexto QTS\n"
            "/grill <plan> - entrevista rigurosa sobre un plan o diseño\n"
            "/tdd <feature> - desarrollo TDD red-green-refactor\n"
            "/improve_arch [foco] - review arquitectural del codebase\n"
            "/effort <level> [lane] - ajustar esfuerzo (low/medium/high/xhigh/max)\n"
            "/verify [foco] - verificar trabajo actual (tests + calidad)\n"
            "/focus - toggle focus mode (solo resultados finales)\n"
            "/voice [voz] - responder por audio (alloy/echo/fable/onyx/nova/shimmer)\n"
            "\n"
            "Ayuda por tema:\n"
            "/help approvals\n"
            "/help pipeline\n"
            "/help agents\n"
            "/help traces\n"
            "/help terminal\n"
            "/help browser\n"
            "/help social\n"
            "/help notebooklm\n"
            "/help autonomy\n"
            "/help spending"
        )

    normalized = topic.strip().lower().replace("-", "").replace("_", "")
    if normalized in {"approval", "approvals"}:
        return (
            "Aprobaciones:\n"
            "/approvals - listar pendientes\n"
            "/approval_status <approval_id> - ver estado\n"
            "/approve <approval_id> <token> - aprobar manualmente\n"
            "/task_approve <approval_id> <token> - aprobar tarea coordinada pendiente\n"
            "/task_abort <approval_id> - abortar tarea coordinada pendiente\n"
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
    if normalized in {"trace", "traces", "replay"}:
        return (
            "Trazas:\n"
            "/traces [limit] - listar traces recientes\n"
            "/trace <trace_id> [limit] - ver replay de eventos para una traza"
        )
    if normalized in {"spending", "cost", "costs", "gasto", "costos"}:
        return (
            "Costos:\n"
            "/spending - gasto LLM de hoy desglosado por lane, provider y modelo\n"
            "/tokens - estimación de uso de contexto de la sesión"
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
            "/chrome_download [ext] - esperar descarga CDP\n"
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
    if normalized in {"autonomy", "sessionstate", "state"}:
        return (
            "Autonomía de sesión:\n"
            "/autonomy - ver estado actual\n"
            "/autonomy <manual|assisted|autonomous> - ajustar modo\n"
            "/autonomy_policy - ver límites efectivos del modo actual\n"
            "/jobs - ver trabajos persistidos de la sesión\n"
            "/job_status <task_id> - inspeccionar un trabajo\n"
            "/job_trace <task_id> [limit] - ver eventos asociados al trabajo\n"
            "/job_cancel <job_id> - cancelar un job genérico/background\n"
            "/task_resume <task_id> - reanudar un trabajo interrumpido\n"
            "/task_cancel <task_id> - cancelar un trabajo autónomo\n"
            "/task_run <objetivo> - disparar research/synthesis/implementation/verification\n"
            "/task_loop - ver presupuesto, pasos y checkpoint\n"
            "/task_queue [mode] - ver siguientes pasos persistidos, opcionalmente filtrados por mode\n"
            "/task_done <task_id> - marcar un paso como done\n"
            "/task_defer <task_id> - marcar un paso como deferred\n"
            "/task_pending - ver aprobaciones pendientes en la sesión\n"
            "/session_state - inspeccionar estado persistente\n"
            "El estado guarda objetivo actual, acción pendiente, objeto activo y opciones recientes."
        )
    return (
        f"Tema de ayuda no reconocido: {topic}\n"
        "Prueba con: approvals, pipeline, agents, terminal, browser, social, notebooklm, spending."
    )
