# NotebookLM Bot Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claw `/nlm_*` Telegram commands to create notebooks, add sources, run Deep Research, generate podcasts, and chat — all via the `notebooklm-py` SDK.

**Architecture:** New `NotebookLMService` class in `claw_v2/notebooklm.py` wraps the async SDK with sync methods for the bot. Long operations (research, podcast) run in daemon threads and notify via Telegram callback. Bot commands in `bot.py` delegate to the service. Wiring in `lifecycle.py` where the transport is available for the notify callback.

**Tech Stack:** `notebooklm-py` SDK (already installed), `asyncio`, `threading`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `claw_v2/notebooklm.py` | Create | `NotebookLMService` — sync wrapper over async SDK, background threading, partial ID resolution |
| `tests/test_notebooklm.py` | Create | Unit tests for the service (mocked SDK) |
| `claw_v2/bot.py` | Modify | Add `/nlm_*` command handlers + `notebooklm` attribute |
| `claw_v2/lifecycle.py` | Modify | Wire `NotebookLMService` with transport notify callback |
| `claw_v2/telegram.py` | Modify | Register `/nlm_*` in bot commands menu |

---

### Task 1: NotebookLMService — sync core methods

**Files:**
- Create: `claw_v2/notebooklm.py`
- Create: `tests/test_notebooklm.py`

- [ ] **Step 1: Write failing tests for list, create, delete, status**

```python
# tests/test_notebooklm.py
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.notebooklm import NotebookLMService


def _mock_notebook(notebook_id: str = "abc123-def456", title: str = "Test NB"):
    nb = MagicMock()
    nb.id = notebook_id
    nb.title = title
    nb.created_at = None
    nb.sources_count = 0
    return nb


class SyncMethodTests(unittest.TestCase):
    def _make_service(self, client_mock: AsyncMock) -> NotebookLMService:
        notify = MagicMock()
        svc = NotebookLMService(notify=notify)
        svc._client_factory = lambda: client_mock
        return svc

    def test_list_notebooks(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook(), _mock_notebook("xyz789", "Second")]
        svc = self._make_service(client)
        result = svc.list_notebooks()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "abc123-def456")
        self.assertEqual(result[1]["title"], "Second")

    def test_create_notebook(self) -> None:
        client = AsyncMock()
        client.notebooks.create.return_value = _mock_notebook("new-id", "My Notebook")
        svc = self._make_service(client)
        result = svc.create_notebook("My Notebook")
        self.assertEqual(result["id"], "new-id")
        self.assertEqual(result["title"], "My Notebook")
        client.notebooks.create.assert_awaited_once_with("My Notebook")

    def test_delete_notebook(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc123-full-id", "NB")]
        client.notebooks.delete.return_value = True
        svc = self._make_service(client)
        result = svc.delete_notebook("abc123")
        self.assertTrue(result)
        client.notebooks.delete.assert_awaited_once_with("abc123-full-id")

    def test_partial_id_match(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [
            _mock_notebook("abc123-full", "First"),
            _mock_notebook("xyz789-full", "Second"),
        ]
        svc = self._make_service(client)
        resolved = svc._resolve_notebook_id("abc1")
        self.assertEqual(resolved, "abc123-full")

    def test_partial_id_no_match(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc123-full", "First")]
        svc = self._make_service(client)
        with self.assertRaises(ValueError):
            svc._resolve_notebook_id("zzz")

    def test_partial_id_ambiguous(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [
            _mock_notebook("abc123-one", "First"),
            _mock_notebook("abc123-two", "Second"),
        ]
        svc = self._make_service(client)
        with self.assertRaises(ValueError):
            svc._resolve_notebook_id("abc123")

    def test_status(self) -> None:
        client = AsyncMock()
        nb = _mock_notebook("abc-full", "NB")
        nb.sources_count = 3
        client.notebooks.list.return_value = [nb]
        client.notebooks.get.return_value = nb
        source = MagicMock()
        source.id = "src-1"
        source.title = "Source 1"
        source.kind = "web_page"
        source.url = "https://example.com"
        client.sources.list.return_value = [source]
        svc = self._make_service(client)
        result = svc.status("abc")
        self.assertEqual(result["notebook"]["id"], "abc-full")
        self.assertEqual(len(result["sources"]), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notebooklm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.notebooklm'`

- [ ] **Step 3: Implement NotebookLMService core**

```python
# claw_v2/notebooklm.py
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


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

    async def _get_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from notebooklm import NotebookLMClient
        return await NotebookLMClient.from_storage()

    async def _async_list_notebooks(self) -> list[dict]:
        async with await self._get_client() as client:
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
        async with await self._get_client() as client:
            nb = await client.notebooks.create(title)
            return {"id": nb.id, "title": nb.title}

    def create_notebook(self, title: str) -> dict:
        return self._run_async(self._async_create_notebook(title))

    async def _async_delete_notebook(self, notebook_id: str) -> bool:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with await self._get_client() as client:
            return await client.notebooks.delete(full_id)

    def delete_notebook(self, notebook_id: str) -> bool:
        return self._run_async(self._async_delete_notebook(notebook_id))

    async def _async_status(self, notebook_id: str) -> dict:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with await self._get_client() as client:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_notebooklm.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/notebooklm.py tests/test_notebooklm.py
git commit -m "feat(nlm): add NotebookLMService with list, create, delete, status"
```

---

### Task 2: NotebookLMService — sources and chat methods

**Files:**
- Modify: `claw_v2/notebooklm.py`
- Modify: `tests/test_notebooklm.py`

- [ ] **Step 1: Write failing tests for add_sources, add_text, chat**

Add to `tests/test_notebooklm.py` inside the `SyncMethodTests` class:

```python
    def test_add_sources(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "NB")]
        src1 = MagicMock()
        src1.id = "s1"
        src1.title = "Page 1"
        src2 = MagicMock()
        src2.id = "s2"
        src2.title = "Page 2"
        client.sources.add_url.side_effect = [src1, src2]
        svc = self._make_service(client)
        result = svc.add_sources("abc", ["https://a.com", "https://b.com"])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "s1")
        self.assertEqual(client.sources.add_url.await_count, 2)

    def test_add_text(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "NB")]
        src = MagicMock()
        src.id = "st1"
        src.title = "My Text"
        client.sources.add_text.return_value = src
        svc = self._make_service(client)
        result = svc.add_text("abc", "My Text", "Some content here")
        self.assertEqual(result["id"], "st1")
        client.sources.add_text.assert_awaited_once_with("abc-full", "My Text", "Some content here")

    def test_chat(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "NB")]
        chat_result = MagicMock()
        chat_result.text = "Here is the summary of your sources."
        chat_result.citations = []
        client.chat.ask.return_value = chat_result
        svc = self._make_service(client)
        result = svc.chat("abc", "resume las fuentes")
        self.assertEqual(result, "Here is the summary of your sources.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notebooklm.py::SyncMethodTests::test_add_sources tests/test_notebooklm.py::SyncMethodTests::test_add_text tests/test_notebooklm.py::SyncMethodTests::test_chat -v`
Expected: FAIL — `AttributeError: 'NotebookLMService' object has no attribute 'add_sources'`

- [ ] **Step 3: Implement add_sources, add_text, chat**

Add to `claw_v2/notebooklm.py` inside `NotebookLMService`:

```python
    async def _async_add_sources(self, notebook_id: str, urls: list[str]) -> list[dict]:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with await self._get_client() as client:
            results = []
            for url in urls:
                src = await client.sources.add_url(full_id, url)
                results.append({"id": src.id, "title": src.title})
            return results

    def add_sources(self, notebook_id: str, urls: list[str]) -> list[dict]:
        return self._run_async(self._async_add_sources(notebook_id, urls))

    async def _async_add_text(self, notebook_id: str, title: str, content: str) -> dict:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with await self._get_client() as client:
            src = await client.sources.add_text(full_id, title, content)
            return {"id": src.id, "title": src.title}

    def add_text(self, notebook_id: str, title: str, content: str) -> dict:
        return self._run_async(self._async_add_text(notebook_id, title, content))

    async def _async_chat(self, notebook_id: str, question: str) -> str:
        full_id = await self._async_resolve_notebook_id(notebook_id)
        async with await self._get_client() as client:
            result = await client.chat.ask(full_id, question)
            return result.text

    def chat(self, notebook_id: str, question: str) -> str:
        return self._run_async(self._async_chat(notebook_id, question))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_notebooklm.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/notebooklm.py tests/test_notebooklm.py
git commit -m "feat(nlm): add sources, text, and chat methods to NotebookLMService"
```

---

### Task 3: NotebookLMService — background research and podcast

**Files:**
- Modify: `claw_v2/notebooklm.py`
- Modify: `tests/test_notebooklm.py`

- [ ] **Step 1: Write failing tests for background operations**

Add a new test class to `tests/test_notebooklm.py`:

```python
import time


class BackgroundTests(unittest.TestCase):
    def _make_service(self, client_mock: AsyncMock) -> NotebookLMService:
        notify = MagicMock()
        svc = NotebookLMService(notify=notify)
        svc._client_factory = lambda: client_mock
        return svc

    def test_research_starts_thread_and_notifies(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "Research NB")]
        client.research.start.return_value = {"task_id": "t1", "report_id": "r1"}
        client.research.poll.return_value = {
            "status": "completed",
            "sources": [{"url": "https://a.com", "title": "A", "result_type": 1}],
            "task_id": "t1",
        }
        client.research.import_sources.return_value = [{"id": "s1", "title": "A"}]
        svc = self._make_service(client)
        result = svc.start_research("abc", "AI trends")
        self.assertIn("Deep Research iniciado", result)
        # Wait for background thread to complete
        for _ in range(50):
            if "abc-full" not in svc._running:
                break
            time.sleep(0.1)
        svc._notify.assert_called_once()
        call_msg = svc._notify.call_args[0][0]
        self.assertIn("completado", call_msg.lower())

    def test_podcast_notifies_on_completion(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "Pod NB")]
        status_obj = MagicMock()
        status_obj.task_id = "task-1"
        client.artifacts.generate_audio.return_value = status_obj
        client.artifacts.wait_for_completion.return_value = MagicMock()
        svc = self._make_service(client)
        result = svc.start_podcast("abc")
        self.assertIn("Generando podcast", result)
        for _ in range(50):
            if "abc-full" not in svc._running:
                break
            time.sleep(0.1)
        svc._notify.assert_called_once()
        call_msg = svc._notify.call_args[0][0]
        self.assertIn("podcast", call_msg.lower())

    def test_background_error_notifies(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "Err NB")]
        client.research.start.side_effect = RuntimeError("API down")
        svc = self._make_service(client)
        result = svc.start_research("abc", "query")
        self.assertIn("Deep Research iniciado", result)
        for _ in range(50):
            if "abc-full" not in svc._running:
                break
            time.sleep(0.1)
        svc._notify.assert_called_once()
        call_msg = svc._notify.call_args[0][0]
        self.assertIn("error", call_msg.lower())

    def test_one_operation_per_notebook(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "NB")]
        # Make research hang
        async def slow_start(*a, **kw):
            await asyncio.sleep(10)
            return {"task_id": "t1"}
        client.research.start = slow_start
        svc = self._make_service(client)
        svc.start_research("abc", "query 1")
        result = svc.start_research("abc", "query 2")
        self.assertIn("ya hay una operación", result.lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notebooklm.py::BackgroundTests -v`
Expected: FAIL — `AttributeError: 'NotebookLMService' object has no attribute 'start_research'`

- [ ] **Step 3: Implement background research and podcast**

Add to `claw_v2/notebooklm.py` inside `NotebookLMService`:

```python
    def start_research(self, notebook_id: str, query: str, mode: str = "deep") -> str:
        full_id = self._resolve_notebook_id(notebook_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        title = self._get_notebook_title(full_id)
        self._emit("nlm_research_started", notebook_id=full_id, query=query, mode=mode)

        def _worker():
            try:
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
        async with await self._get_client() as client:
            task = await client.research.start(notebook_id, query, source="web", mode=mode)
            if task is None:
                raise RuntimeError("Research start returned no task")
            task_id = task["task_id"]
            # Poll until completed (max ~10 min)
            import time as _time
            deadline = _time.monotonic() + 600
            while _time.monotonic() < deadline:
                poll = await client.research.poll(notebook_id)
                if poll.get("status") == "completed":
                    sources = poll.get("sources", [])
                    if sources:
                        imported = await client.research.import_sources(notebook_id, task_id, sources)
                        return len(imported)
                    return 0
                await asyncio.sleep(15)
            raise TimeoutError("Research did not complete within 10 minutes")

    def start_podcast(self, notebook_id: str) -> str:
        full_id = self._resolve_notebook_id(notebook_id)
        if full_id in self._running and self._running[full_id].is_alive():
            return f"Ya hay una operación en curso para este notebook ({full_id[:8]}...)."
        title = self._get_notebook_title(full_id)
        self._emit("nlm_podcast_started", notebook_id=full_id)

        def _worker():
            try:
                self._run_async(self._async_podcast(full_id))
                self._emit("nlm_podcast_completed", notebook_id=full_id)
                self._notify(
                    f"Podcast generado para notebook {title}\n"
                    f"https://notebooklm.google.com/notebook/{full_id}"
                )
            except Exception as exc:
                logger.exception("Background podcast failed for %s", full_id)
                self._emit("nlm_error", notebook_id=full_id, operation="podcast", error=str(exc))
                self._notify(f"Error en podcast: {exc}")
            finally:
                self._running.pop(full_id, None)

        thread = threading.Thread(target=_worker, daemon=True)
        self._running[full_id] = thread
        thread.start()
        return f"Generando podcast para notebook {title}..."

    async def _async_podcast(self, notebook_id: str) -> None:
        async with await self._get_client() as client:
            status = await client.artifacts.generate_audio(notebook_id)
            await client.artifacts.wait_for_completion(notebook_id, status.task_id, timeout=1200)

    def _get_notebook_title(self, full_id: str) -> str:
        try:
            notebooks = self.list_notebooks()
            for nb in notebooks:
                if nb["id"] == full_id:
                    return nb["title"]
        except Exception:
            pass
        return full_id[:8]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_notebooklm.py -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/notebooklm.py tests/test_notebooklm.py
git commit -m "feat(nlm): add background research and podcast with notification"
```

---

### Task 4: Bot commands — `/nlm_*` handlers

**Files:**
- Modify: `claw_v2/bot.py:86-131` (add attribute to `__init__`)
- Modify: `claw_v2/bot.py:207-217` (add command handlers in `handle_text`)
- Modify: `claw_v2/bot.py` (add `_nlm_*` response methods)

- [ ] **Step 1: Write failing test for bot nlm commands**

Add to `tests/test_notebooklm.py`:

```python
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

from tests.helpers import make_config


def _make_bot_with_nlm(nlm_service: NotebookLMService) -> "BotService":
    """Create a minimal BotService with a NotebookLMService attached."""
    from claw_v2.bot import BotService

    with tempfile.TemporaryDirectory() as tmpdir:
        config = make_config(Path(tmpdir))
        brain = MagicMock()
        brain.handle_message.return_value = MagicMock(content="brain response")
        bot = BotService(
            brain=brain,
            auto_research=MagicMock(),
            heartbeat=MagicMock(),
            approvals=MagicMock(),
            allowed_user_id="123",
            config=config,
        )
        bot.notebooklm = nlm_service
        return bot


class BotCommandTests(unittest.TestCase):
    def test_nlm_list(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.list_notebooks.return_value = [
            {"id": "abc123", "title": "Test NB", "created_at": "2026-04-02"},
        ]
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_list")
        self.assertIn("abc123", result)
        self.assertIn("Test NB", result)

    def test_nlm_create(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "new-id", "title": "Noticias AI"}
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_create Noticias AI")
        nlm.create_notebook.assert_called_once_with("Noticias AI")
        self.assertIn("new-id", result)

    def test_nlm_research(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.start_research.return_value = "Deep Research iniciado..."
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_research abc AI trends April")
        nlm.start_research.assert_called_once_with("abc", "AI trends April")
        self.assertIn("Deep Research iniciado", result)

    def test_nlm_not_configured(self) -> None:
        from claw_v2.bot import BotService
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            brain = MagicMock()
            brain.handle_message.return_value = MagicMock(content="brain response")
            bot = BotService(
                brain=brain,
                auto_research=MagicMock(),
                heartbeat=MagicMock(),
                approvals=MagicMock(),
                allowed_user_id="123",
                config=config,
            )
            # notebooklm not set
            result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_list")
            self.assertIn("no disponible", result.lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_notebooklm.py::BotCommandTests -v`
Expected: FAIL — `/nlm_list` goes to brain instead of nlm handler

- [ ] **Step 3: Add `notebooklm` attribute to BotService.__init__**

In `claw_v2/bot.py`, add to `__init__` signature after line 108 (`observe: object | None = None,`):

```python
        self.notebooklm: Any | None = None
```

- [ ] **Step 4: Add `/nlm_*` command routing in handle_text**

In `claw_v2/bot.py`, add the following block in `handle_text` before the line `return self.brain.handle_message(session_id, stripped).content` (line 462). Insert after the social commands block (~line 461):

```python
        # --- NotebookLM commands ---
        if stripped.startswith("/nlm_"):
            return self._nlm_dispatch(stripped)
```

- [ ] **Step 5: Add `_nlm_dispatch` and response methods**

Add at the end of `BotService` class in `claw_v2/bot.py`:

```python
    def _nlm_dispatch(self, command: str) -> str:
        if self.notebooklm is None:
            return "NotebookLM no disponible. El servicio no está configurado."
        try:
            if command == "/nlm_list":
                return self._nlm_list_response()
            if command.startswith("/nlm_create "):
                title = command.split(maxsplit=1)[1]
                return self._nlm_create_response(title)
            if command == "/nlm_create":
                return "usage: /nlm_create <titulo>"
            if command.startswith("/nlm_delete "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_delete_response(nb_id)
            if command == "/nlm_delete":
                return "usage: /nlm_delete <notebook_id>"
            if command.startswith("/nlm_status "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_status_response(nb_id)
            if command == "/nlm_status":
                return "usage: /nlm_status <notebook_id>"
            if command.startswith("/nlm_sources "):
                parts = command.split()
                if len(parts) < 3:
                    return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
                return self._nlm_sources_response(parts[1], parts[2:])
            if command == "/nlm_sources":
                return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
            if command.startswith("/nlm_text "):
                rest = command.split(maxsplit=2)
                if len(rest) < 3 or "|" not in rest[2]:
                    return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
                nb_id = rest[1]
                title_and_content = rest[2]
                pipe_idx = title_and_content.index("|")
                title = title_and_content[:pipe_idx].strip()
                content = title_and_content[pipe_idx + 1:].strip()
                return self._nlm_text_response(nb_id, title, content)
            if command == "/nlm_text":
                return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
            if command.startswith("/nlm_research "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_research <notebook_id> <query>"
                return self._nlm_research_response(parts[1], parts[2])
            if command == "/nlm_research":
                return "usage: /nlm_research <notebook_id> <query>"
            if command.startswith("/nlm_podcast "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_podcast_response(nb_id)
            if command == "/nlm_podcast":
                return "usage: /nlm_podcast <notebook_id>"
            if command.startswith("/nlm_chat "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_chat <notebook_id> <pregunta>"
                return self._nlm_chat_response(parts[1], parts[2])
            if command == "/nlm_chat":
                return "usage: /nlm_chat <notebook_id> <pregunta>"
            return "Comando NLM no reconocido. Disponibles: /nlm_list, /nlm_create, /nlm_delete, /nlm_status, /nlm_sources, /nlm_text, /nlm_research, /nlm_podcast, /nlm_chat"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("NLM command error")
            return f"Error en NotebookLM: {exc}"

    def _nlm_list_response(self) -> str:
        notebooks = self.notebooklm.list_notebooks()
        if not notebooks:
            return "No hay notebooks."
        lines = []
        for nb in notebooks:
            short_id = nb["id"][:8]
            lines.append(f"{short_id}  {nb['title']}")
        return "\n".join(lines)

    def _nlm_create_response(self, title: str) -> str:
        result = self.notebooklm.create_notebook(title)
        return f"Notebook creado: {result['id'][:8]} — {result['title']}"

    def _nlm_delete_response(self, notebook_id: str) -> str:
        self.notebooklm.delete_notebook(notebook_id)
        return "Notebook eliminado."

    def _nlm_status_response(self, notebook_id: str) -> str:
        info = self.notebooklm.status(notebook_id)
        nb = info["notebook"]
        lines = [f"Notebook: {nb['title']} ({nb['id'][:8]})", f"Sources: {nb['sources_count']}"]
        for src in info["sources"]:
            lines.append(f"  - {src['title']} [{src['kind']}]")
        return "\n".join(lines)

    def _nlm_sources_response(self, notebook_id: str, urls: list[str]) -> str:
        results = self.notebooklm.add_sources(notebook_id, urls)
        lines = [f"{len(results)} source(s) agregados:"]
        for src in results:
            lines.append(f"  - {src['title']}")
        return "\n".join(lines)

    def _nlm_text_response(self, notebook_id: str, title: str, content: str) -> str:
        result = self.notebooklm.add_text(notebook_id, title, content)
        return f"Source de texto agregado: {result['title']}"

    def _nlm_research_response(self, notebook_id: str, query: str) -> str:
        return self.notebooklm.start_research(notebook_id, query)

    def _nlm_podcast_response(self, notebook_id: str) -> str:
        return self.notebooklm.start_podcast(notebook_id)

    def _nlm_chat_response(self, notebook_id: str, question: str) -> str:
        return self.notebooklm.chat(notebook_id, question)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_notebooklm.py -v`
Expected: All 18 tests PASS

- [ ] **Step 7: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS, no regressions

- [ ] **Step 8: Commit**

```bash
git add claw_v2/bot.py tests/test_notebooklm.py
git commit -m "feat(nlm): add /nlm_* bot commands for NotebookLM"
```

---

### Task 5: Wire service in lifecycle.py and register Telegram menu

**Files:**
- Modify: `claw_v2/lifecycle.py:46-71`
- Modify: `claw_v2/telegram.py:130-161`

- [ ] **Step 1: Wire NotebookLMService in lifecycle.py**

In `claw_v2/lifecycle.py`, add the import at the top (after existing imports):

```python
from claw_v2.notebooklm import NotebookLMService
```

In the `run()` function, after `await transport.start()` (line 64), add:

```python
        # Wire NotebookLM with Telegram notify callback
        loop = asyncio.get_running_loop()

        def _nlm_notify(message: str) -> None:
            if runtime.config.telegram_allowed_user_id and transport._app:
                asyncio.run_coroutine_threadsafe(
                    transport._app.bot.send_message(
                        chat_id=int(runtime.config.telegram_allowed_user_id),
                        text=message,
                    ),
                    loop,
                )

        nlm_service = NotebookLMService(notify=_nlm_notify, observe=runtime.observe)
        runtime.bot.notebooklm = nlm_service
```

- [ ] **Step 2: Add `/nlm_*` commands to Telegram menu**

In `claw_v2/telegram.py`, add to the `commands` list inside `_set_commands()` (before the closing `]` around line 157):

```python
            BotCommand("nlm_list", "Listar notebooks de NotebookLM"),
            BotCommand("nlm_create", "Crear notebook — /nlm_create <titulo>"),
            BotCommand("nlm_status", "Estado de notebook — /nlm_status <id>"),
            BotCommand("nlm_sources", "Agregar fuentes — /nlm_sources <id> <urls>"),
            BotCommand("nlm_research", "Deep Research — /nlm_research <id> <query>"),
            BotCommand("nlm_podcast", "Generar podcast — /nlm_podcast <id>"),
            BotCommand("nlm_chat", "Chat con notebook — /nlm_chat <id> <pregunta>"),
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add claw_v2/lifecycle.py claw_v2/telegram.py
git commit -m "feat(nlm): wire NotebookLMService in lifecycle and register Telegram menu"
```

---

### Task 6: Manual smoke test

**Files:** None (verification only)

- [ ] **Step 1: Restart the bot**

```bash
kill $(pgrep -f 'claw_v2.main') && sleep 3
```

Wait for auto-restart (daemon supervisor).

- [ ] **Step 2: Verify single process**

```bash
ps aux | grep 'claw_v2.main' | grep -v grep
```

Expected: Single process with new PID.

- [ ] **Step 3: Test via Telegram**

Send these commands in Telegram to Claw:

1. `/nlm_list` — should show notebooks
2. `/nlm_create Test Smoke` — should create and return ID
3. `/nlm_status <id from step 2>` — should show notebook info with 0 sources
4. `/nlm_delete <id from step 2>` — should confirm deletion

- [ ] **Step 4: Commit any fixes if needed**

If smoke test reveals issues, fix and commit.
