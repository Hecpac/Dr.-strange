from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from claw_v2.redaction import redact_sensitive


_URL_PARAM_KEY_FRAGMENTS = ("url", "uri", "href", "src", "origin", "endpoint")

HIGH_RISK_BROWSER_ACTIONS = frozenset(
    {
        "evaluate",
        "save_as_pdf",
        "upload_file",
        "write_file",
        "replace_file",
        "read_file",
        "read_long_content",
        "extract",
    }
)

_LOW_RISK_BROWSER_ACTIONS = frozenset(
    {
        "done",
        "wait",
        "screenshot",
        "scroll",
        "find_text",
        "search_page",
        "find_elements",
        "dropdown_options",
        "navigate",
        "goto",
        "search",
        "go_back",
        "switch",
        "close",
    }
)

_MEDIUM_RISK_BROWSER_ACTIONS = frozenset(
    {
        "click",
        "input",
        "send_keys",
        "select_dropdown",
    }
)


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    action_name: str
    risk: str
    data_access_class: str
    side_effect_class: str
    approval_requirement: str


BROWSER_ACTION_DEFINITIONS: dict[str, ActionDefinition] = {
    **{
        action: ActionDefinition(
            action_name=action,
            risk="low",
            data_access_class="page",
            side_effect_class="read_or_navigation",
            approval_requirement="none",
        )
        for action in _LOW_RISK_BROWSER_ACTIONS
    },
    **{
        action: ActionDefinition(
            action_name=action,
            risk="medium",
            data_access_class="page",
            side_effect_class="page_interaction",
            approval_requirement="none",
        )
        for action in _MEDIUM_RISK_BROWSER_ACTIONS
    },
    **{
        action: ActionDefinition(
            action_name=action,
            risk="high",
            data_access_class="page_or_local_file",
            side_effect_class="script_or_file",
            approval_requirement="human_exact_scope",
        )
        for action in HIGH_RISK_BROWSER_ACTIONS
    },
}


@dataclass(frozen=True, slots=True)
class ActionContext:
    action_name: str
    params: dict[str, Any]
    current_url: str | None
    target_url: str | None
    task_id: str
    browser_context_id: str
    auto_approved: bool = False

    @property
    def params_hash(self) -> str:
        return canonical_params_hash(self.params)

    @property
    def current_origin(self) -> str:
        return normalize_origin(self.current_url)

    @property
    def target_origin(self) -> str:
        return normalize_origin(self.target_url)


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    action_name: str
    params_hash: str
    current_origin: str
    target_origin: str
    task_id: str
    browser_context_id: str
    expires_at: float
    nonce: str
    approved_by: str

    @classmethod
    def from_value(cls, value: Any) -> "ApprovalScope | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                action_name=str(value["action_name"]),
                params_hash=str(value["params_hash"]),
                current_origin=str(value["current_origin"]),
                target_origin=str(value["target_origin"]),
                task_id=str(value["task_id"]),
                browser_context_id=str(value["browser_context_id"]),
                expires_at=float(value["expires_at"]),
                nonce=str(value["nonce"]),
                approved_by=str(value["approved_by"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason_code: str
    action_name: str
    risk: str = "unknown"
    data_access_class: str = "unknown"
    side_effect_class: str = "unknown"
    approval_requirement: str = "unknown"
    params_hash: str = ""
    current_origin: str = ""
    target_origin: str = ""
    task_id: str = ""
    browser_context_id: str = ""
    approval_scope_present: bool = False
    approval_scope_match: bool = False

    @property
    def decision(self) -> str:
        return "allow" if self.allowed else "deny"

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "action_name": self.action_name,
            "risk": self.risk,
            "current_origin": self.current_origin,
            "target_origin": self.target_origin,
            "task_id": self.task_id,
            "browser_context_id": self.browser_context_id,
            "approval_scope_present": self.approval_scope_present,
            "approval_scope_match": self.approval_scope_match,
            "params_hash": self.params_hash,
        }


class ActionPolicyEngine:
    def __init__(
        self,
        *,
        actions: dict[str, ActionDefinition] | None = None,
    ) -> None:
        self._actions = actions if actions is not None else BROWSER_ACTION_DEFINITIONS

    def evaluate(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        current_url: str | None,
        target_url: str | None,
        task_id: str,
        browser_context_id: str,
        approval: Any | None = None,
        auto_approved: bool = False,
        now: float | None = None,
    ) -> PolicyDecision:
        normalized_action = _normalize_action_name(action_name)
        context = ActionContext(
            action_name=normalized_action,
            params=dict(params or {}),
            current_url=current_url,
            target_url=target_url,
            task_id=str(task_id),
            browser_context_id=str(browser_context_id),
            auto_approved=bool(auto_approved),
        )
        definition = self._actions.get(normalized_action)
        if definition is None:
            return _decision(
                allowed=False,
                reason_code="unknown_action",
                context=context,
            )
        if definition.risk != "high":
            return _decision(
                allowed=True,
                reason_code="allowed",
                context=context,
                definition=definition,
                approval_scope_present=approval is not None,
            )
        scope = ApprovalScope.from_value(approval)
        if scope is None:
            return _decision(
                allowed=False,
                reason_code="approval_required",
                context=context,
                definition=definition,
                approval_scope_present=approval is not None,
            )
        reason_code = _approval_scope_mismatch_reason(
            scope=scope,
            context=context,
            now=time.time() if now is None else float(now),
        )
        if reason_code is not None:
            return _decision(
                allowed=False,
                reason_code=reason_code,
                context=context,
                definition=definition,
                approval_scope_present=True,
            )
        return _decision(
            allowed=True,
            reason_code="approved",
            context=context,
            definition=definition,
            approval_scope_present=True,
            approval_scope_match=True,
        )

    def decide(self, **kwargs: Any) -> PolicyDecision:
        return self.evaluate(**kwargs)


def make_approval_scope(
    *,
    action_name: str,
    params: dict[str, Any],
    current_url: str | None,
    target_url: str | None,
    task_id: str,
    browser_context_id: str,
    approved_by: str,
    ttl_seconds: float = 900.0,
    nonce: str | None = None,
    now: float | None = None,
) -> ApprovalScope:
    now_ts = time.time() if now is None else float(now)
    return ApprovalScope(
        action_name=_normalize_action_name(action_name),
        params_hash=canonical_params_hash(params),
        current_origin=normalize_origin(current_url),
        target_origin=normalize_origin(target_url),
        task_id=str(task_id),
        browser_context_id=str(browser_context_id),
        expires_at=now_ts + max(0.0, float(ttl_seconds)),
        nonce=nonce or secrets.token_urlsafe(12),
        approved_by=str(approved_by),
    )


def canonical_params_hash(params: dict[str, Any] | None) -> str:
    redacted = redact_sensitive(_normalize_for_json(params or {}), limit=0)
    canonical = json.dumps(
        redacted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_origin(url: str | None) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme:
        parsed = urlsplit(f"https://{text}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if not scheme or not host:
        return ""
    try:
        ascii_host = host.strip(".").lower().encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    try:
        explicit_port = parsed.port
    except ValueError:
        return ""
    port = explicit_port or _effective_port(scheme)
    if port is None:
        return ""
    return f"{scheme}://{ascii_host}:{port}"


def _normalize_for_json(value: Any, *, key: str | None = None) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, str):
            return _normalize_string(value, key=key)
        return value
    if isinstance(value, dict):
        return {
            str(inner_key): _normalize_for_json(inner, key=str(inner_key))
            for inner_key, inner in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(inner, key=key) for inner in value]
    return repr(value)


def _normalize_string(value: str, *, key: str | None) -> str:
    text = value.strip()
    if not text:
        return ""
    if key is None or not _is_url_key(key):
        return text
    origin = normalize_origin(text)
    if not origin:
        return text
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{origin}{path}{query}{fragment}"


def _is_url_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in _URL_PARAM_KEY_FRAGMENTS)


def _approval_scope_mismatch_reason(
    *,
    scope: ApprovalScope,
    context: ActionContext,
    now: float,
) -> str | None:
    if not scope.nonce or not scope.approved_by:
        return "approval_scope_incomplete"
    if now > scope.expires_at:
        return "approval_expired"
    if _normalize_action_name(scope.action_name) != context.action_name:
        return "approval_action_mismatch"
    if scope.params_hash != context.params_hash:
        return "approval_params_mismatch"
    if scope.current_origin != context.current_origin:
        return "approval_current_origin_mismatch"
    if scope.target_origin != context.target_origin:
        return "approval_target_origin_mismatch"
    if scope.task_id != context.task_id:
        return "approval_task_mismatch"
    if scope.browser_context_id != context.browser_context_id:
        return "approval_browser_context_mismatch"
    return None


def _decision(
    *,
    allowed: bool,
    reason_code: str,
    context: ActionContext,
    definition: ActionDefinition | None = None,
    approval_scope_present: bool = False,
    approval_scope_match: bool = False,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        reason_code=reason_code,
        action_name=context.action_name,
        risk=definition.risk if definition is not None else "unknown",
        data_access_class=definition.data_access_class if definition is not None else "unknown",
        side_effect_class=definition.side_effect_class if definition is not None else "unknown",
        approval_requirement=definition.approval_requirement
        if definition is not None
        else "unknown",
        params_hash=context.params_hash,
        current_origin=context.current_origin,
        target_origin=context.target_origin,
        task_id=context.task_id,
        browser_context_id=context.browser_context_id,
        approval_scope_present=approval_scope_present,
        approval_scope_match=approval_scope_match,
    )


def _effective_port(scheme: str) -> int | None:
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _normalize_action_name(action_name: str) -> str:
    return str(action_name or "").strip().lower()
