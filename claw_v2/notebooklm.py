from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any, Callable

from claw_v2 import notebooklm_cdp

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._notify = notify or (lambda msg: None)
        self._observe = observe
        self._running: dict[str, threading.Thread] = {}
        self._client_factory: Callable[[], Any] | None = None
        # Optional override for CDP-backed methods (used by tests). Defaults to
        # the real CDP driver in claw_v2.notebooklm_cdp.
        self._cdp_create_notebook_fn: Callable[[str], dict] | None = None
        self._cdp_research_fn: Callable[[str, str], int] | None = None
        self._cdp_artifact_fn: Callable[[str, str], None] | None = None
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
        """True when SDK should be used (either injected factory or auto-detected)."""
        return self._client_factory is not None or self._sdk_available

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
            from notebooklm import NotebookLMClient
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
        return self._run_async(self._async_list_notebooks())

    async def _async_create_notebook(self, title: str) -> dict:
        async with self._client_ctx() as client:
            nb = await client.notebooks.create(title)
            return {"id": nb.id, "title": nb.title}

    def create_notebook(self, title: str) -> dict:
        # Test mode: an injected SDK client_factory takes precedence so existing
        # SDK-based test mocks keep working. Otherwise use the CDP path
        # (production), which can be overridden via _cdp_create_notebook_fn for
        # CDP-specific tests.
        if self._use_sdk:
            return self._run_async(self._async_create_notebook(title))
        cdp_fn = self._cdp_create_notebook_fn or notebooklm_cdp.create_notebook
        return cdp_fn(title)

    async def _async_delete_notebook(self, notebook_id: str) -> bool:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            return await client.notebooks.delete(full_id)

    def delete_notebook(self, notebook_id: str) -> bool:
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
        return self._run_async(self._async_add_sources(notebook_id, urls))

    async def _async_add_text(self, notebook_id: str, title: str, content: str) -> dict:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            src = await client.sources.add_text(full_id, title, content)
            return {"id": src.id, "title": src.title}

    def add_text(self, notebook_id: str, title: str, content: str) -> dict:
        return self._run_async(self._async_add_text(notebook_id, title, content))

    async def _async_chat(self, notebook_id: str, question: str) -> str:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with self._client_ctx() as client:
            result = await client.chat.ask(full_id, question)
            return result.answer

    def chat(self, notebook_id: str, question: str) -> str:
        return self._run_async(self._async_chat(notebook_id, question))

    def _resolve_notebook_id(self, partial_id: str) -> str:
        return self._run_async(self._async_resolve_notebook_id(partial_id))

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._observe is not None:
            self._observe.emit(event_type, payload=payload)

    # --- Background research & podcast ---

    def start_research(self, notebook_id: str, query: str, mode: str = "deep") -> str:
        # CDP mode skips SDK-only id resolution and title lookup. The handler
        # passes the full id from create_notebook, and the notify message uses
        # the id as a stand-in title.
        cdp_mode = not self._use_sdk
        if cdp_mode:
            full_id = notebook_id
            title = notebook_id[:8]
        else:
            full_id = self._resolve_notebook_id(notebook_id)
            title = self._get_notebook_title(full_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        self._emit("nlm_research_started", notebook_id=full_id, query=query, mode=mode)

        def _worker():
            try:
                if cdp_mode:
                    cdp_fn = self._cdp_research_fn or notebooklm_cdp.deep_research
                    result = cdp_fn(full_id, query)
                else:
                    result = self._run_async(self._async_research(full_id, query, mode))
                self._emit("nlm_research_completed", notebook_id=full_id, sources_count=result)
                self._notify(
                    f"Deep Research completado en notebook {title}\n"
                    f"{result} fuentes importadas\n"
                    f"https://notebooklm.google.com/notebook/{full_id}"
                )
            except Exception as exc:
                logger.exception("Background research failed for %s", full_id)
                self._emit("nlm_error", notebook_id=full_id, operation="research", error=str(exc))
                self._notify(f"Error en research: {exc}")
            finally:
                self._running.pop(full_id, None)

        thread = threading.Thread(target=_worker, daemon=True)
        self._running[full_id] = thread
        thread.start()
        return f"Deep Research iniciado para '{query}' en notebook {title}..."

    async def _async_research(self, notebook_id: str, query: str, mode: str) -> int:
        async with self._client_ctx(timeout=300) as client:
            task = await client.research.start(notebook_id, query, source="web", mode=mode)
            if task is None:
                raise RuntimeError("Research start returned no task")
            task_id = task["task_id"]
            import time as _time
            deadline = _time.monotonic() + 600
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
            raise TimeoutError("Research did not complete within 10 minutes")

    def start_podcast(self, notebook_id: str) -> str:
        return self.start_artifact(notebook_id, "podcast")

    def start_artifact(self, notebook_id: str, kind: str) -> str:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in self._ARTIFACT_LABELS:
            raise ValueError(f"Tipo de artefacto no soportado: {kind}")
        cdp_mode = not self._use_sdk
        if cdp_mode:
            full_id = notebook_id
            title = notebook_id[:8]
        else:
            full_id = self._resolve_notebook_id(notebook_id)
            self._ensure_artifact_supported(normalized_kind)
            title = self._get_notebook_title(full_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        self._emit(f"nlm_{normalized_kind}_started", notebook_id=full_id)

        def _worker():
            try:
                if cdp_mode:
                    cdp_fn = self._cdp_artifact_fn or notebooklm_cdp.generate_artifact
                    cdp_fn(full_id, normalized_kind)
                else:
                    self._run_async(self._async_generate_artifact(full_id, normalized_kind))
                self._emit(f"nlm_{normalized_kind}_completed", notebook_id=full_id)
                self._notify(
                    f"{self._artifact_name(normalized_kind)} generado para notebook {title}\n"
                    f"https://notebooklm.google.com/notebook/{full_id}"
                )
            except Exception as exc:
                logger.exception("Background %s failed for %s", normalized_kind, full_id)
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
