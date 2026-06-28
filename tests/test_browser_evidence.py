from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.browser_evidence import (
    BrowserEvidenceCollector,
    extract_browser_evidence_targets,
)
from claw_v2.browser_tools import BrowserToolResult
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.sandbox import SandboxPolicy
from claw_v2.tools import ToolRegistry


class BrowserEvidenceCollectorTests(unittest.TestCase):
    def test_extracts_scheme_and_host_urls_in_order(self) -> None:
        targets = extract_browser_evidence_targets(
            [
                "Revisa https://example.com/page.",
                "Luego compara docs.python.org/3/library/asyncio.html",
            ],
            limit=3,
        )

        self.assertEqual(
            targets,
            (
                "https://example.com/page",
                "https://docs.python.org/3/library/asyncio.html",
            ),
        )

    def test_no_target_returns_none_without_tool_calls(self) -> None:
        calls: list[str] = []
        collector = BrowserEvidenceCollector(
            tool_executor=lambda name, _args: calls.append(name) or {"ok": True}
        )

        report = collector.collect(task_id="t1", objective="investiga el tema", research_results=[])

        self.assertIsNone(report)
        self.assertEqual(calls, [])

    def test_url_collection_uses_only_read_only_browser_tools(self) -> None:
        calls: list[str] = []

        def execute(name: str, args: dict) -> dict:
            calls.append(name)
            if name == "BrowserNavigate":
                return {
                    "ok": True,
                    "url": args["url"],
                    "title": "Example",
                    "snapshot": "token=sk-testsecretshouldberedacted",
                    "element_count": 1,
                }
            if name == "BrowserSnapshot":
                return {
                    "ok": True,
                    "url": "https://example.com",
                    "snapshot": "Visible page text",
                    "element_count": 2,
                }
            raise AssertionError(f"unexpected tool {name}")

        collector = BrowserEvidenceCollector(tool_executor=execute)

        report = collector.collect(
            task_id="t1",
            objective="Abre https://example.com y haz click si ves un boton",
            research_results=[],
        )

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(calls, ["BrowserNavigate", "BrowserSnapshot"])
        self.assertIn("Browser Evidence", report.content)
        self.assertIn("Example", report.content)
        self.assertIn("[REDACTED]", report.content)
        self.assertNotIn("sk-testsecretshouldberedacted", report.content)

    def test_current_page_snapshot_uses_snapshot_only(self) -> None:
        calls: list[str] = []

        def execute(name: str, _args: dict) -> dict:
            calls.append(name)
            return {
                "ok": True,
                "url": "https://example.com/current",
                "snapshot": "current tab body",
                "element_count": 0,
            }

        collector = BrowserEvidenceCollector(tool_executor=execute)

        report = collector.collect(
            task_id="t1",
            objective="Resume la pagina actual del browser",
            research_results=[],
        )

        self.assertIsNotNone(report)
        self.assertEqual(calls, ["BrowserSnapshot"])

    def test_tool_registry_collector_respects_runtime_policy_for_read_tools(self) -> None:
        class FakeBrowserService:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def navigate(self, session_id: str, url: str, *, observe=None) -> BrowserToolResult:
                self.calls.append(f"navigate:{session_id}:{url}")
                return BrowserToolResult(
                    success=True,
                    url=url,
                    title="Example",
                    snapshot="Example page",
                    element_count=1,
                )

            def snapshot(
                self, session_id: str, full: bool = False, *, observe=None
            ) -> BrowserToolResult:
                self.calls.append(f"snapshot:{session_id}:{full}")
                return BrowserToolResult(
                    success=True,
                    url="https://example.com",
                    snapshot="Snapshot body",
                    element_count=2,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            fake_service = FakeBrowserService()
            registry = ToolRegistry.default(workspace_root=workspace)
            policy = SandboxPolicy(workspace_root=workspace, network_policy="allow")
            network = DomainAllowlistEnforcer(resolver=lambda _host: ["93.184.216.34"])
            collector = BrowserEvidenceCollector.from_tool_registry(
                registry, policy=policy, network_enforcer=network
            )

            with patch("claw_v2.tools._browser_tool_service", return_value=fake_service):
                report = collector.collect(
                    task_id="task-123",
                    objective="Captura evidencia de https://example.com",
                    research_results=[],
                )

        self.assertIsNotNone(report)
        self.assertEqual(
            fake_service.calls,
            [
                "navigate:coordinator:task-123:https://example.com",
                "snapshot:coordinator:task-123:False",
            ],
        )


if __name__ == "__main__":
    unittest.main()
