from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import urlparse

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import (
    _extract_title_from_url,
    _format_chrome_cdp_error,
    _format_link_analysis_prompt,
    _is_tweet_url,
    _is_usable_browse_content,
    _jina_read,
    _normalize_url,
    _select_navigation_strategy,
    _tweet_fxtwitter_read,
)

logger = logging.getLogger(__name__)


class BrowseHandler:
    def __init__(
        self,
        *,
        config: Any | None = None,
        observe: Any | None = None,
        get_learning: Callable[[], Any | None] = lambda: None,
        get_browser: Callable[[], Any | None],
        get_managed_chrome: Callable[[], Any | None],
        wiki_ingest: Callable[[str, str, str], None],
        capability_unavailable_message: Callable[[str, str], str | None],
        update_session_state: Callable[..., Any],
        get_session_state: Callable[[str], dict[str, Any]],
    ) -> None:
        self.config = config
        self.observe = observe
        self._get_learning = get_learning
        self._get_browser = get_browser
        self._get_managed_chrome = get_managed_chrome
        self._wiki_ingest = wiki_ingest
        self._capability_unavailable_message = capability_unavailable_message
        self._update_session_state = update_session_state
        self._get_session_state = get_session_state
        self._recent_browse_urls: dict[str, str] = {}

    @property
    def browser(self) -> Any | None:
        return self._get_browser()

    @property
    def managed_chrome(self) -> Any | None:
        return self._get_managed_chrome()

    def browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self.remember_recent_browse_url(session_id, normalized_url)

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
        navigation_strategy = _select_navigation_strategy(normalized_url)
        auth_required = navigation_strategy == "authenticated"
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
                navigation_strategy=navigation_strategy,
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
            navigation_strategy=navigation_strategy,
        )
        if outcome["status"] != "success":
            self._record_learning_outcome(
                task_type="browse",
                session_id=session_id,
                description=f"Browse {outcome['status']} for {normalized_url}",
                approach=f"strategy={outcome['strategy']} backend={outcome['selected_backend']}",
                outcome="failure" if outcome["status"] == "error" else "partial",
                error_snippet=response[:500],
                lesson=(
                    "Authenticated or JS-heavy pages need a better backend selection or clearer fallback messaging."
                    if outcome["strategy"] == "authenticated"
                    else "When all browse backends fail, capture the failing backend path and retry strategy explicitly."
                ),
            )
        else:
            browse_title = _extract_title_from_url(normalized_url)
            self._wiki_ingest(browse_title, response, "browse")
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
        navigation_strategy: str,
        configured_backend: str,
        cdp_available: bool,
        browserbase_available: bool,
        playwright_available: bool,
    ) -> tuple[str, dict[str, str]]:
        if navigation_strategy == "js_rendered":
            if playwright_available:
                content = self._playwright_browse_response(url)
                if content:
                    return content, {
                        "strategy": "public",
                        "selected_backend": "playwright_local",
                        "status": "success",
                        "note": "js_rendered",
                    }

            if browserbase_available:
                content = self._browserbase_browse_response(url)
                if content:
                    return content, {
                        "strategy": "public",
                        "selected_backend": "browserbase_cdp",
                        "status": "success",
                        "note": "js_rendered",
                    }

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

        degraded_message = self._capability_unavailable_message(
            "chrome_cdp",
            f"browse error: {url} requiere navegador autenticado y Chrome CDP no está disponible.",
        )
        return degraded_message or f"browse error: {url} requiere navegador autenticado y Chrome CDP no está disponible.", {
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
        navigation_strategy: str | None = None,
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
        if navigation_strategy:
            payload["navigation_strategy"] = navigation_strategy
        self.observe.emit("browse_result", payload=payload)

    def _record_learning_outcome(
        self,
        *,
        task_type: str,
        session_id: str | None,
        description: str,
        approach: str,
        outcome: str,
        error_snippet: str | None = None,
        lesson: str | None = None,
        predicted_confidence: float | None = None,
    ) -> None:
        learning = self._get_learning()
        if learning is None:
            return
        task_id = f"{session_id or 'global'}:{time.time_ns()}"
        try:
            learning.record(
                task_type=task_type,
                task_id=task_id,
                description=description,
                approach=approach,
                outcome=outcome,
                error_snippet=error_snippet,
                lesson=lesson,
                predicted_confidence=predicted_confidence,
            )
        except Exception:
            logger.debug("learning record failed for %s", task_type, exc_info=True)

    def link_review_shortcut(self, text: str, url: str, *, session_id: str) -> object:
        from claw_v2.bot import _BrainShortcut
        try:
            normalized_url = _normalize_url(url)
        except ValueError:
            normalized_url = url
        fetched_content = self.browse_response(url, session_id=session_id)
        return _BrainShortcut(
            text=_format_link_analysis_prompt(text, normalized_url, fetched_content),
            memory_text=text,
        )

    def remember_recent_browse_url(self, session_id: str | None, url: str) -> None:
        if session_id:
            self._recent_browse_urls[session_id] = url
            self._update_session_state(
                session_id,
                mode="browse",
                active_object={"kind": "url", "url": url},
            )

    def recent_tweet_url(self, session_id: str) -> str | None:
        url = self._recent_browse_urls.get(session_id)
        if url and _is_tweet_url(url):
            return url
        return None
