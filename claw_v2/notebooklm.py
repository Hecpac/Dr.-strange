from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from claw_v2 import notebooklm_cdp

if TYPE_CHECKING:
    from claw_v2.jobs import JobService

logger = logging.getLogger(__name__)

ResearchFallback = Callable[[str], str | dict[str, Any] | None]

class NotebookLMSDKUnavailable(RuntimeError):
    """Raised when an operation requires the notebooklm SDK but it is not installed."""


_NOTEBOOKLM_TIMEOUT_PATTERNS = ("timeout", "timed out", "did not complete", "deadline")
_NOTEBOOKLM_AUTH_PATTERNS = ("auth", "unauthorized", "forbidden", "permission", "401", "403", "login")
_NOTEBOOKLM_RATE_LIMIT_PATTERNS = ("rate limit", "too many requests", "quota", "429")
_NOTEBOOKLM_UNAVAILABLE_PATTERNS = (
    "api down",
    "service unavailable",
    "temporarily unavailable",
    "connection refused",
    "connection reset",
    "cdp down",
)


def classify_notebooklm_failure(value: BaseException | str | None) -> str:
    text = _error_text(value)
    if any(pattern in text for pattern in _NOTEBOOKLM_TIMEOUT_PATTERNS):
        return "timeout"
    if any(pattern in text for pattern in _NOTEBOOKLM_RATE_LIMIT_PATTERNS):
        return "rate_limited"
    if any(pattern in text for pattern in _NOTEBOOKLM_AUTH_PATTERNS):
        return "auth"
    if any(pattern in text for pattern in _NOTEBOOKLM_UNAVAILABLE_PATTERNS):
        return "unavailable"
    if "no sources" in text or "0 sources" in text or "empty result" in text:
        return "no_results"
    return "unknown"


def _error_text(value: BaseException | str | None) -> str:
    if value is None:
        return ""
    return str(value).lower()


def _format_fallback_output(value: str | dict[str, Any] | None) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if "summary" in value:
            return _clean_fallback_text(value.get("summary"))
        if "answer" in value:
            return _clean_fallback_text(value.get("answer"))
        if "results" in value and isinstance(value["results"], list):
            lines = []
            for item in value["results"][:3]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("slug") or "resultado")
                snippet = str(item.get("snippet") or item.get("content") or "")
                lines.append(f"- {title}: {_clean_fallback_text(snippet)[:240]}")
            return "\n".join(lines)
        return _clean_fallback_text(value)
    return _clean_fallback_text(value)


def _clean_fallback_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:2000]


class _NoOpContext:
    """Wraps an object so ``async with`` just returns it unchanged."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *exc: Any) -> None:
        pass


class NotebookLMService:
    """Sync wrapper over the async notebooklm-py SDK."""

    def __init__(
        self,
        notify: Callable[[str], None] | None = None,
        observe: Any | None = None,
        job_service: JobService | None = None,
        research_fallback: ResearchFallback | None = None,
        runtime_policy: Any | None = None,
        policy_context: str = "telegram",
        external_backend: Any | None = None,
    ) -> None:
        self._notify = notify or (lambda msg: None)
        self._observe = observe
        self._job_service = job_service
        self._research_fallback = research_fallback
        self._runtime_policy = runtime_policy
        self._policy_context = policy_context
        self._external_backend = external_backend
        self._running: dict[str, threading.Thread] = {}
        self._client_factory: Callable[[], Any] | None = None
        # Optional override for CDP-backed methods (used by tests). Defaults to
        # the real CDP driver in claw_v2.notebooklm_cdp.
        self._cdp_create_notebook_fn: Callable[[str], dict] | None = None
        self._cdp_research_fn: Callable[[str, str], int] | None = None
        self._cdp_artifact_fn: Callable[[str, str], None] | None = None
        self._cdp_orchestrate_step_fn: (
            Callable[[str, dict[str, object], tuple[str, ...]], dict[str, object]] | None
        ) = None
        # Hooks for delivering finished artifacts to the origin chat. Both are
        # overridable in tests; production uses the CDP download + Telegram
        # file sender. _cdp_download_fn(notebook_id, kind) -> path|None.
        self._cdp_download_fn: Callable[[str, str], str | None] | None = None
        # _cdp_report_blocks_fn(notebook_id) -> structured blocks|None (blog is
        # not file-downloadable, so it is scraped and delivered as HTML).
        self._cdp_report_blocks_fn: Callable[[str], list[dict] | None] | None = None
        self._delivery: Any | None = None
        self._sdk_available = self._detect_sdk()

    @staticmethod
    def _detect_sdk() -> bool:
        """Return True if the notebooklm-py SDK is installed and auth state exists."""
        try:
            import notebooklm  # noqa: F401
            import pathlib
            state = pathlib.Path.home() / ".notebooklm" / "storage_state.json"
            return state.exists()
        except ImportError:
            return False

    @property
    def _use_sdk(self) -> bool:
        """True when an SDK client is explicitly injected.

        The production default is the CDP path because NotebookLM SDK auth
        state can exist on the host without being the intended runtime path.
        """
        return self._client_factory is not None

    @property
    def _use_external_backend(self) -> bool:
        """True when an external backend is configured and SDK tests are not overriding it."""
        return self._external_backend is not None and not self._use_sdk

    _ARTIFACT_LABELS = {
        "podcast": "podcast",
        "infographic": "infografia",
        "video": "video",
    }

    def _run_async(self, coro):
        """Run an async coroutine synchronously."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _client_ctx(self, timeout: float = 120):
        """Return an async context manager that yields the SDK client.

        When ``_client_factory`` is set (tests), the factory result is
        wrapped in a no-op context so ``async with`` returns it directly.
        In production, ``NotebookLMClient.from_storage()`` already
        supports ``async with``.
        """
        if self._client_factory is not None:
            return _NoOpContext(self._client_factory())

        @contextlib.asynccontextmanager
        async def _real():
            try:
                from notebooklm import NotebookLMClient
            except ImportError as exc:
                raise NotebookLMSDKUnavailable(
                    "El SDK 'notebooklm' no esta instalado. Esta accion requiere "
                    "instalar el paquete o usar la ruta CDP para esta operacion."
                ) from exc
            async with await NotebookLMClient.from_storage(timeout=timeout) as client:
                yield client

        return _real()

    async def _async_list_notebooks(self) -> list[dict]:
        async with self._client_ctx() as client:
            notebooks = await client.notebooks.list()
            return [
                {
                    "id": nb.id,
                    "title": nb.title,
                    "created_at": str(nb.created_at) if nb.created_at else None,
                }
                for nb in notebooks
            ]

    def list_notebooks(self) -> list[dict]:
        self._enforce_policy("notebooklm.list", {}, mutates_state=False, requires_network=True)
        # Prefer SDK when test factory is injected; otherwise scrape via CDP.
        if self._use_sdk:
            return self._run_async(self._async_list_notebooks())
        if self._use_external_backend:
            try:
                return self._external_backend.list_notebooks()
            except Exception as exc:
                logger.warning("External NotebookLM list failed; falling back to CDP: %s", exc)
        try:
            return notebooklm_cdp.list_notebooks()
        except Exception as exc:
            logger.warning("CDP list_notebooks failed: %s", exc)
            return []

    async def _async_create_notebook(self, title: str) -> dict:
        async with self._client_ctx() as client:
            nb = await client.notebooks.create(title)
            return {"id": nb.id, "title": nb.title}

    def create_notebook(self, title: str) -> dict:
        self._enforce_policy(
            "notebooklm.create",
            {"title": title},
            mutates_state=True,
            requires_network=True,
        )
        # Test mode: an injected SDK client_factory takes precedence so existing
        # SDK-based test mocks keep working. Otherwise use the CDP path
        # (production), which can be overridden via _cdp_create_notebook_fn for
        # CDP-specific tests.
        if self._use_sdk:
            return self._run_async(self._async_create_notebook(title))
        if self._use_external_backend:
            try:
                return self._external_backend.create_notebook(title)
            except Exception as exc:
                logger.warning("External NotebookLM create failed; falling back to CDP: %s", exc)
        cdp_fn = self._cdp_create_notebook_fn or notebooklm_cdp.create_notebook
        return cdp_fn(title)

    async def _async_delete_notebook(self, notebook_id: str) -> bool:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            return await client.notebooks.delete(full_id)

    def delete_notebook(self, notebook_id: str) -> bool:
        self._enforce_policy(
            "notebooklm.delete",
            {"notebook_id": notebook_id},
            mutates_state=True,
            requires_network=True,
        )
        if self._use_external_backend:
            full_id = self._resolve_notebook_id(notebook_id)
            return bool(self._external_backend.delete_notebook(full_id))
        return self._run_async(self._async_delete_notebook(notebook_id))

    async def _async_status(self, notebook_id: str) -> dict:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            nb = await client.notebooks.get(full_id)
            sources = await client.sources.list(full_id)
            return {
                "notebook": {"id": nb.id, "title": nb.title, "sources_count": nb.sources_count},
                "sources": [
                    {"id": s.id, "title": s.title, "kind": s.kind, "url": s.url}
                    for s in sources
                ],
            }

    def status(self, notebook_id: str) -> dict:
        self._enforce_policy("notebooklm.status", {"notebook_id": notebook_id}, mutates_state=False, requires_network=True)
        if self._use_external_backend:
            full_id = self._resolve_notebook_id(notebook_id)
            return self._external_backend.status(full_id)
        return self._run_async(self._async_status(notebook_id))

    async def _async_resolve_notebook_id(self, partial_id: str) -> str:
        notebooks = await self._async_list_notebooks()
        matches = [nb for nb in notebooks if nb["id"].startswith(partial_id)]
        if len(matches) == 0:
            raise ValueError(f"No notebook found matching '{partial_id}'")
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous ID '{partial_id}' matches {len(matches)} notebooks: "
                + ", ".join(m["id"][:12] for m in matches)
            )
        return matches[0]["id"]

    async def _async_add_sources(self, notebook_id: str, urls: list[str]) -> list[dict]:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            results = []
            for url in urls:
                src = await client.sources.add_url(full_id, url)
                results.append({"id": src.id, "title": src.title})
            return results

    def add_sources(self, notebook_id: str, urls: list[str]) -> list[dict]:
        self._enforce_policy(
            "notebooklm.add_sources",
            {"notebook_id": notebook_id, "urls": urls},
            mutates_state=True,
            requires_network=True,
        )
        if self._use_external_backend:
            full_id = self._resolve_notebook_id(notebook_id)
            return self._external_backend.add_sources(full_id, urls)
        return self._run_async(self._async_add_sources(notebook_id, urls))

    async def _async_add_text(self, notebook_id: str, title: str, content: str) -> dict:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            src = await client.sources.add_text(full_id, title, content)
            return {"id": src.id, "title": src.title}

    def add_text(self, notebook_id: str, title: str, content: str) -> dict:
        self._enforce_policy(
            "notebooklm.add_text",
            {"notebook_id": notebook_id, "title": title, "content": content},
            mutates_state=True,
            requires_network=True,
        )
        if self._use_external_backend:
            full_id = self._resolve_notebook_id(notebook_id)
            return self._external_backend.add_text(full_id, title, content)
        return self._run_async(self._async_add_text(notebook_id, title, content))

    async def _async_chat(self, notebook_id: str, question: str) -> str:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            result = await client.chat.ask(full_id, question)
            return result.answer

    def chat(self, notebook_id: str, question: str) -> str:
        self._enforce_policy(
            "notebooklm.chat",
            {"notebook_id": notebook_id, "question": question},
            mutates_state=False,
            requires_network=True,
        )
        if self._use_external_backend:
            full_id = self._resolve_notebook_id(notebook_id)
            return self._external_backend.chat(full_id, question)
        return self._run_async(self._async_chat(notebook_id, question))

    def _resolve_notebook_id(self, partial_id: str) -> str:
        if self._use_external_backend:
            notebooks = self.list_notebooks()
            matches = [nb for nb in notebooks if str(nb.get("id", "")).startswith(partial_id)]
            if len(matches) == 0:
                raise ValueError(f"No notebook found matching '{partial_id}'")
            if len(matches) > 1:
                raise ValueError(
                    f"Ambiguous ID '{partial_id}' matches {len(matches)} notebooks: "
                    + ", ".join(str(m.get("id", ""))[:12] for m in matches)
                )
            return str(matches[0]["id"])
        return self._run_async(self._async_resolve_notebook_id(partial_id))

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._observe is not None:
            self._observe.emit(event_type, payload=payload)

    # --- Background research & podcast ---

    def start_research(self, notebook_id: str, query: str, mode: str = "deep") -> str:
        self._enforce_policy(
            "notebooklm.start_research",
            {"notebook_id": notebook_id, "query": query, "mode": mode},
            mutates_state=True,
            requires_network=True,
        )
        # CDP mode skips SDK-only id resolution and title lookup. The handler
        # passes the full id from create_notebook, and the notify message uses
        # the id as a stand-in title.
        external_mode = self._use_external_backend
        cdp_mode = not self._use_sdk and not external_mode
        if cdp_mode:
            full_id = notebook_id
            title = notebook_id[:8]
        else:
            full_id = self._resolve_notebook_id(notebook_id)
            title = self._get_notebook_title(full_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        job_id: str | None = None
        if self._job_service is not None:
            job = self._job_service.enqueue(
                kind="notebooklm.research",
                payload={
                    "notebook_id": full_id,
                    "query": query,
                    "mode": mode,
                    "cdp_mode": cdp_mode,
                    "external_backend": external_mode,
                },
                metadata={"notebook_title": title},
            )
            job_id = job.job_id
        self._emit("nlm_research_started", notebook_id=full_id, query=query, mode=mode, job_id=job_id)

        def _worker():
            try:
                if self._job_service is not None and job_id is not None:
                    claimed = self._job_service.claim(job_id, worker_id="notebooklm")
                    if claimed is None:
                        self._emit(
                            "nlm_job_skipped",
                            notebook_id=full_id,
                            operation="research",
                            job_id=job_id,
                        )
                        return
                    self._job_service.checkpoint(
                        job_id,
                        {
                            "operation": "research",
                            "notebook_id": full_id,
                            "query": query,
                            "mode": mode,
                        },
                    )
                if cdp_mode:
                    cdp_fn = self._cdp_research_fn or notebooklm_cdp.deep_research
                    result = cdp_fn(full_id, query)
                elif external_mode:
                    try:
                        result = self._external_backend.deep_research(full_id, query, mode=mode)
                    except Exception as exc:
                        logger.warning("External NotebookLM research failed; falling back to CDP: %s", exc)
                        cdp_fn = self._cdp_research_fn or notebooklm_cdp.deep_research
                        result = cdp_fn(full_id, query)
                else:
                    result = self._run_async(self._async_research(full_id, query, mode))
                if result <= 0:
                    fallback = self._try_research_fallback(
                        notebook_id=full_id,
                        query=query,
                        reason="no_results",
                        job_id=job_id,
                    )
                    self._complete_research_job(
                        job_id=job_id,
                        notebook_id=full_id,
                        sources_count=0,
                        degraded=True,
                        failure_kind="no_results",
                        fallback=fallback,
                    )
                    self._emit(
                        "nlm_research_completed",
                        notebook_id=full_id,
                        sources_count=0,
                        degraded=True,
                        failure_kind="no_results",
                        fallback_used=fallback["used"],
                        job_id=job_id,
                    )
                    if fallback["used"]:
                        self._notify(
                            "NotebookLM no importo fuentes; use fallback local.\n"
                            f"Notebook: {title}\n"
                            f"{fallback['summary']}"
                        )
                    else:
                        self._notify(
                            f"Deep Research termino sin fuentes importadas en notebook {title}.\n"
                            "No habia fallback local disponible para cubrir la consulta."
                        )
                    return
                if self._job_service is not None and job_id is not None:
                    self._complete_research_job(
                        job_id=job_id,
                        notebook_id=full_id,
                        sources_count=result,
                    )
                self._emit("nlm_research_completed", notebook_id=full_id, sources_count=result)
                self._notify(
                    f"Deep Research completado en notebook {title}\n"
                    f"{result} fuentes importadas\n"
                    f"https://notebooklm.google.com/notebook/{full_id}"
                )
            except Exception as exc:
                logger.exception("Background research failed for %s", full_id)
                failure_kind = classify_notebooklm_failure(exc)
                fallback = self._try_research_fallback(
                    notebook_id=full_id,
                    query=query,
                    reason=failure_kind,
                    job_id=job_id,
                    error=str(exc),
                )
                if fallback["used"]:
                    self._complete_research_job(
                        job_id=job_id,
                        notebook_id=full_id,
                        sources_count=0,
                        degraded=True,
                        failure_kind=failure_kind,
                        fallback=fallback,
                    )
                    self._emit(
                        "nlm_error",
                        notebook_id=full_id,
                        operation="research",
                        error=str(exc),
                        failure_kind=failure_kind,
                        graceful_fallback=True,
                        job_id=job_id,
                        user_notified=True,
                    )
                    self._notify(
                        "NotebookLM fallo; use fallback local.\n"
                        f"Tipo: {failure_kind}\n"
                        f"Notebook: {title}\n"
                        f"{fallback['summary']}"
                    )
                else:
                    if self._job_service is not None and job_id is not None:
                        try:
                            self._job_service.fail(
                                job_id,
                                error=str(exc),
                                retry=False,
                                checkpoint={"operation": "research", "notebook_id": full_id},
                            )
                        except Exception:
                            logger.exception("Job failure persistence failed for %s", job_id)
                    self._emit(
                        "nlm_error",
                        notebook_id=full_id,
                        operation="research",
                        error=str(exc),
                        failure_kind=failure_kind,
                        graceful_fallback=False,
                        job_id=job_id,
                        user_notified=True,
                    )
                    self._emit(
                        "nlm_research_failed",
                        notebook_id=full_id,
                        operation="research",
                        error=str(exc),
                        failure_kind=failure_kind,
                        job_id=job_id,
                        user_notified=True,
                    )
                    self._notify(f"Error en research ({failure_kind}): {exc}")
            finally:
                self._running.pop(full_id, None)

        thread = threading.Thread(target=_worker, daemon=True)
        self._running[full_id] = thread
        thread.start()
        return f"Deep Research iniciado para '{query}' en notebook {title}..."

    def _complete_research_job(
        self,
        *,
        job_id: str | None,
        notebook_id: str,
        sources_count: int,
        degraded: bool = False,
        failure_kind: str | None = None,
        fallback: dict[str, Any] | None = None,
    ) -> None:
        if self._job_service is None or job_id is None:
            return
        result: dict[str, Any] = {
            "notebook_id": notebook_id,
            "sources_count": sources_count,
        }
        if degraded:
            result.update(
                {
                    "degraded": True,
                    "failure_kind": failure_kind or "unknown",
                    "fallback_used": bool((fallback or {}).get("used")),
                    "fallback_summary": str((fallback or {}).get("summary") or "")[:1000],
                }
            )
        try:
            self._job_service.complete(job_id, result=result)
        except Exception:
            logger.exception("Job completion persistence failed for %s", job_id)

    def _try_research_fallback(
        self,
        *,
        notebook_id: str,
        query: str,
        reason: str,
        job_id: str | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if self._research_fallback is None:
            self._emit(
                "nlm_research_degraded",
                notebook_id=notebook_id,
                query=query,
                reason=reason,
                error=error,
                fallback_used=False,
                job_id=job_id,
                user_notified=True,
            )
            return {"used": False, "summary": "", "error": ""}
        try:
            fallback_output = self._research_fallback(query)
            summary = _format_fallback_output(fallback_output)
            if not summary:
                self._emit(
                    "nlm_research_degraded",
                    notebook_id=notebook_id,
                    query=query,
                    reason=reason,
                    error=error,
                    fallback_used=False,
                    fallback_error="empty_fallback",
                    job_id=job_id,
                    user_notified=True,
                )
                return {"used": False, "summary": "", "error": "empty_fallback"}
            self._emit(
                "nlm_research_degraded",
                notebook_id=notebook_id,
                query=query,
                reason=reason,
                error=error,
                fallback_used=True,
                fallback_summary=summary[:1000],
                job_id=job_id,
                user_notified=True,
            )
            return {"used": True, "summary": summary, "error": ""}
        except Exception as fallback_exc:
            logger.exception("NotebookLM fallback failed for %s", notebook_id)
            self._emit(
                "nlm_research_degraded",
                notebook_id=notebook_id,
                query=query,
                reason=reason,
                error=error,
                fallback_used=False,
                fallback_error=str(fallback_exc),
                job_id=job_id,
                user_notified=True,
            )
            return {"used": False, "summary": "", "error": str(fallback_exc)}

    async def _async_research(self, notebook_id: str, query: str, mode: str) -> int:
        async with self._client_ctx(timeout=1200) as client:
            task = await client.research.start(notebook_id, query, source="web", mode=mode)
            if task is None:
                raise RuntimeError("Research start returned no task")
            task_id = task["task_id"]
            import time as _time
            deadline = _time.monotonic() + 1200
            while _time.monotonic() < deadline:
                poll = await client.research.poll(notebook_id)
                if poll.get("status") == "completed":
                    sources = poll.get("sources", [])
                    if sources:
                        # Retry import up to 3 times — Google API can be slow
                        last_exc: Exception | None = None
                        for attempt in range(3):
                            try:
                                imported = await client.research.import_sources(notebook_id, task_id, sources)
                                return len(imported)
                            except Exception as exc:
                                last_exc = exc
                                logger.warning("import_sources attempt %d failed: %s", attempt + 1, exc)
                                await asyncio.sleep(5 * (attempt + 1))
                        raise last_exc  # type: ignore[misc]
                    return 0
                await asyncio.sleep(15)
            raise TimeoutError("Research did not complete within 20 minutes")

    def start_podcast(self, notebook_id: str) -> str:
        return self.start_artifact(notebook_id, "podcast")

    def start_orchestration(
        self,
        notebook_id: str,
        *,
        session_id: str | None = None,
        outputs: tuple[str, ...] = ("podcast", "blog"),
        poll_interval_seconds: float = 60.0,
    ) -> str:
        """Register a durable NotebookLM orchestration job.

        This is intentionally job-backed only. A caller must not claim that
        NotebookLM is being monitored unless this enqueue succeeds.
        """
        full_id = notebook_id.strip()
        if not full_id:
            raise ValueError("notebook_id is required")
        normalized_outputs = tuple(
            dict.fromkeys(
                output.strip().lower()
                for output in outputs
                if output and output.strip().lower() in {"podcast", "blog", "video"}
            )
        ) or ("podcast", "blog")
        self._enforce_policy(
            "notebooklm.start_artifact",
            {
                "notebook_id": full_id,
                "kind": "orchestrate",
                "outputs": list(normalized_outputs),
            },
            mutates_state=True,
            requires_network=True,
        )
        if self._job_service is None:
            return (
                "No quedó monitor durable registrado: NotebookLM orchestration "
                "requiere JobService activo."
            )
        title = self._get_notebook_title(full_id)
        job = self._job_service.enqueue(
            kind="notebooklm.orchestrate",
            payload={
                "notebook_id": full_id,
                "outputs": list(normalized_outputs),
                "session_id": session_id,
                "poll_interval_seconds": float(poll_interval_seconds),
            },
            resume_key=f"notebooklm:orchestrate:{full_id}",
            metadata={"notebook_title": title, "session_id": session_id},
            max_attempts=120,
        )
        self._emit(
            "nlm_orchestration_registered",
            notebook_id=full_id,
            job_id=job.job_id,
            session_id=session_id,
            outputs=list(normalized_outputs),
        )
        return (
            "Orquestación durable de NotebookLM registrada.\n"
            f"Job: {job.job_id}\n"
            f"Notebook: {title} ({full_id[:8]})\n"
            f"Outputs: {', '.join(normalized_outputs)}"
        )

    def poll_orchestrations(self, *, limit: int = 3) -> int:
        if self._job_service is None:
            return 0
        processed = 0
        for _ in range(max(1, int(limit))):
            job = self._job_service.claim_next(
                worker_id="notebooklm",
                kinds=("notebooklm.orchestrate",),
            )
            if job is None:
                break
            processed += 1
            self._run_orchestration_job(job)
        return processed

    def _run_orchestration_job(self, job: Any) -> None:
        assert self._job_service is not None
        payload = dict(getattr(job, "payload", {}) or {})
        checkpoint = dict(getattr(job, "checkpoint", {}) or {})
        notebook_id = str(payload.get("notebook_id") or "").strip()
        if not notebook_id:
            self._job_service.fail(
                job.job_id,
                error="missing_notebook_id",
                retry=False,
                checkpoint={**checkpoint, "stage": "failed_missing_notebook_id"},
            )
            return
        outputs = tuple(
            str(item).strip().lower()
            for item in payload.get("outputs", [])
            if str(item).strip()
        )
        outputs = tuple(item for item in outputs if item in {"podcast", "blog", "video"}) or ("podcast", "blog")
        step_fn = self._cdp_orchestrate_step_fn
        if step_fn is None:
            def step_fn(
                notebook_id_arg: str,
                checkpoint_arg: dict[str, object],
                outputs_arg: tuple[str, ...],
            ) -> dict[str, object]:
                return notebooklm_cdp.orchestrate_outputs_step(
                    notebook_id_arg,
                    checkpoint_arg,
                    outputs=outputs_arg,
                )
        try:
            result = step_fn(notebook_id, checkpoint, outputs)
        except Exception as exc:
            logger.exception("NotebookLM orchestration step failed for %s", notebook_id)
            self._job_service.fail(
                job.job_id,
                error=str(exc),
                retry=True,
                retry_delay_seconds=float(payload.get("poll_interval_seconds") or 60.0),
                checkpoint={**checkpoint, "stage": "step_error", "error": str(exc)[:300]},
            )
            self._emit(
                "nlm_orchestration_step_error",
                notebook_id=notebook_id,
                job_id=job.job_id,
                error=str(exc),
            )
            return

        status = str(result.get("status") or "pending").lower()
        merged_checkpoint = {
            **checkpoint,
            **dict(result.get("checkpoint") or {}),
            "stage": str(result.get("stage") or checkpoint.get("stage") or "unknown"),
            "last_summary": result.get("summary") or {},
            "updated_at": time.time(),
        }
        if status == "completed":
            session_id = payload.get("session_id")
            delivered = self._deliver_outputs(
                notebook_id, outputs, result, session_id=session_id
            )
            completion_result = {**dict(result), "deliveries": delivered}
            self._job_service.checkpoint(job.job_id, merged_checkpoint)
            self._job_service.complete(job.job_id, result=completion_result)
            self._emit(
                "nlm_orchestration_completed",
                notebook_id=notebook_id,
                job_id=job.job_id,
                evidence_uri=result.get("evidence_uri"),
                deliveries=delivered,
            )
            evidence = str(result.get("evidence_uri") or "").strip()
            suffix = f"\nEvidence: {evidence}" if evidence else ""
            sent = [d for d in delivered if d.get("ok")]
            if sent:
                kinds = ", ".join(sorted({str(d.get("kind")) for d in sent}))
                suffix += f"\nEntregado al chat: {kinds}"
            self._notify(
                "NotebookLM terminó la orquestación durable.\n"
                f"Notebook: {notebook_id[:8]}\n"
                f"Stage: {result.get('stage') or 'outputs_ready'}{suffix}"
            )
            return
        if status == "failed":
            self._job_service.fail(
                job.job_id,
                error=str(result.get("error") or "orchestration_failed"),
                retry=bool(result.get("retry", False)),
                checkpoint=merged_checkpoint,
            )
            self._emit(
                "nlm_orchestration_failed",
                notebook_id=notebook_id,
                job_id=job.job_id,
                stage=merged_checkpoint.get("stage"),
            )
            return

        delay = float(
            result.get("next_delay_seconds") or payload.get("poll_interval_seconds") or 60.0
        )
        self._job_service.reschedule(
            job.job_id,
            checkpoint=merged_checkpoint,
            result={"last_status": status, "stage": merged_checkpoint.get("stage")},
            next_run_at=time.time() + max(1.0, delay),
        )
        self._emit(
            "nlm_orchestration_pending",
            notebook_id=notebook_id,
            job_id=job.job_id,
            stage=merged_checkpoint.get("stage"),
            next_delay_seconds=delay,
        )

    @staticmethod
    def _chat_id_from_session(session_id: str | None) -> str | None:
        if not session_id:
            return None
        text = str(session_id).strip()
        if text.startswith("tg-"):
            text = text[3:]
        return text or None

    def _get_delivery(self) -> Any:
        if self._delivery is None:
            from claw_v2.notebooklm_delivery import NotebookLMDeliveryService

            self._delivery = NotebookLMDeliveryService()
        return self._delivery

    _OUTPUT_DELIVERY_MAP = {
        "podcast": ("audio", "audio_ready", "Resumen en audio (NotebookLM)"),
        "blog": ("blog", "blog_ready", "Informe (NotebookLM)"),
        "video": ("video", "video_ready", "Resumen en video (NotebookLM)"),
    }

    def _obtain_report_path(self, notebook_id: str) -> str | None:
        """Scrape the report blocks and persist them as a self-contained HTML file.

        Reports are delivered as HTML (not markdown): HTML renders accents and
        the comparison table correctly regardless of the viewer's encoding
        guess, which the markdown text preview got wrong.
        """
        if self._cdp_report_blocks_fn is not None:
            items = self._cdp_report_blocks_fn(notebook_id)
        else:
            from claw_v2 import notebooklm_cdp

            items = notebooklm_cdp.extract_report_blocks(notebook_id)
        if not items:
            return None
        from pathlib import Path as _Path

        from claw_v2.notebooklm_delivery import render_report_html

        meta = f"Informe NotebookLM · {time.strftime('%d %b %Y')}"
        _title, doc = render_report_html(items, meta=meta)
        safe_id = "".join(c for c in notebook_id if c.isalnum() or c in "-_")[:12] or "notebook"
        out_dir = _Path("artifacts/notebooklm")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"informe_{safe_id}_{int(time.time())}.html"
        path.write_text(doc, encoding="utf-8")
        return str(path)

    def _deliver_outputs(
        self,
        notebook_id: str,
        outputs: tuple[str, ...],
        result: dict[str, Any],
        *,
        session_id: str | None,
    ) -> list[dict[str, Any]]:
        """Download each ready artifact and push it to the origin chat.

        Best-effort: any download/send failure is recorded and skipped so the
        orchestration still completes and the text notify still fires.
        """
        summary = dict(result.get("summary") or {})
        chat_id = self._chat_id_from_session(session_id)
        download_fn = self._cdp_download_fn
        deliveries: list[dict[str, Any]] = []
        for kind in outputs:
            mapped = self._OUTPUT_DELIVERY_MAP.get(kind)
            if mapped is None:
                continue
            target, ready_flag, caption = mapped
            if not summary.get(ready_flag):
                continue
            try:
                if target == "blog":
                    path = self._obtain_report_path(notebook_id)
                elif download_fn is not None:
                    path = download_fn(notebook_id, target)
                else:
                    from claw_v2 import notebooklm_cdp

                    path = notebooklm_cdp.download_ready_artifact(notebook_id, target)
            except Exception as exc:
                logger.warning("NotebookLM %s fetch failed for %s: %s", target, notebook_id, exc)
                deliveries.append({"kind": kind, "ok": False, "error": f"fetch:{exc}"[:200]})
                continue
            if not path:
                deliveries.append({"kind": kind, "ok": False, "error": "no_artifact_path"})
                continue
            try:
                from pathlib import Path as _Path

                sent = self._get_delivery().send_to_telegram(
                    _Path(path), chat_id=chat_id, caption=caption,
                )
                record = sent.to_dict()
                record["kind"] = kind
                deliveries.append(record)
            except Exception as exc:
                logger.warning("NotebookLM %s delivery failed for %s: %s", target, notebook_id, exc)
                deliveries.append({"kind": kind, "ok": False, "error": f"send:{exc}"[:200]})
        return deliveries

    def start_artifact(self, notebook_id: str, kind: str) -> str:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in self._ARTIFACT_LABELS:
            raise ValueError(f"Tipo de artefacto no soportado: {kind}")
        self._enforce_policy(
            "notebooklm.start_artifact",
            {"notebook_id": notebook_id, "kind": normalized_kind},
            mutates_state=True,
            requires_network=True,
        )
        external_mode = self._use_external_backend
        cdp_mode = not self._use_sdk and not external_mode
        if cdp_mode:
            full_id = notebook_id
            title = notebook_id[:8]
        else:
            full_id = self._resolve_notebook_id(notebook_id)
            if not external_mode:
                self._ensure_artifact_supported(normalized_kind)
            title = self._get_notebook_title(full_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        job_id: str | None = None
        if self._job_service is not None:
            job = self._job_service.enqueue(
                kind=f"notebooklm.{normalized_kind}",
                payload={
                    "notebook_id": full_id,
                    "artifact_kind": normalized_kind,
                    "cdp_mode": cdp_mode,
                    "external_backend": external_mode,
                },
                metadata={"notebook_title": title},
            )
            job_id = job.job_id
        self._emit(f"nlm_{normalized_kind}_started", notebook_id=full_id, job_id=job_id)

        def _worker():
            try:
                if self._job_service is not None and job_id is not None:
                    claimed = self._job_service.claim(job_id, worker_id="notebooklm")
                    if claimed is None:
                        self._emit(
                            "nlm_job_skipped",
                            notebook_id=full_id,
                            operation=normalized_kind,
                            job_id=job_id,
                        )
                        return
                    self._job_service.checkpoint(
                        job_id,
                        {
                            "operation": normalized_kind,
                            "notebook_id": full_id,
                            "artifact_kind": normalized_kind,
                        },
                    )
                if cdp_mode:
                    cdp_fn = self._cdp_artifact_fn or notebooklm_cdp.generate_artifact
                    cdp_fn(full_id, normalized_kind)
                elif external_mode:
                    try:
                        self._external_backend.generate_artifact(full_id, normalized_kind)
                    except Exception as exc:
                        logger.warning("External NotebookLM artifact failed; falling back to CDP: %s", exc)
                        cdp_fn = self._cdp_artifact_fn or notebooklm_cdp.generate_artifact
                        cdp_fn(full_id, normalized_kind)
                else:
                    self._run_async(self._async_generate_artifact(full_id, normalized_kind))
                if self._job_service is not None and job_id is not None:
                    try:
                        self._job_service.complete(
                            job_id,
                            result={"notebook_id": full_id, "artifact_kind": normalized_kind},
                        )
                    except Exception:
                        logger.exception("Job completion persistence failed for %s", job_id)
                self._emit(f"nlm_{normalized_kind}_completed", notebook_id=full_id)
                self._notify(
                    f"{self._artifact_name(normalized_kind)} generado para notebook {title}\n"
                    f"https://notebooklm.google.com/notebook/{full_id}"
                )
            except Exception as exc:
                logger.exception("Background %s failed for %s", normalized_kind, full_id)
                if self._job_service is not None and job_id is not None:
                    try:
                        self._job_service.fail(
                            job_id,
                            error=str(exc),
                            retry=False,
                            checkpoint={"operation": normalized_kind, "notebook_id": full_id},
                        )
                    except Exception:
                        logger.exception("Job failure persistence failed for %s", job_id)
                self._emit("nlm_error", notebook_id=full_id, operation=normalized_kind, error=str(exc))
                self._notify(f"Error en {self._artifact_name(normalized_kind)}: {exc}")
            finally:
                self._running.pop(full_id, None)

        thread = threading.Thread(target=_worker, daemon=True)
        self._running[full_id] = thread
        thread.start()
        return f"Generando {self._artifact_name(normalized_kind)} para notebook {title}..."

    async def _async_generate_artifact(self, notebook_id: str, kind: str) -> None:
        async with self._client_ctx() as client:
            method_name = self._artifact_method_name(client.artifacts, kind)
            generator = getattr(client.artifacts, method_name)
            status = await generator(notebook_id)
            task_id = self._artifact_task_id(status)
            waiter = getattr(client.artifacts, "wait_for_completion", None)
            if waiter is not None and task_id:
                await waiter(notebook_id, task_id, timeout=1200)

    def _ensure_artifact_supported(self, kind: str) -> None:
        self._run_async(self._async_ensure_artifact_supported(kind))

    async def _async_ensure_artifact_supported(self, kind: str) -> None:
        async with self._client_ctx() as client:
            self._artifact_method_name(client.artifacts, kind)

    def _artifact_method_name(self, artifacts: Any, kind: str) -> str:
        candidates = {
            "podcast": ("generate_audio",),
            "infographic": ("generate_infographic", "generate_infographic_overview"),
            "video": ("generate_video", "generate_video_overview"),
        }[kind]
        for name in candidates:
            if hasattr(artifacts, name):
                return name
        raise RuntimeError(
            f"Este runtime de NotebookLM no soporta generar {self._artifact_name(kind)}."
        )

    def _artifact_task_id(self, status: Any) -> str | None:
        if isinstance(status, dict):
            task_id = status.get("task_id")
            return str(task_id) if task_id else None
        task_id = getattr(status, "task_id", None)
        return str(task_id) if task_id else None

    def _artifact_name(self, kind: str) -> str:
        return self._ARTIFACT_LABELS[kind]

    def _get_notebook_title(self, full_id: str) -> str:
        try:
            notebooks = self.list_notebooks()
            for nb in notebooks:
                if nb["id"] == full_id:
                    return nb["title"]
        except Exception:
            pass
        return full_id[:8]

    def _enforce_policy(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        mutates_state: bool,
        requires_network: bool,
    ) -> None:
        if self._runtime_policy is None:
            return
        self._runtime_policy.enforce(
            tool_name,
            args,
            context=self._policy_context,
            mutates_state=mutates_state,
            requires_network=requires_network,
        )
