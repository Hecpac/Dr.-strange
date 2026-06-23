from __future__ import annotations

import concurrent.futures
import logging
import re as _re
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_ELEMENTS = 150
SNAPSHOT_MAX_TEXT_CHARS = 2000


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backend Protocol
# ---------------------------------------------------------------------------


class BrowserToolBackend(Protocol):
    """Selector-level CDP operations. Refs are owned by the service, not here."""

    name: str

    def navigate(self, url: str) -> RawPage: ...
    def snapshot(self, full: bool = False) -> RawPage: ...
    def act(
        self, selector: str, action: str, text: str | None = None, *, clear: bool = True
    ) -> RawPage: ...
    def screenshot(self, path: str) -> bool: ...
    def console(self, clear: bool = False) -> list[str]: ...


# ---------------------------------------------------------------------------
# Service session state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BrowserToolSession:
    session_id: str
    cdp_endpoint: str
    backend: str
    current_url: str | None
    refs: dict[str, BrowserElementRef]
    ref_version: int
    last_used_at: float
    observe: Any | None = field(default=None, repr=False, compare=False)
    action_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


# ---------------------------------------------------------------------------
# URL redaction helper
# ---------------------------------------------------------------------------


def _redact_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))
    except Exception:
        return "(unparseable url)"


_URL_RE = _re.compile(r"https?://\S+")


def _redact_err(msg: str) -> str:
    return _URL_RE.sub(lambda m: _redact_url(m.group(0)), msg)[:200]


_ALLOWED_NAVIGATION_SCHEMES = {"http", "https"}


def _validate_navigation_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except Exception as exc:
        raise ValueError("blocked_scheme: malformed_url") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_NAVIGATION_SCHEMES or not parts.netloc:
        raise ValueError(f"blocked_scheme: {scheme or 'missing'}")


# ---------------------------------------------------------------------------
# BrowserToolService
# ---------------------------------------------------------------------------


class BrowserToolService:
    def __init__(self, *, backend: BrowserToolBackend, cdp_endpoint: str = "") -> None:
        self._backend = backend
        self._cdp_endpoint = cdp_endpoint
        self._sessions: dict[str, BrowserToolSession] = {}
        # Protects only the session registry; action serialization lives on
        # each BrowserToolSession.action_lock.
        self._sessions_lock = threading.Lock()
        self.observe: Any | None = None

    def _session(self, session_id: str) -> BrowserToolSession:
        with self._sessions_lock:
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

    def _ingest(
        self, sess: BrowserToolSession, page: RawPage, *, full: bool = False
    ) -> BrowserToolResult:
        # Refs expire when a new snapshot is captured: replace the whole map and
        # bump the version so stale @eN refs from before are detectable.
        refs: dict[str, BrowserElementRef] = {}
        lines: list[str] = []
        truncated = len(page.elements) > SNAPSHOT_MAX_ELEMENTS
        for idx, el in enumerate(page.elements[:SNAPSHOT_MAX_ELEMENTS], start=1):
            ref = f"@e{idx}"
            refs[ref] = BrowserElementRef(
                ref=ref,
                label=el.label,
                role=el.role,
                selector=el.selector,
                text=el.text,
                href=el.href,
                input_type=el.input_type,
            )
            role = el.role or "element"
            line = f'{ref} {role} "{el.label}"'
            href_display = (el.href or "")[:300]
            if href_display:
                line += f' href="{href_display}"'
            lines.append(line)
        sess.refs = refs
        sess.ref_version += 1
        sess.current_url = page.url
        sess.last_used_at = _time.time()
        body = page.text if full else page.text[:SNAPSHOT_MAX_TEXT_CHARS]
        snapshot = (
            f"URL: {page.url[:300]}\nTITLE: {page.title[:200]}\n\n{body}\n\nELEMENTS ({len(refs)}):\n"
            + "\n".join(lines)
        )
        if truncated:
            snapshot += (
                f"\n[truncated: {len(page.elements)} elements, showing {SNAPSHOT_MAX_ELEMENTS}]"
            )
        if page.login_or_challenge:
            # No-evasion: report human state, do not claim success.
            return BrowserToolResult(
                success=False,
                url=page.url,
                title=page.title,
                snapshot=snapshot,
                element_count=len(refs),
                backend=self._backend.name,
                error="login_or_challenge: page requires human login or verification",
                metadata={"login_or_challenge": True},
            )
        return BrowserToolResult(
            success=True,
            url=page.url,
            title=page.title,
            snapshot=snapshot,
            element_count=len(refs),
            backend=self._backend.name,
            metadata={"truncated": truncated, "ref_version": sess.ref_version},
        )

    def _observer_for_session(
        self, sess: BrowserToolSession, observe: Any | None = None
    ) -> Any | None:
        if observe is not None:
            sess.observe = observe
            return observe
        if sess.observe is not None:
            return sess.observe
        return self.observe

    def _emit(self, observe: Any | None, event_type: str, payload: dict[str, Any]) -> None:
        obs = observe
        if obs is None:
            return
        try:
            emit = getattr(obs, "emit", None)
            if callable(emit):
                emit(event_type, payload=payload)
        except Exception:
            logger.debug("browser_tools observe emit failed: %s", event_type, exc_info=True)

    def _navigate_backend(self, sess: BrowserToolSession, url: str) -> RawPage:
        navigate_for_session = getattr(self._backend, "navigate_for_session", None)
        if callable(navigate_for_session):
            return navigate_for_session(sess.session_id, url)
        return self._backend.navigate(url)

    def _snapshot_backend(self, sess: BrowserToolSession, *, full: bool) -> RawPage:
        snapshot_for_session = getattr(self._backend, "snapshot_for_session", None)
        if callable(snapshot_for_session):
            return snapshot_for_session(sess.session_id, full=full)
        return self._backend.snapshot(full=full)

    def _act_backend(
        self,
        sess: BrowserToolSession,
        selector: str,
        action: str,
        text: str | None,
        *,
        clear: bool = True,
    ) -> RawPage:
        act_for_session = getattr(self._backend, "act_for_session", None)
        if callable(act_for_session):
            return act_for_session(sess.session_id, selector, action, text, clear=clear)
        return self._backend.act(selector, action, text, clear=clear)

    def _screenshot_backend(self, sess: BrowserToolSession, path: str) -> bool:
        screenshot_for_session = getattr(self._backend, "screenshot_for_session", None)
        if callable(screenshot_for_session):
            return bool(screenshot_for_session(sess.session_id, path))
        return bool(self._backend.screenshot(path))

    def close(self) -> None:
        with self._sessions_lock:
            self._sessions.clear()
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()

    def navigate(
        self, session_id: str, url: str, *, observe: Any | None = None
    ) -> BrowserToolResult:
        _validate_navigation_url(url)
        fail: tuple[BrowserToolResult, str] | None = None
        result: BrowserToolResult | None = None
        sess = self._session(session_id)
        event_observe: Any | None = None
        with sess.action_lock:
            event_observe = self._observer_for_session(sess, observe)
            self._emit(
                event_observe,
                "browser_tool_action_started",
                {"action": "navigate", "url": _redact_url(url), "backend": self._backend.name},
            )
            try:
                page = self._navigate_backend(sess, url)
            except Exception as exc:
                fail = (
                    BrowserToolResult(
                        success=False, error=str(exc)[:300], backend=self._backend.name
                    ),
                    _redact_err(str(exc)),
                )
            else:
                result = self._ingest(sess, page)
        if fail is not None:
            res, emsg = fail
            self._emit(
                event_observe,
                "browser_tool_action_failed",
                {"action": "navigate", "url": _redact_url(url), "error": emsg},
            )
            return res
        assert result is not None
        self._emit(
            event_observe,
            "browser_tool_action_completed",
            {
                "action": "navigate",
                "url": _redact_url(result.url),
                "success": result.success,
                "element_count": result.element_count,
            },
        )
        return result

    def snapshot(
        self, session_id: str, full: bool = False, *, observe: Any | None = None
    ) -> BrowserToolResult:
        fail: tuple[BrowserToolResult, str] | None = None
        result: BrowserToolResult | None = None
        sess = self._session(session_id)
        event_observe: Any | None = None
        with sess.action_lock:
            event_observe = self._observer_for_session(sess, observe)
            self._emit(
                event_observe,
                "browser_tool_action_started",
                {"action": "snapshot", "backend": self._backend.name},
            )
            try:
                page = self._snapshot_backend(sess, full=full)
            except Exception as exc:
                fail = (
                    BrowserToolResult(
                        success=False, error=str(exc)[:300], backend=self._backend.name
                    ),
                    _redact_err(str(exc)),
                )
            else:
                result = self._ingest(sess, page, full=full)
        if fail is not None:
            res, emsg = fail
            self._emit(
                event_observe,
                "browser_tool_action_failed",
                {"action": "snapshot", "error": emsg},
            )
            return res
        assert result is not None
        self._emit(
            event_observe,
            "browser_tool_action_completed",
            {
                "action": "snapshot",
                "url": _redact_url(result.url),
                "success": result.success,
                "element_count": result.element_count,
            },
        )
        return result

    def _act(
        self,
        session_id: str,
        ref: str,
        action: str,
        text: str | None = None,
        *,
        clear: bool = True,
        observe: Any | None = None,
    ) -> BrowserToolResult:
        fail: tuple[BrowserToolResult, str] | None = None
        result: BrowserToolResult | None = None
        sess = self._session(session_id)
        event_observe: Any | None = None
        with sess.action_lock:
            event_observe = self._observer_for_session(sess, observe)
            self._emit(
                event_observe,
                "browser_tool_action_started",
                {"action": action, "ref": ref, "backend": self._backend.name},
            )
            target = sess.refs.get(ref)
            if target is None or not target.selector:
                fail = (
                    BrowserToolResult(
                        success=False,
                        url=sess.current_url,
                        backend=self._backend.name,
                        error=f"stale_ref: {ref} not in current snapshot",
                        metadata={"ref_version": sess.ref_version},
                    ),
                    f"stale_ref: {ref} not in current snapshot",
                )
            else:
                try:
                    page = self._act_backend(
                        sess, target.selector, action, text, clear=clear
                    )
                except Exception as exc:
                    fail = (
                        BrowserToolResult(
                            success=False,
                            url=sess.current_url,
                            backend=self._backend.name,
                            error=str(exc)[:300],
                        ),
                        _redact_err(str(exc)),
                    )
                else:
                    result = self._ingest(sess, page)
        if fail is not None:
            res, emsg = fail
            self._emit(
                event_observe,
                "browser_tool_action_failed",
                {"action": action, "ref": ref, "error": emsg},
            )
            return res
        assert result is not None
        self._emit(
            event_observe,
            "browser_tool_action_completed",
            {
                "action": action,
                "ref": ref,
                "url": _redact_url(result.url),
                "success": result.success,
            },
        )
        return result

    def click(self, session_id: str, ref: str, *, observe: Any | None = None) -> BrowserToolResult:
        return self._act(session_id, ref, "click", observe=observe)

    def type(
        self,
        session_id: str,
        ref: str,
        text: str,
        clear: bool = True,
        *,
        observe: Any | None = None,
    ) -> BrowserToolResult:
        return self._act(session_id, ref, "type", text, clear=clear, observe=observe)

    def screenshot(
        self, session_id: str, path: str | None = None, *, observe: Any | None = None
    ) -> BrowserToolResult:
        if not path:
            raise ValueError("screenshot_path_required")
        sess = self._session(session_id)
        ok = False
        error: str | None = None
        event_observe: Any | None = None
        with sess.action_lock:
            event_observe = self._observer_for_session(sess, observe)
            self._emit(
                event_observe,
                "browser_tool_action_started",
                {"action": "screenshot", "path": path, "backend": self._backend.name},
            )
            try:
                ok = self._screenshot_backend(sess, path)
            except Exception as exc:
                error = str(exc)[:300]
        if error is not None:
            self._emit(
                event_observe,
                "browser_tool_action_failed",
                {"action": "screenshot", "error": error[:200]},
            )
            return BrowserToolResult(success=False, error=error, backend=self._backend.name)
        result = BrowserToolResult(
            success=ok,
            screenshot_path=path if ok else None,
            backend=self._backend.name,
        )
        self._emit(
            event_observe,
            "browser_tool_action_completed",
            {"action": "screenshot", "success": result.success, "path": result.screenshot_path},
        )
        return result


# ---------------------------------------------------------------------------
# ChromeCdpBrowserBackend (real Playwright CDP)
# ---------------------------------------------------------------------------

_LOGIN_MARKERS = (
    "log in",
    "sign in",
    "login",
    "iniciar sesión",
    "verify you are human",
    "verifica que eres",
    "captcha",
    "unusual activity",
    "are you a robot",
    "checking your browser",
    "enable javascript and cookies",
)

_SNAPSHOT_JS = r"""
() => {
  const sel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return el.tagName.toLowerCase() + '[name="' + nm + '"]';
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 4) {
      let p = n.tagName.toLowerCase();
      if (n.parentElement) {
        const sibs = Array.from(n.parentElement.children).filter(c => c.tagName === n.tagName);
        if (sibs.length > 1) p += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
      }
      parts.unshift(p);
      n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const q = 'a[href],button,input,textarea,select,[role=button],[role=link],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  const out = [];
  for (const el of Array.from(document.querySelectorAll(q)).slice(0, 150)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value ||
                   el.getAttribute('placeholder') || el.getAttribute('title') || '').trim().slice(0, 80);
    out.push({
      selector: sel(el),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      label: label,
      text: (el.innerText || '').trim().slice(0, 120),
      href: el.getAttribute('href') || null,
      input_type: el.getAttribute('type') || null,
    });
  }
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, 4000),
    elements: out,
  };
}
"""


@dataclass(slots=True)
class _ChromeCdpOwnedPage:
    context: Any
    page: Any


class ChromeCdpBrowserBackend:
    """Selector-level CDP backend over Playwright sync API.

    SYNCHRONOUS on purpose: callers (the ToolRegistry handler) invoke it off the
    event loop via asyncio.to_thread (C3). sync_playwright cannot run inside a
    live asyncio loop, so never call this from the brain coroutine directly.

    The sync Playwright/CDP connection is owned by one backend worker thread.
    BrowserToolService still serializes same-session actions; the backend also
    maps each service session id to its own BrowserContext/Page and serializes
    CDP calls on the worker so different sessions never touch Chrome's first
    shared page by accident.
    """

    name = "chrome_cdp"

    def __init__(self, *, cdp_endpoint: str, nav_timeout_ms: int = 45000) -> None:
        self._endpoint = cdp_endpoint
        self._nav_timeout = nav_timeout_ms
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="claw-browser-cdp"
        )
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._pw_manager: Any | None = None
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._owned_pages: dict[str, _ChromeCdpOwnedPage] = {}

    def _run_on_worker(self, fn):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("chrome_cdp_backend_closed")
            future = self._executor.submit(fn)
        return future.result()

    def _browser_connected(self) -> bool:
        if self._browser is None:
            return False
        is_connected = getattr(self._browser, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return True

    def _cleanup_worker(self) -> None:
        for owned in list(self._owned_pages.values()):
            try:
                owned.context.close()
            except Exception:  # noqa: BLE001 - best-effort browser cleanup must continue
                logger.debug("failed to close owned browser context", exc_info=True)
        self._owned_pages.clear()
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - best-effort browser cleanup must continue
                logger.debug("failed to close CDP browser", exc_info=True)
            self._browser = None
        if self._pw_manager is not None:
            try:
                self._pw_manager.stop()
            except Exception:  # noqa: BLE001 - best-effort browser cleanup must continue
                logger.debug("failed to stop Playwright manager", exc_info=True)
            self._pw_manager = None
        self._pw = None

    def _ensure_connected_worker(self) -> None:
        from claw_v2.browser import _cdp_connect, _require_sync_playwright

        if self._browser_connected():
            return
        self._cleanup_worker()
        self._pw_manager = _require_sync_playwright()
        self._pw = self._pw_manager.start()
        try:
            self._browser = _cdp_connect(self._pw, self._endpoint, enable_downloads=False)
        except Exception:
            self._cleanup_worker()
            raise

    def _page_is_usable(self, owned: _ChromeCdpOwnedPage) -> bool:
        is_closed = getattr(owned.page, "is_closed", None)
        if callable(is_closed):
            try:
                return not bool(is_closed())
            except Exception:
                return False
        return True

    def _discard_session_worker(self, session_id: str) -> None:
        owned = self._owned_pages.pop(session_id, None)
        if owned is None:
            return
        try:
            owned.context.close()
        except Exception:  # noqa: BLE001 - best-effort browser cleanup must continue
            logger.debug("failed to close discarded browser context", exc_info=True)

    def _owned_page_worker(self, session_id: str) -> _ChromeCdpOwnedPage:
        self._ensure_connected_worker()
        owned = self._owned_pages.get(session_id)
        if owned is not None and self._page_is_usable(owned):
            return owned
        self._discard_session_worker(session_id)
        assert self._browser is not None
        context = None
        try:
            context = self._browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
        except Exception as exc:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 - best-effort browser cleanup must continue
                    logger.debug(
                        "failed to close partially-created browser context",
                        exc_info=True,
                    )
            raise RuntimeError(
                "cdp_session_isolation_unavailable: could not create isolated browser context"
            ) from exc
        owned = _ChromeCdpOwnedPage(context=context, page=page)
        self._owned_pages[session_id] = owned
        return owned

    def _with_page(self, session_id_or_fn, fn=None):
        if fn is None:
            session_id = "brain"
            callback = session_id_or_fn
        else:
            session_id = str(session_id_or_fn or "brain")
            callback = fn

        def _work():
            owned = self._owned_page_worker(session_id)
            return callback(owned.page)

        return self._run_on_worker(_work)

    def _read_page(self, page) -> RawPage:
        data = page.evaluate(_SNAPSHOT_JS)
        text = str(data.get("text") or "")
        login = any(m in text.lower() for m in _LOGIN_MARKERS)
        elements = [
            RawElement(
                selector=str(e.get("selector") or ""),
                role=e.get("role"),
                label=str(e.get("label") or ""),
                text=e.get("text"),
                href=e.get("href"),
                input_type=e.get("input_type"),
            )
            for e in (data.get("elements") or [])
            if e.get("selector")
        ]
        return RawPage(
            url=str(data.get("url") or ""),
            title=str(data.get("title") or ""),
            text=text,
            elements=elements,
            login_or_challenge=login,
        )

    def navigate_for_session(self, session_id: str, url: str) -> RawPage:
        def _go(page):
            # C4: domcontentloaded, then best-effort load/networkidle (bounded).
            page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout)
            for state in ("load", "networkidle"):
                try:
                    page.wait_for_load_state(state, timeout=8000)
                except Exception:
                    pass
            return self._read_page(page)

        return self._with_page(session_id, _go)

    def navigate(self, url: str) -> RawPage:
        return self.navigate_for_session("brain", url)

    def snapshot_for_session(self, session_id: str, full: bool = False) -> RawPage:
        return self._with_page(session_id, self._read_page)

    def snapshot(self, full: bool = False) -> RawPage:
        return self.snapshot_for_session("brain", full=full)

    def act_for_session(
        self,
        session_id: str,
        selector: str,
        action: str,
        text: str | None = None,
        *,
        clear: bool = True,
    ) -> RawPage:
        def _do(page):
            if action == "click":
                page.click(selector, timeout=10000)
            elif action == "type":
                if clear:
                    page.fill(selector, text or "", timeout=10000)
                else:
                    page.type(selector, text or "", timeout=10000)
            else:
                raise ValueError(f"unsupported action: {action}")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            return self._read_page(page)

        return self._with_page(session_id, _do)

    def act(
        self, selector: str, action: str, text: str | None = None, *, clear: bool = True
    ) -> RawPage:
        return self.act_for_session("brain", selector, action, text, clear=clear)

    def screenshot_for_session(self, session_id: str, path: str) -> bool:
        def _shot(page):
            page.screenshot(path=path, full_page=True)
            return True

        return self._with_page(session_id, _shot)

    def screenshot(self, path: str) -> bool:
        return self.screenshot_for_session("brain", path)

    def console(self, clear: bool = False) -> list[str]:
        # Console history needs a persistent listener; PR1 returns empty and the
        # tool degrades. Real console capture lands in PR5 with the dialog work.
        return []

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
        try:
            executor.submit(self._cleanup_worker).result()
        finally:
            executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_chrome_cdp_service(
    *, cdp_endpoint: str, observe: Any | None = None
) -> BrowserToolService:
    svc = BrowserToolService(
        backend=ChromeCdpBrowserBackend(cdp_endpoint=cdp_endpoint), cdp_endpoint=cdp_endpoint
    )
    svc.observe = observe
    return svc
