from __future__ import annotations

import asyncio
import time
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


import tempfile
from pathlib import Path

from tests.helpers import make_config


def _make_bot_with_nlm(nlm_service: NotebookLMService) -> "BotService":
    """Create a minimal BotService with a NotebookLMService attached."""
    from claw_v2.bot import BotService

    tmpdir = tempfile.mkdtemp()
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

        tmpdir = tempfile.mkdtemp()
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
        result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_list")
        self.assertIn("no disponible", result.lower())


if __name__ == "__main__":
    unittest.main()
