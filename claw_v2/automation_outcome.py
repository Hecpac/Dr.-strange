from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit


AutomationOutcomeStatus = Literal[
    "passed",
    "failed",
    "blocked",
    "no_result",
    "needs_login",
    "needs_approval",
]

VALID_AUTOMATION_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "blocked",
        "no_result",
        "needs_login",
        "needs_approval",
    }
)

CANONICAL_REASON_CODES = frozenset(
    {
        "ok",
        "no_result",
        "missing_screenshot",
        "missing_final_url",
        "missing_positive_assertion",
        "assertion_failed",
        "wrong_page",
        "login_required",
        "challenge_required",
        "approval_required",
        "browser_error",
        "executor_error",
        "policy_denied",
    }
)


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    passed: bool
    reason_code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("assertion name is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    status: AutomationOutcomeStatus
    reason_code: str
    human_summary: str
    final_url: str | None = None
    title: str | None = None
    screenshot_artifact_id: str | None = None
    evidence_artifact_ids: tuple[str, ...] = ()
    assertions: tuple[AssertionResult, ...] = ()

    def __post_init__(self) -> None:
        status = str(self.status or "").strip()
        if status not in VALID_AUTOMATION_STATUSES:
            raise ValueError(f"invalid automation outcome status: {self.status}")
        if not str(self.reason_code or "").strip():
            raise ValueError("reason_code is required")
        if not str(self.human_summary or "").strip():
            raise ValueError("human_summary is required")
        evidence = tuple(str(value).strip() for value in self.evidence_artifact_ids)
        if any(not value for value in evidence):
            raise ValueError("evidence_artifact_ids must not contain empty values")
        assertions = tuple(self.assertions)
        if any(not isinstance(item, AssertionResult) for item in assertions):
            raise TypeError("assertions must be AssertionResult instances")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", str(self.reason_code).strip())
        object.__setattr__(self, "human_summary", str(self.human_summary).strip())
        object.__setattr__(self, "evidence_artifact_ids", evidence)
        object.__setattr__(self, "assertions", assertions)
        if status == "passed" and not self.is_passed_validated():
            raise ValueError(f"passed outcome lacks required evidence: {self.invalid_passed_reason()}")

    def invalid_passed_reason(self) -> str | None:
        if self.status != "passed":
            return None
        if not self.final_url:
            return "missing_final_url"
        if not self.screenshot_artifact_id:
            return "missing_screenshot"
        if not any(assertion.passed for assertion in self.assertions):
            return "missing_positive_assertion"
        if any(not assertion.passed for assertion in self.assertions):
            return "assertion_failed"
        return None

    def is_passed_validated(self) -> bool:
        return self.status == "passed" and self.invalid_passed_reason() is None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_artifact_ids"] = list(self.evidence_artifact_ids)
        payload["assertions"] = [assertion.to_dict() for assertion in self.assertions]
        return payload

    def to_legacy_text(self) -> str:
        lines = [self.human_summary]
        if self.final_url:
            lines.append(f"URL final: {self.final_url}")
        if self.title:
            lines.append(f"Título: {self.title}")
        if self.screenshot_artifact_id:
            lines.append(f"Captura guardada: {self.screenshot_artifact_id}")
        return "\n".join(line for line in lines if line)

    @classmethod
    def passed(
        cls,
        *,
        human_summary: str,
        final_url: str,
        screenshot_artifact_id: str,
        assertions: tuple[AssertionResult, ...],
        title: str | None = None,
        evidence_artifact_ids: tuple[str, ...] = (),
    ) -> "AutomationOutcome":
        return cls(
            status="passed",
            reason_code="ok",
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
            evidence_artifact_ids=evidence_artifact_ids,
            assertions=assertions,
        )

    @classmethod
    def no_result(
        cls,
        *,
        human_summary: str,
        reason_code: str = "no_result",
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
        assertions: tuple[AssertionResult, ...] = (),
    ) -> "AutomationOutcome":
        return cls(
            status="no_result",
            reason_code=reason_code,
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
            assertions=assertions,
        )

    @classmethod
    def failed(
        cls,
        *,
        human_summary: str,
        reason_code: str = "browser_error",
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
        assertions: tuple[AssertionResult, ...] = (),
    ) -> "AutomationOutcome":
        return cls(
            status="failed",
            reason_code=reason_code,
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
            assertions=assertions,
        )

    @classmethod
    def blocked(
        cls,
        *,
        human_summary: str,
        reason_code: str,
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
        assertions: tuple[AssertionResult, ...] = (),
    ) -> "AutomationOutcome":
        return cls(
            status="blocked",
            reason_code=reason_code,
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
            assertions=assertions,
        )

    @classmethod
    def needs_login(
        cls,
        *,
        human_summary: str,
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
    ) -> "AutomationOutcome":
        return cls(
            status="needs_login",
            reason_code="login_required",
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
        )

    @classmethod
    def needs_approval(
        cls,
        *,
        human_summary: str,
        reason_code: str = "approval_required",
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
    ) -> "AutomationOutcome":
        return cls(
            status="needs_approval",
            reason_code=reason_code,
            human_summary=human_summary,
            final_url=final_url,
            title=title,
            screenshot_artifact_id=screenshot_artifact_id,
        )

    @classmethod
    def from_legacy_text(
        cls,
        text: str,
        *,
        final_url: str | None = None,
        title: str | None = None,
        screenshot_artifact_id: str | None = None,
        evidence_artifact_ids: tuple[str, ...] = (),
        objective: str | None = None,
    ) -> "AutomationOutcome":
        summary = str(text or "").strip() or "Tarea de navegador sin resultado."
        parsed_final_url = final_url or _extract_legacy_field(summary, "URL final")
        parsed_title = title or _extract_legacy_field(summary, "Título")
        parsed_screenshot = screenshot_artifact_id or _extract_legacy_screenshot(summary)
        normalized = _normalize(summary)
        if _looks_like_approval_required(normalized):
            return cls.needs_approval(human_summary=summary[:300], reason_code="policy_denied")
        if _looks_like_login_required(normalized):
            return cls.needs_login(
                human_summary=summary[:300],
                final_url=parsed_final_url,
                title=parsed_title,
                screenshot_artifact_id=parsed_screenshot,
            )
        if _looks_like_challenge_required(normalized):
            return cls.blocked(
                human_summary=summary[:300],
                reason_code="challenge_required",
                final_url=parsed_final_url,
                title=parsed_title,
                screenshot_artifact_id=parsed_screenshot,
            )
        if _looks_like_no_result(normalized):
            return cls.no_result(
                human_summary=summary[:300],
                reason_code="no_result",
                final_url=parsed_final_url,
                title=parsed_title,
                screenshot_artifact_id=parsed_screenshot,
            )
        if not parsed_final_url:
            return cls.no_result(human_summary=summary[:300], reason_code="missing_final_url")
        if not parsed_screenshot:
            return cls.no_result(
                human_summary=summary[:300],
                reason_code="missing_screenshot",
                final_url=parsed_final_url,
                title=parsed_title,
            )
        assertions = tuple(_assertions_for_objective(objective, parsed_final_url, summary))
        if any(not assertion.passed for assertion in assertions):
            return cls.failed(
                human_summary=summary[:300],
                reason_code="wrong_page",
                final_url=parsed_final_url,
                title=parsed_title,
                screenshot_artifact_id=parsed_screenshot,
                assertions=assertions,
            )
        if not any(assertion.passed for assertion in assertions):
            return cls.no_result(
                human_summary=summary[:300],
                reason_code="missing_positive_assertion",
                final_url=parsed_final_url,
                title=parsed_title,
                screenshot_artifact_id=parsed_screenshot,
                assertions=assertions,
            )
        return cls.passed(
            human_summary=summary[:300],
            final_url=parsed_final_url,
            title=parsed_title,
            screenshot_artifact_id=parsed_screenshot,
            evidence_artifact_ids=evidence_artifact_ids,
            assertions=assertions,
        )


def _normalize(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _extract_legacy_field(text: str, label: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(label)}:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _extract_legacy_screenshot(text: str) -> str | None:
    patterns = (
        r"Captura guardada:\s*([^\]\n]+)",
        r"\[Captura guardada:\s*([^\]\n]+)\]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _looks_like_approval_required(normalized: str) -> bool:
    return "necesito autorizacion" in normalized or "needs approval" in normalized


def _looks_like_login_required(normalized: str) -> bool:
    markers = (
        "login",
        "log in",
        "inicia sesion",
        "iniciar sesion",
        "deslogueado",
        "accounts/login",
    )
    return any(marker in normalized for marker in markers)


def _looks_like_challenge_required(normalized: str) -> bool:
    markers = ("challenge", "checkpoint", "muro de verificacion", "verificacion")
    return any(marker in normalized for marker in markers)


def _looks_like_no_result(normalized: str) -> bool:
    markers = (
        "(no result)",
        "sin resultado",
        "sin salida",
        "no pude completar",
        "no puedo completar",
        "no puedo ejecutar",
        "no tengo capacidad",
        "browser_use no esta disponible",
    )
    return not normalized or any(marker in normalized for marker in markers)


def _assertions_for_objective(
    objective: str | None,
    final_url: str,
    text: str,
) -> list[AssertionResult]:
    expected_url = _first_url(objective or "")
    if expected_url:
        expected_origin = _origin(expected_url)
        actual_origin = _origin(final_url)
        passed = bool(expected_origin and actual_origin == expected_origin)
        return [
            AssertionResult(
                name="expected_url_reached",
                passed=passed,
                reason_code=None if passed else "wrong_page",
                message=None if passed else f"expected {expected_origin}, got {actual_origin}",
            )
        ]
    extraction_markers = ("captur", "extra", "timeline", "posts", "resultado", "visible")
    if any(marker in _normalize(text) for marker in extraction_markers):
        return [AssertionResult(name="result_non_empty", passed=True)]
    return [AssertionResult(name="browser_reached_page", passed=True)]


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>)\]]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,;")


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    if port is None:
        return None
    return f"{parsed.scheme.lower()}://{parsed.hostname.encode('idna').decode('ascii').lower()}:{port}"
