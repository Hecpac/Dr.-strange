from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any, Callable

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

    def _run_async(self, coro):
        """Run an async coroutine synchronously."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _client_ctx(self):
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
            async with await NotebookLMClient.from_storage() as client:
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
        return self._run_async(self._async_create_notebook(title))

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

    def _resolve_notebook_id(self, partial_id: str) -> str:
        return self._run_async(self._async_resolve_notebook_id(partial_id))

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._observe is not None:
            self._observe.emit(event_type, payload=payload)
