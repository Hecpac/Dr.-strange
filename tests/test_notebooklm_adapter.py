from __future__ import annotations

import subprocess
import time
import unittest
from typing import Any
from unittest.mock import AsyncMock

from claw_v2.notebooklm import NotebookLMService
from claw_v2.notebooklm_adapter import JacobNotebookLMCLIAdapter


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")


class JacobNotebookLMCLIAdapterTests(unittest.TestCase):
    def test_list_and_create_normalize_json_shapes(self) -> None:
        calls: list[list[str]] = []

        def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[1:4] == ["notebook", "list", "--json"]:
                return _completed(
                    cmd,
                    '[{"id":"nb-1","title":"One","source_count":2,"updated_at":"2026-06-01"}]',
                )
            if cmd[1:4] == ["notebook", "create", "New"]:
                return _completed(cmd, '{"notebook_id":"nb-new","title":"New"}')
            raise AssertionError(f"unexpected command: {cmd}")

        adapter = JacobNotebookLMCLIAdapter(command="nlm", profile="work", runner=runner)

        self.assertEqual(
            adapter.list_notebooks(),
            [{"id": "nb-1", "title": "One", "created_at": "2026-06-01", "source_count": 2}],
        )
        self.assertEqual(adapter.create_notebook("New"), {"id": "nb-new", "title": "New"})
        self.assertTrue(all(call[-2:] == ["--profile", "work"] for call in calls))

    def test_chat_returns_answer_from_query_json(self) -> None:
        def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            self.assertEqual(cmd[1:4], ["notebook", "query", "nb-1"])
            return _completed(cmd, '{"answer":"respuesta citada","conversation_id":"c1"}')

        adapter = JacobNotebookLMCLIAdapter(command="nlm", runner=runner)

        self.assertEqual(adapter.chat("nb-1", "pregunta"), "respuesta citada")

    def test_deep_research_returns_verified_source_delta(self) -> None:
        counts = iter([2, 5])
        research_command: list[str] | None = None

        def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            nonlocal research_command
            if cmd[1:4] == ["notebook", "get", "nb-1"]:
                return _completed(cmd, f'{{"notebook_id":"nb-1","title":"One","source_count":{next(counts)}}}')
            if cmd[1:3] == ["research", "start"]:
                research_command = cmd
                return _completed(cmd, "✓ 3 source(s) imported.")
            raise AssertionError(f"unexpected command: {cmd}")

        adapter = JacobNotebookLMCLIAdapter(command="nlm", runner=runner)

        self.assertEqual(adapter.deep_research("nb-1", "tema", mode="deep"), 3)
        self.assertIsNotNone(research_command)
        assert research_command is not None
        self.assertIn("--auto-import", research_command)
        self.assertIn("--force", research_command)

    def test_generate_artifact_polls_until_new_artifact_completed(self) -> None:
        status_calls = 0

        def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            nonlocal status_calls
            if cmd[1:3] == ["studio", "status"]:
                status_calls += 1
                if status_calls == 1:
                    return _completed(cmd, '[{"id":"old","type":"audio","status":"completed"}]')
                return _completed(
                    cmd,
                    '[{"id":"old","type":"audio","status":"completed"},'
                    '{"id":"new","type":"audio","status":"completed"}]',
                )
            if cmd[1:3] == ["audio", "create"]:
                return _completed(cmd, "✓ Audio generation started")
            raise AssertionError(f"unexpected command: {cmd}")

        adapter = JacobNotebookLMCLIAdapter(
            command="nlm",
            runner=runner,
            artifact_timeout_seconds=1,
            poll_interval_seconds=0.01,
        )

        adapter.generate_artifact("nb-1", "podcast")
        self.assertEqual(status_calls, 2)


class NotebookLMServiceExternalBackendTests(unittest.TestCase):
    def test_service_routes_create_chat_and_status_to_external_backend(self) -> None:
        backend = _StubExternalBackend()
        svc = NotebookLMService(external_backend=backend)

        self.assertEqual(svc.create_notebook("Title"), {"id": "nb-full", "title": "Title"})
        self.assertEqual(svc.chat("nb", "pregunta"), "external answer")
        self.assertEqual(svc.status("nb")["notebook"]["id"], "nb-full")

        self.assertIn(("create_notebook", ("Title",)), backend.calls)
        self.assertIn(("chat", ("nb-full", "pregunta")), backend.calls)
        self.assertIn(("status", ("nb-full",)), backend.calls)

    def test_service_prefers_injected_sdk_client_over_external_backend(self) -> None:
        backend = _StubExternalBackend()
        client = AsyncMock()
        nb = _Notebook("sdk-id", "SDK")
        client.notebooks.create.return_value = nb
        svc = NotebookLMService(external_backend=backend)
        svc._client_factory = lambda: client

        result = svc.create_notebook("SDK")

        self.assertEqual(result, {"id": "sdk-id", "title": "SDK"})
        self.assertEqual(backend.calls, [])

    def test_service_start_research_uses_external_backend_in_worker(self) -> None:
        backend = _StubExternalBackend()
        svc = NotebookLMService(external_backend=backend)

        message = svc.start_research("nb", "tema")

        self.assertIn("Deep Research iniciado", message)
        deadline = time.time() + 2.0
        while time.time() < deadline and svc._running:
            time.sleep(0.01)
        self.assertIn(("deep_research", ("nb-full", "tema", "deep")), backend.calls)


class _Notebook:
    def __init__(self, notebook_id: str, title: str) -> None:
        self.id = notebook_id
        self.title = title
        self.created_at = None


class _StubExternalBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def list_notebooks(self) -> list[dict[str, Any]]:
        self.calls.append(("list_notebooks", ()))
        return [{"id": "nb-full", "title": "External", "created_at": "today"}]

    def create_notebook(self, title: str) -> dict[str, str]:
        self.calls.append(("create_notebook", (title,)))
        return {"id": "nb-full", "title": title}

    def status(self, notebook_id: str) -> dict[str, Any]:
        self.calls.append(("status", (notebook_id,)))
        return {"notebook": {"id": notebook_id, "title": "External", "sources_count": 1}, "sources": []}

    def chat(self, notebook_id: str, question: str) -> str:
        self.calls.append(("chat", (notebook_id, question)))
        return "external answer"

    def deep_research(self, notebook_id: str, query: str, mode: str = "deep") -> int:
        self.calls.append(("deep_research", (notebook_id, query, mode)))
        return 3


if __name__ == "__main__":
    unittest.main()
