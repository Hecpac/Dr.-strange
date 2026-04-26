from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.adapters.codex import CodexAdapter


def _make_request(prompt: str = "Write a hello world function", lane: str = "worker") -> LLMRequest:
    return LLMRequest(
        prompt=prompt,
        system_prompt=None,
        lane=lane,
        provider="codex",
        model="codex-mini-latest",
        effort=None,
        session_id=None,
        max_budget=0.5,
        evidence_pack=None,
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=60.0,
        cwd="/tmp",
    )


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["codex"], returncode, stdout=stdout, stderr=stderr)


def _preflight_ok() -> list[subprocess.CompletedProcess[str]]:
    return [
        _proc(stdout="codex-cli 0.125.0"),
        _proc(stdout="Logged in using ChatGPT"),
    ]


class CodexAdapterTests(unittest.TestCase):
    def test_transport_override_bypasses_subprocess(self) -> None:
        from claw_v2.types import LLMResponse
        fixed = LLMResponse(
            content="hello world", lane="worker", provider="codex",
            model="codex-mini-latest", confidence=0.7, cost_estimate=0.0, artifacts={},
        )
        adapter = CodexAdapter(transport=lambda _: fixed)
        result = adapter.complete(_make_request())
        self.assertEqual(result.content, "hello world")
        self.assertEqual(result.provider, "codex")

    def test_raises_unavailable_when_cli_not_found(self) -> None:
        adapter = CodexAdapter(cli_path="nonexistent-codex-abc123")
        with self.assertRaises(AdapterUnavailableError):
            adapter.complete(_make_request())

    def test_raises_adapter_error_on_nonzero_exit(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = _proc(returncode=1, stdout="", stderr="some error")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[*_preflight_ok(), fake_result]):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(AdapterError):
                    adapter.complete(_make_request())

    def test_successful_completion_returns_stdout(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="def hello():\n    print('hello')\n", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())
        self.assertEqual(result.content, "def hello():\n    print('hello')")
        self.assertEqual(result.provider, "codex")
        self.assertEqual(result.cost_estimate, 0.0)
        self.assertGreater(result.confidence, 0.4)
        self.assertLessEqual(result.confidence, 0.85)
        self.assertIn("codex_version", result.artifacts)
        self.assertIn("auth_status", result.artifacts)
        cmd = mock_run.call_args.args[0]
        self.assertNotIn("Write a hello world function", cmd)
        self.assertEqual(mock_run.call_args.kwargs["input"], "Write a hello world function")

    def test_confidence_is_low_when_response_empty(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())
        self.assertEqual(result.confidence, 0.3)

    def test_confidence_is_high_when_response_is_structured(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        structured = (
            "## Edits\n"
            "- claw_v2/foo.py: bumped timeout 120 -> 300\n"
            "- claw_v2/bar.py: added retry helper\n"
            "## Build/Verify\n"
            "- cmd: pytest -q\n"
            "  result: ok\n"
            "## Evidence\n"
            "- diff hunks attached above; no screenshots needed for backend change\n"
        )
        fake_result = MagicMock(returncode=0, stdout=structured, stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())
        self.assertGreaterEqual(result.confidence, 0.85)

    def test_passes_cwd_to_subprocess(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="done", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                adapter.complete(_make_request())
        call_kwargs = mock_run.call_args
        cmd = call_kwargs[0][0]
        self.assertIn("-C", cmd)
        self.assertIn("/tmp", cmd)

    def test_tool_capable_is_true(self) -> None:
        self.assertTrue(CodexAdapter.tool_capable)

    def test_raises_adapter_error_on_timeout(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        with patch(
            "claw_v2.adapters.codex.subprocess.run",
            side_effect=[*_preflight_ok(), subprocess.TimeoutExpired("codex", 60)],
        ):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(AdapterError):
                    adapter.complete(_make_request())

    def test_retries_startup_stdin_failure_once(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        first = MagicMock(returncode=1, stdout="", stderr="Reading additional input from stdin...")
        second = MagicMock(returncode=0, stdout="done", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[*_preflight_ok(), first, second]) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())
        self.assertEqual(result.content, "done")
        self.assertEqual(mock_run.call_count, 4)

    def test_no_cwd_flag_when_cwd_is_none(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="done", stderr="")
        request = _make_request()
        request = LLMRequest(
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            lane=request.lane,
            provider=request.provider,
            model=request.model,
            effort=request.effort,
            session_id=request.session_id,
            max_budget=request.max_budget,
            evidence_pack=request.evidence_pack,
            allowed_tools=request.allowed_tools,
            agents=request.agents,
            hooks=request.hooks,
            timeout=request.timeout,
            cwd=None,
        )
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                adapter.complete(request)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("-C", cmd)

    def test_preflight_checks_version_and_login_status_before_exec(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        execution = _proc(stdout="done")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[*_preflight_ok(), execution]) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())

        self.assertEqual(result.content, "done")
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[0], ["/usr/local/bin/codex", "--version"])
        self.assertEqual(calls[1], ["/usr/local/bin/codex", "login", "status"])
        self.assertEqual(calls[2][0:2], ["/usr/local/bin/codex", "exec"])

    def test_preflight_rejects_missing_cwd_before_subprocess(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = f"{tmpdir}/missing"
            request = _make_request()
            request.cwd = missing
            with patch("claw_v2.adapters.codex.subprocess.run") as mock_run:
                with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                    with self.assertRaisesRegex(AdapterUnavailableError, "cwd does not exist"):
                        adapter.complete(request)
        mock_run.assert_not_called()

    def test_preflight_auth_failure_is_unavailable(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        auth_failure = _proc(returncode=1, stderr="Not logged in")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[_preflight_ok()[0], auth_failure]):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaisesRegex(AdapterUnavailableError, "codex login"):
                    adapter.complete(_make_request())

    def test_exec_auth_failure_is_unavailable(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        exec_failure = _proc(returncode=1, stderr="authentication required")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[*_preflight_ok(), exec_failure]):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaisesRegex(AdapterUnavailableError, "codex login"):
                    adapter.complete(_make_request())

    def test_preflight_is_cached_for_subsequent_calls(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        first = _proc(stdout="first")
        second = _proc(stdout="second")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=[*_preflight_ok(), first, second]) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                self.assertEqual(adapter.complete(_make_request()).content, "first")
                self.assertEqual(adapter.complete(_make_request("Write another function")).content, "second")
        self.assertEqual(mock_run.call_count, 4)
