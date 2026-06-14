from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class RawElement:
    """One interactive element as the backend sees it (pre-ref)."""
    selector: str
    role: str | None
    label: str
    text: str | None
    href: str | None
    input_type: str | None


@dataclass(slots=True)
class RawPage:
    """Backend's raw view of a page; the service turns this into refs + snapshot."""
    url: str
    title: str
    text: str
    elements: list[RawElement]
    login_or_challenge: bool = False


@dataclass(slots=True)
class BrowserElementRef:
    ref: str
    label: str
    role: str | None
    selector: str | None
    text: str | None
    href: str | None
    input_type: str | None


@dataclass(slots=True)
class BrowserToolResult:
    success: bool
    url: str | None = None
    title: str | None = None
    snapshot: str | None = None
    element_count: int = 0
    screenshot_path: str | None = None
    error: str | None = None
    backend: str = "chrome_cdp"
    metadata: dict[str, object] = field(default_factory=dict)


class BrowserToolBackend(Protocol):
    """Selector-level CDP operations. Refs are owned by the service, not here."""
    name: str

    def navigate(self, url: str) -> RawPage: ...
    def snapshot(self, full: bool = False) -> RawPage: ...
    def act(self, selector: str, action: str, text: str | None = None) -> RawPage: ...
    def screenshot(self, path: str) -> bool: ...
    def console(self, clear: bool = False) -> list[str]: ...


import logging
import threading
import time as _time
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_ELEMENTS = 150
SNAPSHOT_MAX_TEXT_CHARS = 2000


@dataclass(slots=True)
class BrowserToolSession:
    session_id: str
    cdp_endpoint: str
    backend: str
    current_url: str | None
    refs: dict[str, BrowserElementRef]
    ref_version: int
    last_used_at: float


class BrowserToolService:
    def __init__(self, *, backend: BrowserToolBackend, cdp_endpoint: str = "") -> None:
        self._backend = backend
        self._cdp_endpoint = cdp_endpoint
        self._sessions: dict[str, BrowserToolSession] = {}
        self._lock = threading.Lock()
        self.observe: Any | None = None

    def _session(self, session_id: str) -> BrowserToolSession:
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = BrowserToolSession(
                session_id=session_id,
                cdp_endpoint=self._cdp_endpoint,
                backend=self._backend.name,
                current_url=None,
                refs={},
                ref_version=0,
                last_used_at=_time.time(),
            )
            self._sessions[session_id] = sess
        return sess

    def _ingest(self, sess: BrowserToolSession, page: RawPage) -> BrowserToolResult:
        # Refs expire when a new snapshot is captured: replace the whole map and
        # bump the version so stale @eN refs from before are detectable.
        refs: dict[str, BrowserElementRef] = {}
        lines: list[str] = []
        truncated = len(page.elements) > SNAPSHOT_MAX_ELEMENTS
        for idx, el in enumerate(page.elements[:SNAPSHOT_MAX_ELEMENTS], start=1):
            ref = f"@e{idx}"
            refs[ref] = BrowserElementRef(
                ref=ref, label=el.label, role=el.role, selector=el.selector,
                text=el.text, href=el.href, input_type=el.input_type,
            )
            role = el.role or "element"
            line = f'{ref} {role} "{el.label}"'
            if el.href:
                line += f' href="{el.href}"'
            lines.append(line)
        sess.refs = refs
        sess.ref_version += 1
        sess.current_url = page.url
        sess.last_used_at = _time.time()
        body = page.text[:SNAPSHOT_MAX_TEXT_CHARS]
        snapshot = f"URL: {page.url}\nTITLE: {page.title}\n\n{body}\n\nELEMENTS ({len(refs)}):\n" + "\n".join(lines)
        if truncated:
            snapshot += f"\n[truncated: {len(page.elements)} elements, showing {SNAPSHOT_MAX_ELEMENTS}]"
        if page.login_or_challenge:
            # No-evasion: report human state, do not claim success.
            return BrowserToolResult(
                success=False, url=page.url, title=page.title, snapshot=snapshot,
                element_count=len(refs), backend=self._backend.name,
                error="login_or_challenge: page requires human login or verification",
                metadata={"login_or_challenge": True},
            )
        return BrowserToolResult(
            success=True, url=page.url, title=page.title, snapshot=snapshot,
            element_count=len(refs), backend=self._backend.name,
            metadata={"truncated": truncated, "ref_version": sess.ref_version},
        )

    def navigate(self, session_id: str, url: str) -> BrowserToolResult:
        with self._lock:
            sess = self._session(session_id)
            try:
                page = self._backend.navigate(url)
            except Exception as exc:
                return BrowserToolResult(success=False, error=str(exc)[:300],
                                         backend=self._backend.name)
            return self._ingest(sess, page)

    def snapshot(self, session_id: str, full: bool = False) -> BrowserToolResult:
        with self._lock:
            sess = self._session(session_id)
            try:
                page = self._backend.snapshot(full=full)
            except Exception as exc:
                return BrowserToolResult(success=False, error=str(exc)[:300],
                                         backend=self._backend.name)
            return self._ingest(sess, page)
