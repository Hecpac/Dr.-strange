from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from claw_v2.redaction import redact_text

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_EVIDENCE_TARGETS = 3
DEFAULT_BROWSER_EVIDENCE_SNAPSHOT_CHARS = 6_000
DEFAULT_BROWSER_EVIDENCE_TOTAL_CHARS = 18_000

_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()\"']+)", re.IGNORECASE)
_HOST_URL_RE = re.compile(
    r"(?P<url>(?<!@)(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})"
    r"(?::\d+)?(?:[/?#][^\s<>()\"']*)?)",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}'\""
_CURRENT_PAGE_RE = re.compile(
    r"\b("
    r"current\s+(?:page|tab)|"
    r"p[aá]gina\s+actual|"
    r"pestana\s+actual|"
    r"pestaña\s+actual|"
    r"tab\s+actual"
    r")\b",
    re.IGNORECASE,
)


ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BrowserEvidenceReport:
    content: str
    duration_seconds: float
    target_count: int
    status: str


class BrowserEvidenceCollector:
    """Collect read-only browser evidence for coordinator synthesis.

    The collector is intentionally tool-name constrained. It may navigate and
    snapshot; it never clicks, types, screenshots, or performs a local mutation.
    ToolRegistry policy remains the enforcement boundary for network, context,
    and tier decisions.
    """

    def __init__(
        self,
        *,
        tool_executor: ToolExecutor,
        observe: Any | None = None,
        max_targets: int = DEFAULT_BROWSER_EVIDENCE_TARGETS,
        snapshot_chars: int = DEFAULT_BROWSER_EVIDENCE_SNAPSHOT_CHARS,
        total_chars: int = DEFAULT_BROWSER_EVIDENCE_TOTAL_CHARS,
    ) -> None:
        if max_targets <= 0:
            raise ValueError("max_targets must be positive")
        if snapshot_chars <= 0:
            raise ValueError("snapshot_chars must be positive")
        if total_chars <= 0:
            raise ValueError("total_chars must be positive")
        self._tool_executor = tool_executor
        self.observe = observe
        self.max_targets = int(max_targets)
        self.snapshot_chars = int(snapshot_chars)
        self.total_chars = int(total_chars)

    @classmethod
    def from_tool_registry(
        cls,
        registry: Any,
        *,
        policy: Any,
        network_enforcer: Any | None = None,
        observe: Any | None = None,
        max_targets: int = DEFAULT_BROWSER_EVIDENCE_TARGETS,
        snapshot_chars: int = DEFAULT_BROWSER_EVIDENCE_SNAPSHOT_CHARS,
        total_chars: int = DEFAULT_BROWSER_EVIDENCE_TOTAL_CHARS,
    ) -> "BrowserEvidenceCollector":
        def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            return registry.execute(
                name,
                args,
                agent_class="researcher",
                policy=policy,
                network_enforcer=network_enforcer,
                session_id=str(args.get("session_id") or "coordinator"),
            )

        return cls(
            tool_executor=execute,
            observe=observe,
            max_targets=max_targets,
            snapshot_chars=snapshot_chars,
            total_chars=total_chars,
        )

    def collect(
        self,
        *,
        task_id: str,
        objective: str,
        research_results: Sequence[Any] = (),
    ) -> BrowserEvidenceReport | None:
        start = time.time()
        texts = [objective, *(_result_text(result) for result in research_results)]
        targets = extract_browser_evidence_targets(texts, limit=self.max_targets)
        wants_current_page = _requests_current_page_snapshot(objective)
        if not targets and not wants_current_page:
            return None

        session_id = f"coordinator:{task_id}"
        sections: list[str] = [
            "## Browser Evidence",
            "Source: managed browser read-only tools (BrowserNavigate/BrowserSnapshot).",
        ]
        successes = 0
        failures = 0

        if targets:
            for url in targets:
                navigate = self._execute_read_tool(
                    "BrowserNavigate", {"url": url, "session_id": session_id}
                )
                if bool(navigate.get("ok")):
                    successes += 1
                else:
                    failures += 1
                sections.append(
                    _format_browser_tool_result(
                        "BrowserNavigate", navigate, url=url, snapshot_chars=self.snapshot_chars
                    )
                )
                if bool(navigate.get("ok")):
                    snapshot = self._execute_read_tool(
                        "BrowserSnapshot", {"session_id": session_id, "full": False}
                    )
                    if bool(snapshot.get("ok")):
                        successes += 1
                    else:
                        failures += 1
                    sections.append(
                        _format_browser_tool_result(
                            "BrowserSnapshot", snapshot, snapshot_chars=self.snapshot_chars
                        )
                    )
        elif wants_current_page:
            snapshot = self._execute_read_tool(
                "BrowserSnapshot", {"session_id": session_id, "full": False}
            )
            if bool(snapshot.get("ok")):
                successes += 1
            else:
                failures += 1
            sections.append(
                _format_browser_tool_result(
                    "BrowserSnapshot", snapshot, snapshot_chars=self.snapshot_chars
                )
            )

        status = _status_for_counts(successes=successes, failures=failures)
        sections.append(f"Status: {status}")
        content = redact_text("\n\n".join(sections), limit=self.total_chars)
        duration = time.time() - start
        self._emit(
            "coordinator_browser_evidence_collected",
            {
                "task_id": task_id,
                "target_count": len(targets),
                "current_page_snapshot": wants_current_page and not targets,
                "status": status,
                "duration_seconds": duration,
            },
        )
        return BrowserEvidenceReport(
            content=content,
            duration_seconds=duration,
            target_count=len(targets),
            status=status,
        )

    def _execute_read_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in {"BrowserNavigate", "BrowserSnapshot"}:
            raise ValueError(f"unsupported browser evidence tool: {name}")
        try:
            result = self._tool_executor(name, args)
        except Exception as exc:  # noqa: BLE001 - policy/tool failures become evidence blockers
            logger.info("browser evidence tool failed: %s", name, exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if not isinstance(result, dict):
            return {"ok": False, "error": f"unexpected_result_type:{type(result).__name__}"}
        return result

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        observe = self.observe
        if observe is None:
            return
        try:
            emit = getattr(observe, "emit", None)
            if callable(emit):
                emit(event_type, payload=payload)
        except Exception:
            logger.debug("browser evidence observe emit failed: %s", event_type, exc_info=True)


def extract_browser_evidence_targets(texts: Iterable[str], *, limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    targets: list[str] = []
    for text in texts:
        if not text:
            continue
        for raw in _iter_raw_urls(str(text)):
            normalized = _normalize_browser_url(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            targets.append(normalized)
            if len(targets) >= limit:
                return tuple(targets)
    return tuple(targets)


def _iter_raw_urls(text: str) -> Iterable[str]:
    scheme_spans: list[tuple[int, int]] = []
    for match in _SCHEME_URL_RE.finditer(text):
        scheme_spans.append(match.span())
        yield match.group("url")
    for match in _HOST_URL_RE.finditer(text):
        start, end = match.span()
        if any(
            start >= scheme_start and end <= scheme_end for scheme_start, scheme_end in scheme_spans
        ):
            continue
        yield match.group("url")


def _normalize_browser_url(raw: str) -> str | None:
    cleaned = str(raw or "").strip().rstrip(_TRAILING_URL_PUNCTUATION)
    if not cleaned:
        return None
    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned
    return f"https://{cleaned}"


def _requests_current_page_snapshot(objective: str) -> bool:
    return _CURRENT_PAGE_RE.search(objective or "") is not None


def _result_text(result: Any) -> str:
    content = getattr(result, "content", "")
    error = getattr(result, "error", "")
    if error:
        return f"{content}\n{error}"
    return str(content or "")


def _format_browser_tool_result(
    tool_name: str,
    result: dict[str, Any],
    *,
    url: str | None = None,
    snapshot_chars: int = DEFAULT_BROWSER_EVIDENCE_SNAPSHOT_CHARS,
) -> str:
    ok = bool(result.get("ok"))
    lines = [f"### {tool_name}", f"ok: {'true' if ok else 'false'}"]
    display_url = result.get("url") or url
    if display_url:
        lines.append(f"url: {redact_text(str(display_url), limit=500)}")
    title = result.get("title")
    if title:
        lines.append(f"title: {redact_text(str(title), limit=500)}")
    error = result.get("error")
    if error:
        lines.append(f"error: {redact_text(str(error), limit=500)}")
    element_count = result.get("element_count")
    if element_count is not None:
        lines.append(f"element_count: {element_count}")
    snapshot = result.get("snapshot")
    if snapshot:
        lines.append("snapshot:")
        lines.append(redact_text(str(snapshot), limit=snapshot_chars))
    return "\n".join(lines)


def _status_for_counts(*, successes: int, failures: int) -> str:
    if successes and failures:
        return "partial"
    if successes:
        return "collected"
    return "blocked"
