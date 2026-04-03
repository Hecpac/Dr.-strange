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


if __name__ == "__main__":
    unittest.main()
