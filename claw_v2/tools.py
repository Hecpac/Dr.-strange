from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from claw_v2.memory import MemoryStore
from claw_v2.action_events import ActionResult, ProposedAction, emit_event
from claw_v2.evidence_ledger import EvidenceRef, record_claim
from claw_v2.goal_contract import create_goal
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.runtime_policy import RuntimePolicyEngine
from claw_v2.sandbox import SandboxPolicy
from claw_v2.sanitizer import extract_structured, sanitize
from claw_v2.tool_policy import validate_workspace_path
from claw_v2.types import AgentClass, SanitizedContent

logger = logging.getLogger(__name__)


def _is_systemic_block(exc: Exception) -> bool:
    """A systemic block (cost/rate breaker) blocks every tool, so pivoting
    to an alternative is wasted retry. Hard-denylist matches and sandbox
    policy rejections are tool-specific and SHOULD pivot."""
    msg = str(exc).lower()
    return "observation window frozen" in msg or "tool_calls_per_minute breaker" in msg

if TYPE_CHECKING:
    from claw_v2.a2a import A2AService
    from claw_v2.skills import SkillRegistry


ToolHandler = Callable[[dict], dict]
_FIRECRAWL_CONTENT_LIMIT = 12_000
_FIRECRAWL_CREDIT_PATTERNS = (
    "insufficient credits",
    "not enough credits",
    "credit balance",
    "payment required",
)
_FIRECRAWL_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "too many requests",
    "429",
)
_IMAGE_MEDIA_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class FirecrawlUnavailableError(RuntimeError):
    """Raised when Firecrawl cannot serve a request for operational reasons."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"Firecrawl unavailable: {reason}: {detail[:300]}")
        self.reason = reason
        self.detail = detail


def classify_firecrawl_error(text: str, *, status_code: int | None = None) -> str | None:
    normalized = (text or "").lower()
    if any(pattern in normalized for pattern in _FIRECRAWL_CREDIT_PATTERNS) or status_code == 402:
        return "insufficient_credits"
    if any(pattern in normalized for pattern in _FIRECRAWL_RATE_LIMIT_PATTERNS) or status_code == 429:
        return "rate_limited"
    return None


def _firecrawl_post_json(endpoint: str, *, api_key: str, payload: dict, timeout: float) -> dict:
    request = Request(
        f"https://api.firecrawl.dev/v1/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        detail = _read_http_error(exc)
        reason = classify_firecrawl_error(detail, status_code=exc.code)
        if reason is not None:
            raise FirecrawlUnavailableError(reason, detail) from exc
        raise
    reason = classify_firecrawl_error(json.dumps(body, default=str))
    if reason is not None:
        raise FirecrawlUnavailableError(reason, json.dumps(body, default=str))
    return body


def _read_http_error(exc: HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _image_media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    media_type = _IMAGE_MEDIA_BY_SUFFIX.get(suffix)
    if media_type is None:
        raise ValueError("image_path must point to a supported image file")
    return media_type


def _looks_like_supported_image(raw: bytes, suffix: str) -> bool:
    normalized = suffix.lower()
    if normalized == ".png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if normalized in {".jpg", ".jpeg"}:
        return raw.startswith(b"\xff\xd8\xff")
    if normalized == ".gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    if normalized == ".webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return False

# Autonomy tiers (per SOUL.md). Enforced in code, not prompt.
#   1 = read-only / local-safe / observation  -> auto-execute, no approval
#   2 = local mutation / run scripts / web read -> auto-execute, logged
#   3 = irreversible / spends money / sends externally -> requires approval
#
# Canonical mapping for default tools (audited 2026-04-24 per HEC-14):
#   Tier 1: Read, Glob, Grep, SearchMemory, WebSearch, WebFetch, WikiSearch,
#           WikiGraph, SkillList, A2ACard, A2APeers, FirecrawlScrape,
#           FirecrawlSearch, FirecrawlExtract
#     note (Bash): Tier 2 assumes the handler stays `external_stub` and the SDK
#       runtime enforces its own shell approval. If Bash ever executes locally,
#       promote to Tier 3.
#     note (Firecrawl): consumes paid credits (~$0.001/call). Rate-limit by
#       credit budget is the circuit breaker. Tier 1 accepted.
#     note (SkillExecute): Tier 3 (fail-safe). A registered skill may invoke
#       sub-tools of any tier; without tier-introspection of the skill body the
#       conservative default is to treat every skill run as approval-gated.
#   Tier 2: Write, Edit, Bash, WikiLint, SkillGenerate, AnalyzeImage
#   Tier 3: WikiDelete, A2ASend, HeyGenVideo, HeyGenDeliver, InstagramPublish, GPTImage, SkillExecute
TIER_READ_ONLY = 1
TIER_LOCAL_MUTATION = 2
TIER_REQUIRES_APPROVAL = 3
DEFAULT_TOOL_TIER = TIER_LOCAL_MUTATION


def tool_requires_approval(tier: int) -> bool:
    """Return True iff a tool of this tier must go through ApprovalManager."""
    return tier >= TIER_REQUIRES_APPROVAL


def _tier_label(tier: int) -> str:
    if tier >= TIER_REQUIRES_APPROVAL:
        return "tier_3"
    if tier >= TIER_LOCAL_MUTATION:
        return "tier_2"
    return "tier_1"


def _risk_level_for_tier(tier: int) -> str:
    if tier >= TIER_REQUIRES_APPROVAL:
        return "critical"
    if tier >= TIER_LOCAL_MUTATION:
        return "medium"
    return "low"


def _result_hash(result: object) -> str:
    try:
        payload = json.dumps(result, sort_keys=True, default=str)
    except TypeError:
        payload = str(result)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


SUPPORTED_AGENT_CLASSES: tuple[AgentClass, ...] = ("researcher", "operator", "deployer")
DEFAULT_TOOL_AGENT_CLASSES: dict[str, tuple[AgentClass, ...]] = {
    "Read": ("researcher", "operator", "deployer"),
    "Write": ("operator", "deployer"),
    "Edit": ("operator", "deployer"),
    "Glob": ("researcher", "operator", "deployer"),
    "Grep": ("researcher", "operator", "deployer"),
    "Bash": ("operator", "deployer"),
    "WebSearch": ("researcher",),
    "WebFetch": ("researcher",),
    "SearchMemory": ("researcher", "operator", "deployer"),
    "WikiSearch": ("researcher", "operator", "deployer"),
    "WikiLint": ("researcher", "operator", "deployer"),
    "WikiDelete": ("operator", "deployer"),
    "WikiGraph": ("researcher", "operator", "deployer"),
    "SkillList": ("researcher", "operator", "deployer"),
    "SkillGenerate": ("operator", "deployer"),
    "SkillExecute": ("operator", "deployer"),
    "A2ACard": ("researcher", "operator", "deployer"),
    "A2APeers": ("researcher", "operator", "deployer"),
    "A2ASend": ("operator", "deployer"),
    "HeyGenVideo": ("operator", "deployer"),
    "HeyGenDeliver": ("operator", "deployer"),
    "InstagramPublish": ("operator", "deployer"),
    "SocialCaptionScaffold": ("researcher", "operator", "deployer"),
    "SocialReplyScaffold": ("researcher", "operator", "deployer"),
    "SocialCompetitorResearch": ("researcher", "operator", "deployer"),
    "BrowserNavigate": ("researcher", "operator", "deployer"),
    "BrowserSnapshot": ("researcher", "operator", "deployer"),
    "BrowserScreenshot": ("researcher", "operator", "deployer"),
    "BrowserClick": ("operator", "deployer"),
    "BrowserType": ("operator", "deployer"),
}


_browser_svc = None
_browser_svc_lock = threading.Lock()


def _browser_tool_service(observe: object | None = None):
    # Process-wide singleton: the @eN ref map lives in BrowserToolService._sessions,
    # so navigate/click/type must share one instance or refs go stale. Also avoids
    # re-running ensure_ready() (which focuses/launches Chrome) on every tool call.
    # Lazy + lock-guarded; never caches on failure so the handler try/except still degrades.
    global _browser_svc
    if _browser_svc is not None:
        return _browser_svc
    with _browser_svc_lock:
        if _browser_svc is None:
            from claw_v2.browser_capability import BrowserCapability
            from claw_v2.browser_tools import build_chrome_cdp_service

            endpoint = BrowserCapability(observe=observe).ensure_ready(visible=True)
            _browser_svc = build_chrome_cdp_service(cdp_endpoint=endpoint, observe=observe)
    return _browser_svc


def _run_off_loop(fn, *a, **kw):
    # Sync callers run inline. Async callers must use ToolRegistry.execute_async,
    # which already delegates sync handlers through asyncio.to_thread.
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*a, **kw)

    raise RuntimeError(
        "browser tool sync handler called from an active event loop; use ToolRegistry.execute_async"
    )


def _browser_navigate(args: dict) -> dict:
    def _work():
        observe = args.get("_observe")
        svc = _browser_tool_service(observe=observe)
        return svc.navigate(
            str(args.get("session_id") or "brain"), str(args["url"]), observe=observe
        )

    try:
        r = _run_off_loop(_work)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {
        "ok": r.success,
        "url": r.url,
        "title": r.title,
        "snapshot": r.snapshot,
        "element_count": r.element_count,
        "error": r.error,
    }


def _browser_snapshot(args: dict) -> dict:
    def _work():
        observe = args.get("_observe")
        svc = _browser_tool_service(observe=observe)
        return svc.snapshot(
            str(args.get("session_id") or "brain"),
            bool(args.get("full", False)),
            observe=observe,
        )

    try:
        r = _run_off_loop(_work)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {
        "ok": r.success,
        "url": r.url,
        "snapshot": r.snapshot,
        "element_count": r.element_count,
        "error": r.error,
    }


def _browser_screenshot_path(raw_path: object | None = None) -> Path:
    import os as _os
    import time as _t

    scratch_raw = _os.getenv("CLAW_BROWSER_SCRATCH_DIR")
    scratch = (
        Path(scratch_raw).expanduser()
        if scratch_raw
        else Path.home() / ".claw" / "scratch" / "browser"
    ).resolve(strict=False)
    raw_name = Path(str(raw_path)).name if raw_path else ""
    if raw_name not in {"", ".", ".."} and Path(raw_name).suffix.lower() != ".png":
        raw_name = Path(raw_name).with_suffix(".png").name
    filename = (
        raw_name
        if raw_name not in {"", ".", ".."}
        else f"browser_shot_{_t.time_ns()}_{secrets.token_hex(4)}.png"
    )
    target = (scratch / filename).resolve(strict=False)
    if not target.is_relative_to(scratch):
        raise PermissionError("screenshot path escaped browser scratch")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _browser_screenshot(args: dict) -> dict:
    def _work():
        observe = args.get("_observe")
        svc = _browser_tool_service(observe=observe)
        path = _browser_screenshot_path(args.get("path"))
        return svc.screenshot(str(args.get("session_id") or "brain"), str(path), observe=observe)

    try:
        r = _run_off_loop(_work)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": r.success, "screenshot_path": r.screenshot_path, "error": r.error}


def _browser_click(args: dict) -> dict:
    def _work():
        observe = args.get("_observe")
        svc = _browser_tool_service(observe=observe)
        return svc.click(str(args.get("session_id") or "brain"), str(args["ref"]), observe=observe)

    try:
        r = _run_off_loop(_work)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": r.success, "url": r.url, "snapshot": r.snapshot, "error": r.error}


def _browser_type(args: dict) -> dict:
    def _work():
        observe = args.get("_observe")
        svc = _browser_tool_service(observe=observe)
        return svc.type(
            str(args.get("session_id") or "brain"),
            str(args["ref"]),
            str(args.get("text", "")),
            observe=observe,
        )

    try:
        r = _run_off_loop(_work)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": r.success, "url": r.url, "snapshot": r.snapshot, "error": r.error}


def is_valid_agent_class(value: str) -> bool:
    return value in SUPPORTED_AGENT_CLASSES


def default_allowed_tools_for(agent_class: AgentClass) -> list[str]:
    if not is_valid_agent_class(agent_class):
        raise ValueError(f"agent_class must be one of: {', '.join(SUPPORTED_AGENT_CLASSES)}")
    return sorted(name for name, classes in DEFAULT_TOOL_AGENT_CLASSES.items() if agent_class in classes)


def _ensure_strict_schema(schema: dict) -> dict:
    """Recursively add additionalProperties=false to all object schemas."""
    if not isinstance(schema, dict):
        return schema
    cleaned = dict(schema)
    if cleaned.get("type") == "object" and "additionalProperties" not in cleaned:
        cleaned["additionalProperties"] = False
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["properties"] = {
            key: _ensure_strict_schema(value) for key, value in properties.items()
        }
    items = cleaned.get("items")
    if isinstance(items, dict):
        cleaned["items"] = _ensure_strict_schema(items)
    return cleaned


_OPENAI_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _openai_tool_name(name: str) -> str:
    """Return an OpenAI-compatible function name for a local tool name."""
    if _OPENAI_TOOL_NAME_RE.fullmatch(name):
        return name
    return "".join(
        char if re.fullmatch(r"[a-zA-Z0-9_-]", char) else f"_x{ord(char):02x}_"
        for char in name
    )


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    allowed_agent_classes: tuple[AgentClass, ...]
    handler: ToolHandler
    mutates_state: bool = False
    requires_network: bool = False
    parameter_schema: dict | None = None
    ingests_external_content: bool = False
    sanitize_fields: tuple[str, ...] = ()
    tier: int = DEFAULT_TOOL_TIER
    # F1 (2026-05-26) — opt-in success contract; warn-only at registration.
    # The actual evaluator lives in claw_v2.verification and is invoked by
    # callers in F3 (task_handler / coordinator_schema). Default None preserves
    # full backward compatibility with all existing tool registrations.
    success_condition: "object | None" = None      # SuccessCondition; quoted to avoid import cycle
    preflight: "object | None" = None              # PreflightSpec
    memory_load_bearing_keys: tuple[str, ...] = ()


_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _strip_code_blocks(text: str) -> str:
    without_fences = _CODE_FENCE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def _collect_strings(value: object) -> list[str]:
    """Recursively extract non-empty strings from nested lists/dicts."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        parts: list[str] = []
        for v in value.values():
            parts.extend(_collect_strings(v))
        return parts
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(_collect_strings(item))
        return parts
    return []


def _extract_sanitizable_text(result: dict, fields: tuple[str, ...]) -> tuple[str, str | None]:
    """Return (text_to_scan, field_used). Checks declared fields first, then falls back to common keys."""
    candidates = list(fields) if fields else ["content", "text", "body", "markdown", "result", "output"]
    for field_name in candidates:
        value = result.get(field_name)
        if value is None:
            continue
        parts = _collect_strings(value)
        if parts:
            return "\n".join(parts), field_name
    return "", None


def sanitize_tool_output(
    definition: "ToolDefinition",
    result: dict,
    *,
    agent_class: AgentClass,
    source_hint: str | None = None,
) -> dict:
    """Scan external-content tool output for prompt-injection patterns.

    Patterns in code fences / backticks are ignored (quoted content is assumed inert).
    Malicious outputs are replaced with a structured quarantine payload so the agent
    can see that something was filtered instead of silently losing the result.
    """
    if not definition.ingests_external_content:
        return result
    text, field_name = _extract_sanitizable_text(result, definition.sanitize_fields)
    if not text:
        return result
    scrubbed = _strip_code_blocks(text)
    source = source_hint or definition.name
    verdict: SanitizedContent = sanitize(scrubbed, source=source, target_agent_class=agent_class)
    if verdict.verdict != "malicious":
        return result
    quarantine = extract_structured(
        text,
        source_url=result.get("url") if isinstance(result.get("url"), str) else None,
        reason=verdict.reason or "suspicious pattern",
    )
    return {
        "sanitized": True,
        "verdict": "malicious",
        "reason": verdict.reason,
        "source": source,
        "field_quarantined": field_name,
        "quarantine": asdict(quarantine),
    }


ApprovalGate = Callable[["ToolDefinition", dict], None]


class ToolRegistry:
    def __init__(
        self,
        *,
        workspace_root: Path | str,
        memory: MemoryStore | None = None,
        observe: object | None = None,
        telemetry_root: Path | str | None = None,
        observation_window: object | None = None,
        autoexec_max_tier: int = TIER_LOCAL_MUTATION,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory = memory
        self.observe = observe
        self.telemetry_root = Path(telemetry_root).expanduser() if telemetry_root is not None else None
        self.observation_window = observation_window
        self.autoexec_max_tier = autoexec_max_tier
        self._runtime_goal_id: str | None = None
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.parameter_schema is not None:
            definition.parameter_schema = _ensure_strict_schema(definition.parameter_schema)
        # F1 warn-only contract check (hard-gate planned for F4).
        # Avoids breaking any existing tool registration while making the
        # contract violation visible in logs / pytest warnings.
        try:
            from claw_v2.verification import warn_if_contract_missing, ToolContractWarning  # local import to avoid cycle
            msg = warn_if_contract_missing(
                tool_name=definition.name,
                tier=int(definition.tier),
                has_sc=definition.success_condition is not None,
                has_pf=definition.preflight is not None,
            )
            if msg:
                import warnings
                warnings.warn(msg, ToolContractWarning, stacklevel=2)
                logger.warning("tool_contract_warning name=%s tier=%s msg=%s", definition.name, definition.tier, msg)
        except Exception:  # pragma: no cover — never block real registration on the contract check
            logger.exception("warn_if_contract_missing failed for tool %s", definition.name)
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        if name not in self._definitions:
            raise KeyError(f"Unknown tool '{name}'.")
        return self._definitions[name]

    def allowed_tools(self, agent_class: AgentClass) -> list[str]:
        return sorted(
            definition.name
            for definition in self._definitions.values()
            if agent_class in definition.allowed_agent_classes
        )

    def openai_tool_schemas(self, agent_class: AgentClass | None = None) -> list[dict]:
        """Export tool definitions as OpenAI function-calling schemas."""
        schemas: list[dict] = []
        seen_names: dict[str, str] = {}
        for defn in self._definitions.values():
            if defn.parameter_schema is None:
                continue
            if agent_class and agent_class not in defn.allowed_agent_classes:
                continue
            openai_name = _openai_tool_name(defn.name)
            if openai_name in seen_names and seen_names[openai_name] != defn.name:
                raise ValueError(
                    f"OpenAI tool name collision: {seen_names[openai_name]} and {defn.name}"
                )
            seen_names[openai_name] = defn.name
            schemas.append({
                "type": "function",
                "name": openai_name,
                "description": defn.description,
                "parameters": defn.parameter_schema,
            })
        return schemas

    def original_tool_name_from_openai(self, name: str) -> str:
        if name in self._definitions:
            return name
        for defn in self._definitions.values():
            if _openai_tool_name(defn.name) == name:
                return defn.name
        return name

    def execute(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
        goal_id: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        definition = self.get(name)
        if agent_class not in definition.allowed_agent_classes:
            raise PermissionError(f"Agent class '{agent_class}' cannot use tool '{name}'.")
        self._observation_before_tool(definition, args, agent_class)
        runtime_decision = None
        if policy is not None:
            runtime_decision = RuntimePolicyEngine(
                workspace_root=policy.workspace_root,
                sandbox_policy=policy,
                network_enforcer=network_enforcer,
                autoexec_max_tier=self.autoexec_max_tier,
            ).enforce_tool(
                definition,
                args,
                context=agent_class,
                approval_gate=approval_gate,
            )
        # Tier enforcement (HEC-14): bypass approval for Tier 1/2, gate Tier 3.
        # Logged to observe stream for audit. approval_gate may raise to block.
        if runtime_decision is not None:
            if runtime_decision.approval_required:
                self._emit_autonomy_event("AUTONOMY_APPROVED", definition, agent_class)
            else:
                self._emit_autonomy_event("AUTONOMY_BYPASS", definition, agent_class)
        elif tool_requires_approval(definition.tier) or definition.tier > self.autoexec_max_tier:
            if approval_gate is None:
                raise PermissionError(
                    f"Tool '{name}' is Tier {definition.tier} (requires approval) "
                    f"but no approval_gate was provided to the dispatcher."
                )
            approval_gate(definition, args)
            self._emit_autonomy_event("AUTONOMY_APPROVED", definition, agent_class)
        else:
            self._emit_autonomy_event("AUTONOMY_BYPASS", definition, agent_class)
        p0_goal_id = goal_id or self._p0_runtime_goal_id()
        p0_session_id = session_id or "runtime"
        proposed_event_id = self._emit_p0_tool_event(
            event_type="action_proposed",
            definition=definition,
            args=args,
            agent_class=agent_class,
            goal_id=p0_goal_id,
            session_id=p0_session_id,
            result=None,
            claims=[],
        )
        # F3a.1+F3a.2 — snapshot pre-state for state_delta_check BEFORE the
        # handler runs. If observation itself fails, we still continue (handler
        # must run) but we record the failure so the artifact-attach step
        # below sees a "no_observation" pre-state and emits the contract marker
        # without an artifact — the gate then blocks fail-closed.
        _pre_state: dict | None = None
        _pre_state_error: str | None = None
        if definition.success_condition is not None:
            try:
                from claw_v2.verification.local_tool_runner import observe_pre_state
                _pre_state = observe_pre_state(definition.name, args)
            except Exception as exc:
                _pre_state_error = f"{type(exc).__name__}: {exc}"[:200]
                logger.exception("observe_pre_state failed for tool %s", definition.name)
                _pre_state = None
        try:
            handler_args = args
            if definition.name.startswith("Browser") and self.observe is not None:
                handler_args = {**args, "_observe": self.observe}
            result = definition.handler(handler_args)
        except Exception as exc:
            self._observation_after_tool(
                definition,
                agent_class,
                status="fail",
                error=f"{type(exc).__name__}: {exc}",
            )
            claim_id = self._record_p0_tool_claim(
                definition=definition,
                goal_id=p0_goal_id,
                status="failure",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._emit_p0_tool_event(
                event_type="action_failed",
                definition=definition,
                args=args,
                agent_class=agent_class,
                goal_id=p0_goal_id,
                session_id=p0_session_id,
                originating_event_id=proposed_event_id,
                result=ActionResult(status="failure", output_hash="", error=f"{type(exc).__name__}: {exc}"),
                claims=[claim_id] if claim_id else [],
            )
            raise
        self._observation_after_tool(definition, agent_class, status="ok")
        claim_id = self._record_p0_tool_claim(
            definition=definition,
            goal_id=p0_goal_id,
            status="success",
        )
        self._emit_p0_tool_event(
            event_type="action_executed",
            definition=definition,
            args=args,
            agent_class=agent_class,
            goal_id=p0_goal_id,
            session_id=p0_session_id,
            originating_event_id=proposed_event_id,
            result=ActionResult(status="success", output_hash=_result_hash(result), error=None),
            claims=[claim_id] if claim_id else [],
        )
        # F3a.1+F3a.2 — attach `_success_condition_artifact` to the result.
        # FAIL-CLOSED: if the tool declared a contract, ALWAYS mark the result
        # with `_contract_required=True` first, BEFORE attach is attempted.
        # Even if attach_artifact_to_result raises, the marker survives and
        # the downstream gate will block silently-succeeded tasks.
        if definition.success_condition is not None and isinstance(result, dict):
            from claw_v2.verification.local_tool_runner import (
                CONTRACT_REQUIRED_KEY,
                attach_artifact_to_result,
            )
            result[CONTRACT_REQUIRED_KEY] = True
            if _pre_state_error is not None:
                # Pre-state observation failed → record cause so gate event is descriptive.
                result["_pre_state_error"] = _pre_state_error
            try:
                result = attach_artifact_to_result(
                    tool_name=definition.name,
                    args=args,
                    result=result,
                    pre_state=_pre_state or {},
                    workspace_root=str(self.workspace_root) if hasattr(self, "workspace_root") else None,
                )
            except Exception as exc:
                # The marker is already on the result; the gate will detect
                # the missing artifact and block. We surface a structured
                # error field instead of silently swallowing.
                logger.exception("attach_artifact_to_result failed for tool %s", definition.name)
                result["_artifact_build_error"] = f"{type(exc).__name__}: {exc}"[:200]
        if definition.ingests_external_content and isinstance(result, dict):
            return sanitize_tool_output(definition, result, agent_class=agent_class)
        return result

    def execute_with_pivot(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
        goal_id: str | None = None,
        session_id: str | None = None,
        alternatives: list[str] | None = None,
    ) -> dict:
        """Execute a tool with automatic pivot to alternatives on tool-specific blocks.

        Pivots when the framework refuses the SPECIFIC tool: PermissionError from
        sandbox, hard denylist matches. Does NOT pivot on systemic blocks
        (observation window frozen, tool_calls_per_minute breaker) — same window
        blocks every alternative, so retrying is wasted.

        Alternatives source: explicit `alternatives` param, else
        `ToolPolicy.fallback_tools` for the primary tool. Same args are passed
        verbatim — incompatibility just falls through the chain.

        Emits `tool_pivot` per pivot with from_tool / to_tool / reason.
        """
        if alternatives is None:
            try:
                from claw_v2.tool_policy import TOOL_POLICIES

                policy_entry = TOOL_POLICIES.get(name)
                alternatives = list(policy_entry.fallback_tools) if policy_entry else []
            except Exception:
                alternatives = []
        chain = [name, *(alternatives or [])]
        last_error: PermissionError | None = None
        for idx, candidate in enumerate(chain):
            try:
                return self.execute(
                    candidate,
                    args,
                    agent_class=agent_class,
                    policy=policy,
                    network_enforcer=network_enforcer,
                    approval_gate=approval_gate,
                    goal_id=goal_id,
                    session_id=session_id,
                )
            except PermissionError as exc:
                last_error = exc
                if _is_systemic_block(exc):
                    raise
                if idx < len(chain) - 1:
                    next_candidate = chain[idx + 1]
                    self._emit_tool_pivot(
                        from_tool=candidate,
                        to_tool=next_candidate,
                        reason=f"{type(exc).__name__}: {str(exc)[:160]}",
                    )
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("execute_with_pivot reached unreachable branch")

    def _emit_tool_pivot(self, *, from_tool: str, to_tool: str, reason: str) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(
                "tool_pivot",
                payload={"from_tool": from_tool, "to_tool": to_tool, "reason": reason[:300]},
            )
        except Exception:
            logger.debug("tool_pivot emit failed", exc_info=True)

    def _p0_runtime_goal_id(self) -> str:
        if self._runtime_goal_id is not None:
            return self._runtime_goal_id
        if self.telemetry_root is None:
            return "g_runtime_tool_dispatch"
        try:
            goal = create_goal(
                self.telemetry_root,
                objective="Observe runtime tool dispatch without changing execution behavior.",
                allowed_actions=sorted(self._definitions.keys()),
                success_criteria=["typed action events are append-only and redacted"],
                risk_profile="tier_1",
                anchor_source="runtime:tool_registry",
                observe=self.observe,
            )
            self._runtime_goal_id = goal.goal_id
        except Exception:
            self._runtime_goal_id = "g_runtime_tool_dispatch"
        return self._runtime_goal_id

    def _record_p0_tool_claim(
        self,
        *,
        definition: "ToolDefinition",
        goal_id: str,
        status: str,
        error: str = "",
    ) -> str | None:
        if self.telemetry_root is None:
            return None
        try:
            claim = record_claim(
                self.telemetry_root,
                goal_id=goal_id,
                claim_text=(
                    f"Tool {definition.name} executed with status {status}."
                    if not error
                    else f"Tool {definition.name} failed with {error[:180]}."
                ),
                claim_type="fact",
                evidence_refs=[EvidenceRef(kind="tool_call", ref=f"tool_registry.execute:{definition.name}:{status}")],
                verification_status="verified",
                confidence=1.0,
                observe=self.observe,
            )
            return claim.claim_id
        except Exception:
            logger.exception("P0 record_claim failed for %s", definition.name)
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "p0_telemetry_failed",
                        payload={"phase": "record_claim", "tool": definition.name},
                    )
                except Exception:
                    logger.debug(
                        "p0_telemetry_failed emit suppressed",
                        exc_info=True,
                    )
            return None

    def _emit_p0_tool_event(
        self,
        *,
        event_type: str,
        definition: "ToolDefinition",
        args: dict,
        agent_class: AgentClass,
        goal_id: str,
        session_id: str,
        result: ActionResult | None,
        claims: list[str],
        originating_event_id: str | None = None,
    ) -> str | None:
        if self.telemetry_root is None:
            return None
        try:
            event = emit_event(
                self.telemetry_root,
                event_type=event_type,  # type: ignore[arg-type]
                actor="claw",
                goal_id=goal_id,
                session_id=session_id,
                originating_event_id=originating_event_id,
                proposed_next_action=ProposedAction(
                    tool=definition.name,
                    args_redacted=args,
                    tier=_tier_label(definition.tier),
                    rationale_brief=f"{agent_class} tool dispatch",
                ),
                risk_level=_risk_level_for_tier(definition.tier),
                claims=claims,
                result=result,
                observe=self.observe,
            )
            return event.event_id
        except Exception:
            logger.exception(
                "P0 emit_event %s failed for %s", event_type, definition.name
            )
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "p0_telemetry_failed",
                        payload={
                            "phase": "emit_event",
                            "event_type": event_type,
                            "tool": definition.name,
                        },
                    )
                except Exception:
                    logger.debug(
                        "p0_telemetry_failed emit suppressed",
                        exc_info=True,
                    )
            return None

    def _emit_autonomy_event(
        self, event: str, definition: "ToolDefinition", agent_class: AgentClass
    ) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(  # type: ignore[attr-defined]
                event,
                lane="tool_dispatcher",
                payload={
                    "tool": definition.name,
                    "tier": definition.tier,
                    "agent_class": agent_class,
                },
            )
        except Exception:
            # Observability is best-effort; never block execution on log failure.
            pass

    def _observation_before_tool(
        self,
        definition: "ToolDefinition",
        args: dict,
        agent_class: AgentClass,
    ) -> None:
        if self.observation_window is None:
            return
        before = getattr(self.observation_window, "before_tool_execution", None)
        if before is None:
            return
        before(
            tool_name=definition.name,
            args=args,
            tier=definition.tier,
            actor=agent_class,
        )

    def _observation_after_tool(
        self,
        definition: "ToolDefinition",
        agent_class: AgentClass,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        if self.observation_window is None:
            return
        after = getattr(self.observation_window, "after_tool_execution", None)
        if after is None:
            return
        after(
            tool_name=definition.name,
            tier=definition.tier,
            actor=agent_class,
            status=status,
            error=error,
        )

    async def execute_async(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
        goal_id: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Async-safe wrapper: offloads blocking handlers to a worker thread.

        Use from async call sites (bot, daemon) so that shell/HTTP/file I/O in
        handlers never blocks the event loop. SQLite (WAL) and sandbox/sanitizer
        helpers are thread-safe; handlers with thread-local state should not be
        marked for async execution.
        """
        import asyncio

        return await asyncio.to_thread(
            self.execute,
            name,
            args,
            agent_class=agent_class,
            policy=policy,
            network_enforcer=network_enforcer,
            approval_gate=approval_gate,
            goal_id=goal_id,
            session_id=session_id,
        )

    @classmethod
    def default(
        cls,
        *,
        workspace_root: Path | str,
        memory: MemoryStore | None = None,
        wiki: object | None = None,
        skill_registry: SkillRegistry | None = None,
        a2a: A2AService | None = None,
        observe: object | None = None,
        telemetry_root: Path | str | None = None,
        observation_window: object | None = None,
        autoexec_max_tier: int = TIER_LOCAL_MUTATION,
    ) -> "ToolRegistry":
        registry = cls(
            workspace_root=workspace_root,
            memory=memory,
            observe=observe,
            telemetry_root=telemetry_root,
            observation_window=observation_window,
            autoexec_max_tier=autoexec_max_tier,
        )
        _ws = Path(workspace_root).resolve()

        def _safe_path(raw: str | Path) -> Path:
            resolved = Path(raw).resolve()
            if not resolved.is_relative_to(_ws):
                raise PermissionError(f"path {raw} is outside workspace root")
            return resolved

        def _readable_path(raw: str | Path) -> Path:
            return validate_workspace_path(raw, workspace_root=_ws)

        def read_file(args: dict) -> dict:
            path = _readable_path(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def read_workspace_nonsecret(args: dict) -> dict:
            path = _readable_path(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def write_file(args: dict) -> dict:
            path = _safe_path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""), encoding="utf-8")
            return {"path": str(path), "written": len(args.get("content", ""))}

        def edit_file(args: dict) -> dict:
            path = _safe_path(args["path"])
            content = path.read_text(encoding="utf-8")
            old_text = args.get("old_text", "")
            new_text = args.get("new_text", "")
            if old_text not in content:
                raise ValueError("old_text not found in file")
            updated = content.replace(old_text, new_text, 1)
            path.write_text(updated, encoding="utf-8")
            return {"path": str(path), "replaced": True}

        def glob_files(args: dict) -> dict:
            root = _safe_path(args.get("root", registry.workspace_root))
            pattern = args.get("pattern", "**/*")
            matches = [str(path) for path in root.glob(pattern)]
            return {"matches": matches[:200]}

        def grep_files(args: dict) -> dict:
            root = _readable_path(args.get("root", registry.workspace_root))
            needle = args.get("query", "")
            matches: list[dict] = []
            candidates = [root] if root.is_file() else root.rglob("*")
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    readable = _readable_path(path)
                except PermissionError:
                    continue
                try:
                    content = readable.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if needle in line:
                        matches.append({"path": str(readable), "line_number": line_number, "line": line})
                        if len(matches) >= 100:
                            return {"matches": matches}
            return {"matches": matches}

        def search_memory(args: dict) -> dict:
            if memory is None:
                raise RuntimeError("memory-backed tool is unavailable")
            safe_search = getattr(memory, "search_prompt_safe_facts", None)
            if callable(safe_search):
                return {
                    "matches": safe_search(
                        args.get("query", ""),
                        limit=int(args.get("limit", 10)),
                    )
                }
            return {"matches": memory.search_facts(args.get("query", ""), limit=int(args.get("limit", 10)))}

        def external_stub(args: dict) -> dict:
            return {
                "status": "delegated_to_provider_runtime",
                "input": args,
            }

        registry.register(
            ToolDefinition(
                name="Read",
                description="Read a file from the workspace.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Read"],
                handler=read_file,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string", "description": "Absolute file path"}}, "required": ["path"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="file.read_workspace_nonsecret",
                description="Read a non-secret file from inside WORKSPACE_ROOT (daemon-safe).",
                allowed_agent_classes=("researcher", "operator", "deployer"),
                handler=read_workspace_nonsecret,
                tier=TIER_READ_ONLY,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path inside WORKSPACE_ROOT; secret paths are rejected.",
                        }
                    },
                    "required": ["path"],
                },
            )
        )
        # F3a (2026-05-26) — local Tier-2 tools declare success conditions.
        from claw_v2.verification.local_tool_contracts import LOCAL_TOOL_SUCCESS_CONDITIONS  # local import to avoid cycle
        # F3b.0 (2026-05-26) — Tier-3 external tools declare contracts + preflight.
        from claw_v2.verification.external_tool_contracts import (
            EXTERNAL_TOOL_PREFLIGHTS,
            EXTERNAL_TOOL_SUCCESS_CONDITIONS,
        )
        registry.register(
            ToolDefinition(
                name="Write",
                description="Write a file in the workspace.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Write"],
                handler=write_file,
                mutates_state=True,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Write"],
            )
        )
        registry.register(
            ToolDefinition(
                name="Edit",
                description="Replace a text span inside a file.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Edit"],
                handler=edit_file,
                mutates_state=True,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
                success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Edit"],
            )
        )
        registry.register(
            ToolDefinition(
                name="Glob",
                description="List files matching a glob pattern.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Glob"],
                handler=glob_files,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"}, "root": {"type": "string", "description": "Root directory (optional)"}}, "required": ["pattern"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Grep",
                description="Search text content across files.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Grep"],
                handler=grep_files,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string", "description": "Text to search for"}, "root": {"type": "string", "description": "Root directory (optional)"}}, "required": ["query"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Bash",
                description="Run an SDK-managed shell command.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Bash"],
                handler=external_stub,
                mutates_state=True,
                tier=TIER_LOCAL_MUTATION,
                success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Bash"],
            )
        )
        registry.register(
            ToolDefinition(
                name="WebSearch",
                description="Search the web through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebSearch"],
                handler=external_stub,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("content", "markdown", "results", "text"),
                tier=TIER_READ_ONLY,
            )
        )
        registry.register(
            ToolDefinition(
                name="WebFetch",
                description="Fetch a single webpage through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebFetch"],
                handler=external_stub,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("content", "markdown", "text", "body"),
                tier=TIER_READ_ONLY,
            )
        )
        registry.register(
            ToolDefinition(
                name="SearchMemory",
                description="Search stored semantic facts.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SearchMemory"],
                handler=search_memory,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
            )
        )

        def wiki_search(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            return {"results": wiki.search(args.get("query", ""), limit=int(args.get("limit", 5)))}  # type: ignore[union-attr]

        def wiki_lint(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            deep = args.get("deep", False)
            if deep:
                return wiki.deep_lint(auto_fix=bool(args.get("auto_fix", False)))  # type: ignore[union-attr]
            return wiki.lint()  # type: ignore[union-attr]

        registry.register(
            ToolDefinition(
                name="WikiSearch",
                description="Semantic search across wiki pages. Args: query (str), limit (int, default 5).",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiSearch"],
                handler=wiki_search,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, "required": ["query"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiLint",
                description="Audit wiki health. Args: deep (bool) for LLM-powered analysis, auto_fix (bool) to auto-deprecate stale pages and create gap stubs.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiLint"],
                handler=wiki_lint,
                tier=TIER_LOCAL_MUTATION,
                parameter_schema={"type": "object", "properties": {"deep": {"type": "boolean", "default": False}, "auto_fix": {"type": "boolean", "default": False}}, "required": []},
                success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["WikiLint"],
            )
        )

        def wiki_delete(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            slug = args.get("slug", "")
            if not slug:
                return {"error": "slug is required"}
            return wiki.delete(slug)

        def wiki_graph(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            slug = args.get("slug", "")
            if slug:
                edges = wiki._graph.get(slug, [])
                neighbors = wiki._graph_neighbors(slug, depth=int(args.get("depth", 1)))
                return {"slug": slug, "edges": edges, "neighbors": neighbors}
            # Full graph summary
            nodes = list(wiki._graph.keys())
            total_edges = sum(len(v) for v in wiki._graph.values())
            return {"nodes": len(nodes), "total_edges": total_edges, "top_nodes": nodes[:20]}

        registry.register(
            ToolDefinition(
                name="WikiDelete",
                description="Cascade-delete a wiki entry. Removes raw source, wiki page, embeddings, graph edges, and index references. Args: slug (str).",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiDelete"],
                handler=wiki_delete,
                mutates_state=True,
                tier=TIER_REQUIRES_APPROVAL,
                success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["WikiDelete"],
                preflight=EXTERNAL_TOOL_PREFLIGHTS["WikiDelete"],
                parameter_schema={"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiGraph",
                description="Query the knowledge graph. Args: slug (str, optional) for a node's edges & neighbors, depth (int, default 1). Without slug returns graph summary.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiGraph"],
                handler=wiki_graph,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"slug": {"type": "string"}, "depth": {"type": "integer", "default": 1}}, "required": []},
            )
        )

        # --- Memento-Skills tools ---
        def skill_list(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            return {"skills": skill_registry.list_skills(), "stats": skill_registry.stats()}

        def skill_generate(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            task = args.get("task", "")
            tags = args.get("tags", [])
            return skill_registry.generate_skill(task_description=task, tags=tags)

        def skill_execute(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            name = args.get("name", "")
            kwargs = args.get("kwargs", {})
            return skill_registry.execute_skill(name, **kwargs)

        registry.register(ToolDefinition(
            name="SkillList", description="List all registered skills and stats.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillList"], handler=skill_list,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="SkillGenerate",
            description="Generate a new skill from description. Args: task (str), tags (list[str], optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillGenerate"],
            handler=skill_generate, mutates_state=True,
            tier=TIER_LOCAL_MUTATION,
            success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["SkillGenerate"],
        ))
        registry.register(ToolDefinition(
            name="SkillExecute",
            description="Execute a registered skill. Args: name (str), kwargs (dict, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillExecute"],
            handler=skill_execute, mutates_state=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["SkillExecute"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["SkillExecute"],
        ))

        # --- A2A Protocol tools ---
        def a2a_card(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return a2a.get_card()

        def a2a_peers(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return {"peers": a2a.list_peers(), "stats": a2a.stats()}

        def a2a_send(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return a2a.send_task(
                to_agent=args.get("to_agent", ""),
                action=args.get("action", ""),
                payload=args.get("payload", {}),
            )

        registry.register(ToolDefinition(
            name="A2ACard", description="Get this agent's A2A identity card.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2ACard"], handler=a2a_card,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="A2APeers", description="List registered A2A peer agents and stats.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2APeers"], handler=a2a_peers,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="A2ASend",
            description="Send a task to an A2A peer. Args: to_agent (str), action (str), payload (dict).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2ASend"],
            handler=a2a_send, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["A2ASend"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["A2ASend"],
        ))

        # --- HeyGen Video tool ---
        def _heygen_api_key() -> str:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "heygen", "-s", "HEYGEN_API_KEY", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            key = result.stdout.strip()
            if not key:
                raise RuntimeError("HEYGEN_API_KEY not found in Keychain")
            return key

        def heygen_video(args: dict) -> dict:
            text = args.get("text", "")
            if not text:
                raise ValueError("text is required")
            avatar_id = args.get("avatar_id", "284630e731f04f49ae7ba9f5d839e6bb")
            voice_id = args.get("voice_id", "398936ac428244c6966feefe6d151c6a")
            title = args.get("title", "Claw Briefing")

            api_key = _heygen_api_key()
            payload = json.dumps({
                "video_inputs": [{
                    "character": {"type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal"},
                    "voice": {"type": "text", "input_text": text, "voice_id": voice_id},
                }],
                "title": title,
                "dimension": {"width": 1280, "height": 720},
            }).encode()
            req = Request(
                "https://api.heygen.com/v2/video/generate",
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Api-Key": api_key,
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            return {"video_id": body.get("data", {}).get("video_id"), "status": body.get("data", {}).get("status")}

        registry.register(ToolDefinition(
            name="HeyGenVideo",
            description="Generate a video with a talking avatar. Args: text (str, required), avatar_id (str, optional), voice_id (str, optional), title (str, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["HeyGenVideo"],
            handler=heygen_video, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["HeyGenVideo"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["HeyGenVideo"],
            parameter_schema={"type": "object", "properties": {"text": {"type": "string"}, "avatar_id": {"type": "string"}, "voice_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["text"]},
        ))

        # --- HeyGen Deliver tool: poll → download → compress → send to Telegram ---
        def heygen_deliver(args: dict) -> dict:
            from claw_v2.heygen_delivery import HeygenDeliveryService

            mode = str(args.get("mode") or "delivery").strip()
            if mode == "read_only_live":
                from claw_v2.heygen_readonly import HeyGenReadOnlyAdapter

                endpoint = str(args.get("endpoint") or "quota").strip()
                params: dict[str, object] = {}
                if endpoint in {"video_status", "status", "/v1/video_status.get"}:
                    params["video_id"] = str(args.get("video_id") or "").strip()
                elif endpoint in {"video_list", "list", "/v1/video.list"}:
                    params["limit"] = int(args.get("limit") or 5)
                    params["offset"] = 0
                adapter = HeyGenReadOnlyAdapter(
                    workspace_root=registry.workspace_root,
                    observe=registry.observe,
                    allow_legacy_v1=bool(args.get("allow_legacy_v1")),
                )
                return adapter.read_only_call(endpoint, params).to_dict()

            video_id = (args.get("video_id") or "").strip()
            latest = bool(args.get("latest"))
            if not video_id and not latest:
                raise ValueError("Provide video_id or set latest=true")

            if latest:
                api_key = _heygen_api_key()
                req = Request(
                    "https://api.heygen.com/v1/video.list?limit=1",
                    headers={"X-Api-Key": api_key},
                )
                with urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read())
                videos = (payload.get("data") or {}).get("videos") or []
                if not videos:
                    raise RuntimeError("no_videos_in_account")
                video_id = videos[0]["video_id"]

            svc = HeygenDeliveryService()
            result = svc.auto_deliver(
                video_id=video_id,
                caption=args.get("caption"),
                chat_id=args.get("chat_id"),
                slug=args.get("slug"),
            )
            return result.to_dict()

        registry.register(ToolDefinition(
            name="HeyGenDeliver",
            description=(
                "Poll a HeyGen render until complete, download, transcode for "
                "Telegram's 50MB Bot API cap, and deliver via sendVideo. "
                "Args: video_id (str, optional if latest=true), latest (bool, "
                "optional - picks most recent), caption (str, optional), "
                "chat_id (str, optional - defaults to TELEGRAM_ALLOWED_USER_ID), "
                "slug (str, optional - filename slug). For F3b.2, "
                "mode=read_only_live performs gated HeyGen status inspection only. "
                "Legacy v1 endpoints require allow_legacy_v1=true."
            ),
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["HeyGenDeliver"],
            handler=heygen_deliver, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["HeyGenDeliver"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["HeyGenDeliver"],
            parameter_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["delivery", "read_only_live"]},
                    "endpoint": {
                        "type": "string",
                        "enum": ["quota", "video_status", "video_list"],
                    },
                    "video_id": {"type": "string"},
                    "latest": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "allow_legacy_v1": {"type": "boolean"},
                    "caption": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "slug": {"type": "string"},
                },
            },
        ))

        def instagram_publish(args: dict) -> dict:
            from claw_v2.instagram_publish import InstagramPublishService

            media_type = (args.get("media_type") or "").strip().lower()
            photo_path = (args.get("photo_path") or "").strip()
            video_path = (args.get("video_path") or "").strip()
            media_path = (args.get("media_path") or "").strip()
            caption = args.get("caption") or ""
            if not media_type:
                media_type = "photo" if photo_path else "reel"
            if media_type not in {"reel", "photo"}:
                raise ValueError("media_type must be 'reel' or 'photo'")
            svc = InstagramPublishService()
            if media_type == "photo":
                target = photo_path or media_path
                if not target:
                    raise ValueError("photo_path or media_path is required for media_type=photo")
                result = svc.publish_photo(
                    photo_path=target,
                    caption=caption,
                    expected_account=args.get("account"),
                )
            else:
                target = video_path or media_path
                if not target:
                    raise ValueError("video_path or media_path is required for media_type=reel")
                result = svc.publish_reel(
                    video_path=target,
                    caption=caption,
                    expected_account=args.get("account"),
                )
            return result.to_dict()

        registry.register(ToolDefinition(
            name="InstagramPublish",
            description=(
                "Publish a local video Reel or photo post via the logged-in "
                "Instagram CDP Chrome session: create flow, upload, caption, "
                "share, then verify via Instagram's share confirmation. "
                "Photo posts also verify profile/top-post change. "
                "Args: media_type (reel|photo), video_path/photo_path/media_path, "
                "caption (str, optional), account (str, optional - expected handle, guards against "
                "posting from the wrong account). Tier 3: external publication."
            ),
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["InstagramPublish"],
            handler=instagram_publish, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["InstagramPublish"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["InstagramPublish"],
            parameter_schema={
                "type": "object",
                "properties": {
                    "video_path": {"type": "string"},
                    "photo_path": {"type": "string"},
                    "media_path": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["reel", "photo"]},
                    "caption": {"type": "string"},
                    "account": {"type": "string"},
                },
                "required": [],
            },
        ))

        # --- Social media skills (scaffolds + competitor research) ---
        def social_caption_scaffold(args: dict) -> dict:
            from claw_v2.social_media import draft_caption_scaffold
            return draft_caption_scaffold(
                topic=args.get("topic", ""),
                platform=args.get("platform", "instagram_reel"),
                voice=args.get("voice", "punchy_contrarian"),
                hook_style=args.get("hook_style", "contrarian"),
            ).to_dict()

        def social_reply_scaffold(args: dict) -> dict:
            from claw_v2.social_media import suggest_reply_scaffold
            return suggest_reply_scaffold(
                incoming_comment=args.get("incoming_comment", ""),
                platform=args.get("platform", "instagram_feed"),
                tone=args.get("tone", "warm"),
            ).to_dict()

        def social_competitor_research(args: dict) -> dict:
            from claw_v2.social_media import research_competitor
            kwargs: dict = {
                "handle": args.get("handle", ""),
                "recent_post_count": int(args.get("recent_post_count", 6)),
            }
            cdp_url = args.get("cdp_url")
            if cdp_url:
                kwargs["cdp_url"] = cdp_url
            return research_competitor(**kwargs).to_dict()

        registry.register(ToolDefinition(
            name="SocialCaptionScaffold",
            description="Return platform-aware scaffold for caption drafting: char limits, hashtag caps, hook patterns, structure. The model writes the actual copy. Args: topic (str), platform (instagram_feed|instagram_reel|instagram_story|linkedin|x|threads), voice (punchy_contrarian|warm_authority|story_driven), hook_style (contrarian|specific_number|concrete_story|question_loop).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SocialCaptionScaffold"],
            handler=social_caption_scaffold, mutates_state=False, requires_network=False,
            tier=TIER_READ_ONLY,
            parameter_schema={"type": "object", "properties": {"topic": {"type": "string"}, "platform": {"type": "string"}, "voice": {"type": "string"}, "hook_style": {"type": "string"}}, "required": ["topic"]},
        ))
        registry.register(ToolDefinition(
            name="SocialReplyScaffold",
            description="Return tone + length + structure guidance for replying to a comment. Does not publish. Args: incoming_comment (str), platform (str), tone (warm|expert|playful|direct).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SocialReplyScaffold"],
            handler=social_reply_scaffold, mutates_state=False, requires_network=False,
            tier=TIER_READ_ONLY,
            parameter_schema={"type": "object", "properties": {"incoming_comment": {"type": "string"}, "platform": {"type": "string"}, "tone": {"type": "string"}}, "required": ["incoming_comment"]},
        ))
        registry.register(ToolDefinition(
            name="SocialCompetitorResearch",
            description="Scrape a public Instagram profile via Chrome CDP: header stats + recent post captions + hook-pattern classification. Read-only. Args: handle (str), recent_post_count (int, default 6), cdp_url (str, optional — override default http://localhost:9250).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SocialCompetitorResearch"],
            handler=social_competitor_research, mutates_state=False, requires_network=True,
            tier=TIER_READ_ONLY,
            parameter_schema={"type": "object", "properties": {"handle": {"type": "string"}, "recent_post_count": {"type": "integer"}, "cdp_url": {"type": "string"}}, "required": ["handle"]},
        ))

        # --- GPT Image generation tool ---
        def _openai_api_key() -> str:
            import os as _os
            key = _os.getenv("OPENAI_API_KEY", "")
            if not key:
                result = subprocess.run(
                    ["security", "find-generic-password", "-a", "openai", "-s", "OPENAI_API_KEY", "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                key = result.stdout.strip()
            if not key:
                raise RuntimeError("OPENAI_API_KEY not found")
            return key

        def gpt_image(args: dict) -> dict:
            prompt_text = args.get("prompt", "")
            if not prompt_text:
                raise ValueError("prompt is required")
            size = args.get("size", "1024x1024")
            quality = args.get("quality", "auto")
            api_key = _openai_api_key()
            payload = json.dumps({
                "model": "gpt-image-1",
                "prompt": prompt_text,
                "size": size,
                "quality": quality,
            }).encode()
            req = Request(
                "https://api.openai.com/v1/images/generations",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
            images = body.get("data", [])
            # Save base64 images to files if present
            saved: list[str] = []
            output_dir = registry.workspace_root / "generated_images"
            output_dir.mkdir(exist_ok=True)
            import base64
            import time as _time
            for i, img in enumerate(images):
                if img.get("b64_json"):
                    fname = f"gpt_image_{int(_time.time())}_{i}.png"
                    fpath = output_dir / fname
                    fpath.write_bytes(base64.b64decode(img["b64_json"]))
                    saved.append(str(fpath))
                elif img.get("url"):
                    saved.append(img["url"])
            return {"images": saved, "revised_prompt": images[0].get("revised_prompt", "") if images else ""}

        DEFAULT_TOOL_AGENT_CLASSES["GPTImage"] = ("operator", "deployer")
        registry.register(ToolDefinition(
            name="GPTImage",
            description="Generate images using GPT Image API. Args: prompt (str, required), size (str, default '1024x1024'), quality (str, default 'auto').",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["GPTImage"],
            handler=gpt_image, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            success_condition=EXTERNAL_TOOL_SUCCESS_CONDITIONS["GPTImage"],
            preflight=EXTERNAL_TOOL_PREFLIGHTS["GPTImage"],
            parameter_schema={"type": "object", "properties": {"prompt": {"type": "string", "description": "Image description"}, "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536"], "default": "1024x1024"}, "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"], "default": "auto"}}, "required": ["prompt"]},
        ))

        # --- GPT Vision / Image Analysis tool ---
        def analyze_image(args: dict) -> dict:
            image_path = args.get("image_path", "")
            image_url = args.get("image_url", "")
            question = args.get("question", "Describe this image in detail.")
            if not image_path and not image_url:
                raise ValueError("image_path or image_url is required")
            content: list[dict] = [{"type": "input_text", "text": question}]
            if image_path:
                import base64 as _b64
                p = _readable_path(image_path)
                if not p.exists():
                    raise ValueError(f"Image file not found: {image_path}")
                media = _image_media_type_for_path(p)
                raw_image = p.read_bytes()
                if not _looks_like_supported_image(raw_image, p.suffix.lower()):
                    raise ValueError("image_path content does not match a supported image format")
                data = _b64.b64encode(raw_image).decode()
                content.append({"type": "input_image", "image_url": f"data:{media};base64,{data}"})
            else:
                content.append({"type": "input_image", "image_url": image_url})
            api_key = _openai_api_key()
            payload = json.dumps({
                "model": "gpt-5.4-mini",
                "input": [{"role": "user", "content": content}],
            }).encode()
            req = Request(
                "https://api.openai.com/v1/responses",
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            return {"analysis": body.get("output_text", ""), "model": "gpt-5.4-mini"}

        DEFAULT_TOOL_AGENT_CLASSES["AnalyzeImage"] = ("researcher", "operator", "deployer")
        registry.register(ToolDefinition(
            name="AnalyzeImage",
            description="Analyze an image using GPT vision. Args: image_path (str) or image_url (str), question (str, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["AnalyzeImage"],
            handler=analyze_image, requires_network=True,
            tier=TIER_LOCAL_MUTATION,
            success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["AnalyzeImage"],
            parameter_schema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Local file path to the image"},
                    "image_url": {"type": "string", "description": "URL of the image to analyze"},
                    "question": {"type": "string", "description": "What to analyze (default: describe the image)", "default": "Describe this image in detail."},
                },
                "required": [],
            },
        ))

        # --- Firecrawl Scrape tool ---
        def _firecrawl_api_key() -> str:
            import os as _os
            key = _os.getenv("FIRECRAWL_API_KEY", "")
            if not key:
                result = subprocess.run(
                    ["security", "find-generic-password", "-a", "firecrawl", "-s", "FIRECRAWL_API_KEY", "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                key = result.stdout.strip()
            if not key:
                raise RuntimeError("FIRECRAWL_API_KEY not found in env or Keychain")
            return key

        def firecrawl_scrape(args: dict) -> dict:
            url = args.get("url", "")
            if not url:
                raise ValueError("url is required")
            formats = args.get("formats", ["markdown"])
            api_key = _firecrawl_api_key()
            body = _firecrawl_post_json(
                "scrape",
                api_key=api_key,
                payload={"url": url, "formats": formats},
                timeout=60,
            )
            data = body.get("data", {})
            return {
                "markdown": data.get("markdown", "")[:_FIRECRAWL_CONTENT_LIMIT],
                "metadata": data.get("metadata", {}),
                "url": data.get("metadata", {}).get("sourceURL", url),
            }

        def firecrawl_search(args: dict) -> dict:
            query = args.get("query", "")
            if not query:
                raise ValueError("query is required")
            limit = int(args.get("limit", 5))
            api_key = _firecrawl_api_key()
            body = _firecrawl_post_json(
                "search",
                api_key=api_key,
                payload={
                    "query": query,
                    "limit": limit,
                    "scrapeOptions": {"formats": ["markdown"]},
                },
                timeout=60,
            )
            results = []
            for item in body.get("data", [])[:limit]:
                results.append({
                    "title": item.get("metadata", {}).get("title", ""),
                    "url": item.get("metadata", {}).get("sourceURL", ""),
                    "markdown": item.get("markdown", "")[:2000],
                })
            return {"results": results, "count": len(results)}

        def firecrawl_extract(args: dict) -> dict:
            url = args.get("url", "")
            if not url:
                raise ValueError("url is required")
            schema = args.get("schema", {})
            if not schema:
                raise ValueError("schema is required")
            prompt = args.get("prompt", "")
            api_key = _firecrawl_api_key()
            body: dict = {"urls": [url], "schema": schema}
            if prompt:
                body["prompt"] = prompt
            result = _firecrawl_post_json(
                "extract",
                api_key=api_key,
                payload=body,
                timeout=90,
            )
            data = result.get("data", [])
            return {"extracted": data[0] if len(data) == 1 else data, "success": result.get("success", False)}

        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlExtract"] = ("researcher", "operator", "deployer")
        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlScrape"] = ("researcher", "operator", "deployer")
        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlSearch"] = ("researcher", "operator", "deployer")
        registry.register(ToolDefinition(
            name="FirecrawlScrape",
            description="Scrape a URL and return markdown content. Works with JS-rendered pages, SPAs, social media. Args: url (str, required), formats (list[str], default ['markdown']).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlScrape"],
            handler=firecrawl_scrape, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("markdown", "content", "html", "text"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to scrape"},
                    "formats": {"type": "array", "items": {"type": "string"}, "default": ["markdown"]},
                },
                "required": ["url"],
            },
        ))
        registry.register(ToolDefinition(
            name="FirecrawlSearch",
            description="Search the web and return scraped results with markdown content. Args: query (str, required), limit (int, default 5).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlSearch"],
            handler=firecrawl_search, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("markdown", "content", "results"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 5, "description": "Max results"},
                },
                "required": ["query"],
            },
        ))
        registry.register(ToolDefinition(
            name="FirecrawlExtract",
            description="Extract structured data from a URL using a JSON schema. Args: url (str, required), schema (dict, required), prompt (str, optional guidance).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlExtract"],
            handler=firecrawl_extract, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("data", "extracted", "markdown", "content"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to extract data from"},
                    "schema": {"type": "object", "description": "JSON schema for the data to extract"},
                    "prompt": {"type": "string", "description": "Optional prompt to guide extraction"},
                },
                "required": ["url", "schema"],
            },
        ))

        registry.register(
            ToolDefinition(
                name="BrowserNavigate",
                description="Navigate to a URL in the managed Chrome browser and return the page snapshot with @eN element refs.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["BrowserNavigate"],
                handler=_browser_navigate,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("snapshot",),
                tier=TIER_READ_ONLY,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to navigate to"},
                        "session_id": {
                            "type": "string",
                            "description": "Browser session id (default: brain)",
                        },
                    },
                    "required": ["url"],
                },
            )
        )
        registry.register(
            ToolDefinition(
                name="BrowserSnapshot",
                description="Read the current page snapshot (DOM, text, @eN refs) without navigating.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["BrowserSnapshot"],
                handler=_browser_snapshot,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("snapshot",),
                tier=TIER_READ_ONLY,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session id (default: brain)",
                        },
                        "full": {
                            "type": "boolean",
                            "description": "Include full page text (default: false)",
                        },
                    },
                    "required": [],
                },
            )
        )
        registry.register(
            ToolDefinition(
                name="BrowserScreenshot",
                description="Capture a PNG screenshot of the current browser viewport.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["BrowserScreenshot"],
                handler=_browser_screenshot,
                requires_network=True,
                ingests_external_content=False,
                mutates_state=True,
                tier=TIER_LOCAL_MUTATION,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Output file name/path (confined to browser scratch)",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Browser session id (default: brain)",
                        },
                    },
                    "required": [],
                },
            )
        )
        registry.register(
            ToolDefinition(
                name="BrowserClick",
                description="Click an element in the browser by @eN ref. Returns updated snapshot.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["BrowserClick"],
                handler=_browser_click,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("snapshot",),
                mutates_state=True,
                tier=TIER_REQUIRES_APPROVAL,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref (e.g. @e1)"},
                        "session_id": {
                            "type": "string",
                            "description": "Browser session id (default: brain)",
                        },
                    },
                    "required": ["ref"],
                },
            )
        )
        registry.register(
            ToolDefinition(
                name="BrowserType",
                description="Type text into an input element in the browser by @eN ref.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["BrowserType"],
                handler=_browser_type,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("snapshot",),
                mutates_state=True,
                tier=TIER_REQUIRES_APPROVAL,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref (e.g. @e1)"},
                        "text": {"type": "string", "description": "Text to type"},
                        "session_id": {
                            "type": "string",
                            "description": "Browser session id (default: brain)",
                        },
                    },
                    "required": ["ref"],
                },
            )
        )

        return registry
