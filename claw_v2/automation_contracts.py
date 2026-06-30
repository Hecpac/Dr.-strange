from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class AutomationSurface(Enum):
    BROWSER = "browser"
    DESKTOP = "desktop"


class AutomationExecutor(Enum):
    DETERMINISTIC_BROWSER = "deterministic_browser"
    BROWSER_USE = "browser_use"
    COMPUTER_USE = "computer_use"


class AutomationIntent(Enum):
    OPEN_URL = "open_url"
    SNAPSHOT = "snapshot"
    EXTRACT = "extract"
    CLICK = "click"
    FORM_FILL = "form_fill"
    AUTH_CHECK = "auth_check"
    EXPLORE = "explore"
    COMPUTER_APP = "computer_app"


class AutomationStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"
    BLOCKED = "blocked"
    BLOCKED_BY_LOGIN = "blocked_by_login"
    BLOCKED_BY_CHALLENGE = "blocked_by_challenge"
    RUNTIME_FAILED = "runtime_failed"
    NO_RESULT = "no_result"
    TIMED_OUT = "timed_out"


BROWSER_READ_HIGH_RISK_ACTIONS: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationRequest:
    request_id: str
    session_id: str
    task_id: str
    objective: str
    mode: str
    surface: AutomationSurface
    intent: AutomationIntent
    target_url: str | None = None
    target_domains: tuple[str, ...] = ()
    requested_actions: tuple[str, ...] = ()
    evidence_required: tuple[str, ...] = ()
    time_budget_seconds: int | None = None
    model_policy: str = "subscription_first"

    @classmethod
    def browser(
        cls,
        *,
        request_id: str,
        session_id: str,
        task_id: str,
        objective: str,
        mode: str,
        intent: AutomationIntent,
        target_url: str | None = None,
        target_domains: list[str] | tuple[str, ...] = (),
        requested_actions: list[str] | tuple[str, ...] = (),
        evidence_required: list[str] | tuple[str, ...] = (),
        time_budget_seconds: int | None = None,
        model_policy: str = "subscription_first",
    ) -> "AutomationRequest":
        return cls(
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            objective=objective,
            mode=mode,
            surface=AutomationSurface.BROWSER,
            intent=intent,
            target_url=target_url,
            target_domains=tuple(_normalize_domains(target_domains)),
            requested_actions=tuple(str(action) for action in requested_actions),
            evidence_required=tuple(str(item) for item in evidence_required),
            time_budget_seconds=time_budget_seconds,
            model_policy=model_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["surface"] = self.surface.value
        payload["intent"] = self.intent.value
        payload["target_domains"] = list(self.target_domains)
        payload["requested_actions"] = list(self.requested_actions)
        payload["evidence_required"] = list(self.evidence_required)
        return payload


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    surface: AutomationSurface
    reason: str
    approved_domains: tuple[str, ...] = ()
    allow_high_risk_actions: bool = False
    allowed_high_risk_actions: tuple[str, ...] = ()
    approved_by: str = "system"
    auto_approved: bool = False
    sensitive: bool = False

    @classmethod
    def browser_read(
        cls,
        *,
        domains: list[str] | tuple[str, ...],
        reason: str,
        auto_approved: bool = False,
        approved_by: str = "system",
        sensitive: bool = False,
    ) -> "CapabilityGrant":
        return cls(
            surface=AutomationSurface.BROWSER,
            reason=reason,
            approved_domains=tuple(_normalize_domains(domains)),
            allow_high_risk_actions=False,
            allowed_high_risk_actions=(),
            approved_by=approved_by,
            auto_approved=auto_approved,
            sensitive=sensitive,
        )

    def approved_domains_list(self) -> list[str]:
        return list(self.approved_domains)

    def allowed_high_risk_actions_list(self) -> list[str]:
        return list(self.allowed_high_risk_actions)

    def allows_browser_use_action(
        self,
        action_name: str,
        *,
        url: str | None,
        params: dict[str, Any] | None = None,
    ) -> bool:
        if self.surface is not AutomationSurface.BROWSER:
            return False
        if not self.allow_high_risk_actions:
            return False
        if self.sensitive and self.approved_by != "user":
            return False
        allowed_actions = {action.lower() for action in self.allowed_high_risk_actions}
        if allowed_actions and str(action_name or "").lower() not in allowed_actions:
            return False
        if not self.approved_domains:
            return False
        params = params or {}
        candidates = [url, str(params.get("url") or "").strip() or None]
        return any(
            _url_matches_domains(candidate, self.approved_domains)
            for candidate in candidates
            if candidate
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["surface"] = self.surface.value
        payload["approved_domains"] = list(self.approved_domains)
        payload["allowed_high_risk_actions"] = list(self.allowed_high_risk_actions)
        return payload


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    status: AutomationStatus
    surface: AutomationSurface
    executor: AutomationExecutor
    summary: str
    reason_code: str = ""
    artifacts: tuple[str, ...] = ()
    detail: str = ""
    needs_approval_reason: str | None = None

    @classmethod
    def passed(
        cls,
        *,
        surface: AutomationSurface,
        executor: AutomationExecutor,
        summary: str,
        artifacts: list[str] | tuple[str, ...] = (),
        detail: str = "",
    ) -> "AutomationOutcome":
        return cls(
            status=AutomationStatus.PASSED,
            surface=surface,
            executor=executor,
            summary=summary,
            reason_code="passed",
            artifacts=tuple(artifacts),
            detail=detail,
        )

    @classmethod
    def failed(
        cls,
        *,
        surface: AutomationSurface,
        executor: AutomationExecutor,
        summary: str,
        detail: str = "",
        reason_code: str = "runtime_failed",
        status: AutomationStatus = AutomationStatus.RUNTIME_FAILED,
    ) -> "AutomationOutcome":
        return cls(
            status=status,
            surface=surface,
            executor=executor,
            summary=summary,
            reason_code=reason_code,
            detail=detail,
        )

    @classmethod
    def needs_approval(
        cls,
        *,
        surface: AutomationSurface,
        executor: AutomationExecutor,
        summary: str,
        reason: str,
    ) -> "AutomationOutcome":
        return cls(
            status=AutomationStatus.NEEDS_APPROVAL,
            surface=surface,
            executor=executor,
            summary=summary,
            reason_code="needs_approval",
            needs_approval_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["surface"] = self.surface.value
        payload["executor"] = self.executor.value
        payload["artifacts"] = list(self.artifacts)
        return payload


def _normalize_domains(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        host = _host_from_url(value)
        if not host:
            host = str(value or "").strip().lower().strip("/")
        if not host or host in seen:
            continue
        seen.add(host)
        normalized.append(host)
    return normalized


def _url_matches_domains(url: str | None, domains: tuple[str, ...]) -> bool:
    host = _host_from_url(url)
    if not host:
        return False
    for domain in domains:
        normalized = domain.lower().lstrip("*.").strip()
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def _host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip().lower()
    if not text:
        return None
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host.lower().strip(".")
