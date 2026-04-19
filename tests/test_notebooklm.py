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
        chat_result.answer = "Here is the summary of your sources."
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

    def test_infographic_notifies_on_completion(self) -> None:
        client = AsyncMock()
        client.notebooks.list.return_value = [_mock_notebook("abc-full", "Info NB")]
        status_obj = MagicMock()
        status_obj.task_id = "task-2"
        client.artifacts.generate_infographic.return_value = status_obj
        client.artifacts.wait_for_completion.return_value = MagicMock()
        svc = self._make_service(client)
        result = svc.start_artifact("abc", "infographic")
        self.assertIn("infografia", result.lower())
        for _ in range(50):
            if "abc-full" not in svc._running:
                break
            time.sleep(0.1)
        svc._notify.assert_called_once()
        call_msg = svc._notify.call_args[0][0]
        self.assertIn("infografia", call_msg.lower())

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
        self.assertIn("2026-04-02", result)

    def test_nlm_create(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "new-id", "title": "Noticias AI"}
        nlm.start_research.return_value = "Deep Research iniciado..."
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(user_id="123", session_id="s1", text="/nlm_create Noticias AI")
        nlm.create_notebook.assert_called_once_with("Noticias AI")
        nlm.start_research.assert_called_once_with("new-id", "Noticias AI")
        self.assertIn("new-id", result)
        self.assertIn("cuaderno activo", result.lower())

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

    def test_plain_language_create_notebook_starts_research(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "nb-full-id", "title": "Tendencias IA"}
        nlm.start_research.return_value = "Deep Research iniciado..."
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(
            user_id="123",
            session_id="s1",
            text="Créame un cuaderno sobre Tendencias IA",
        )
        nlm.create_notebook.assert_called_once_with("Tendencias IA")
        nlm.start_research.assert_called_once_with("nb-full-id", "Tendencias IA")
        self.assertIn("Deep Research iniciado", result)

    def test_plain_language_podcast_uses_active_notebook(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "nb-full-id", "title": "Tendencias IA"}
        nlm.start_research.return_value = "Deep Research iniciado..."
        nlm.start_artifact.return_value = "Generando podcast..."
        bot = _make_bot_with_nlm(nlm)
        bot.handle_text(
            user_id="123",
            session_id="s1",
            text="creame un cuaderno sobre Tendencias IA",
        )
        result = bot.handle_text(
            user_id="123",
            session_id="s1",
            text="hazme un podcast",
        )
        nlm.start_artifact.assert_called_once_with("nb-full-id", "podcast")
        self.assertIn("Generando podcast", result)

    def test_plain_language_infographic_uses_active_notebook(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "nb-full-id", "title": "Tendencias IA"}
        nlm.start_research.return_value = "Deep Research iniciado..."
        nlm.start_artifact.return_value = "Generando infografia..."
        bot = _make_bot_with_nlm(nlm)
        bot.handle_text(
            user_id="123",
            session_id="s1",
            text="creame un cuaderno sobre Tendencias IA",
        )
        result = bot.handle_text(
            user_id="123",
            session_id="s1",
            text="generame una infografia",
        )
        nlm.start_artifact.assert_called_once_with("nb-full-id", "infographic")
        self.assertIn("infografia", result.lower())

    def test_nlm_podcast_without_id_uses_active_notebook(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        nlm.create_notebook.return_value = {"id": "nb-full-id", "title": "Tendencias IA"}
        nlm.start_research.return_value = "Deep Research iniciado..."
        nlm.start_podcast.return_value = "Generando podcast..."
        bot = _make_bot_with_nlm(nlm)
        bot.handle_text(
            user_id="123",
            session_id="s1",
            text="/nlm_create Tendencias IA",
        )
        result = bot.handle_text(
            user_id="123",
            session_id="s1",
            text="/nlm_podcast",
        )
        nlm.start_podcast.assert_called_once_with("nb-full-id")
        self.assertIn("Generando podcast", result)

    def test_nlm_text_rejects_empty_title_or_content(self) -> None:
        nlm = MagicMock(spec=NotebookLMService)
        bot = _make_bot_with_nlm(nlm)
        result = bot.handle_text(
            user_id="123",
            session_id="s1",
            text="/nlm_text abc | contenido",
        )
        self.assertIn("usage:", result)


class CdpCreateNotebookTests(unittest.TestCase):
    """Production path: when no SDK client_factory is set, create_notebook
    delegates to the CDP driver (claw_v2.notebooklm_cdp.create_notebook by
    default, overridable via _cdp_create_notebook_fn for unit tests).
    """

    def test_create_notebook_uses_cdp_when_no_client_factory(self) -> None:
        svc = NotebookLMService()
        captured: dict[str, str] = {}

        def fake_cdp(title: str) -> dict:
            captured["title"] = title
            return {"id": "cdp-notebook-id", "title": title}

        svc._cdp_create_notebook_fn = fake_cdp
        result = svc.create_notebook("My CDP Notebook")
        self.assertEqual(result, {"id": "cdp-notebook-id", "title": "My CDP Notebook"})
        self.assertEqual(captured["title"], "My CDP Notebook")

    def test_create_notebook_prefers_client_factory_when_set(self) -> None:
        client = AsyncMock()
        client.notebooks.create.return_value = _mock_notebook("sdk-id", "SDK NB")
        svc = NotebookLMService()
        svc._client_factory = lambda: client
        cdp_called = False

        def fake_cdp(_title: str) -> dict:
            nonlocal cdp_called
            cdp_called = True
            return {"id": "should-not-be-used", "title": _title}

        svc._cdp_create_notebook_fn = fake_cdp
        result = svc.create_notebook("Test")
        self.assertEqual(result["id"], "sdk-id")
        self.assertFalse(cdp_called, "CDP should not be invoked when _client_factory is set")


if __name__ == "__main__":
    unittest.main()
