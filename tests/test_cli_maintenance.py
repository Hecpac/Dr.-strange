from __future__ import annotations

import os
import subprocess
import unittest
from collections import defaultdict
from unittest.mock import patch

from claw_v2.cli_maintenance import run_cli_maintenance_update


def _completed(
    args: list[str], stdout: str, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class CliMaintenanceTests(unittest.TestCase):
    @patch.dict(os.environ, {"CODEX_CLI_PATH": "", "CLAUDE_CLI_PATH": ""})
    @patch("shutil.which")
    def test_codex_standalone_from_path_updates_same_entrypoint_without_npm(self, which) -> None:
        codex_entrypoint = "/Users/hector/.local/bin/codex"
        which.side_effect = {"codex": codex_entrypoint, "claude": None}.get
        outputs = defaultdict(list)
        outputs[(codex_entrypoint, "--version")] = [
            "codex-cli 0.142.4\n",
            "codex-cli 0.144.1\n",
        ]
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            calls.append(command)
            match command:
                case (entrypoint, "--version") if entrypoint == codex_entrypoint:
                    return _completed(args, outputs[command].pop(0))
                case (entrypoint, "update") if entrypoint == codex_entrypoint:
                    return _completed(args, "updated\n")
                case ("npm", "view", "@anthropic-ai/claude-code", "version"):
                    return _completed(args, "2.1.207\n")
                case (
                    "npm",
                    "install",
                    "-g",
                    "--fetch-timeout=300000",
                    "@anthropic-ai/claude-code@2.1.207",
                ):
                    return _completed(args, "added 1 package\n")
                case ("claude", "--version"):
                    return _completed(args, "2.1.207 (Claude Code)\n")
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(result.tool_versions["codex"]["action"], "updated")
        self.assertNotIn(("npm", "view", "@openai/codex", "version"), calls)
        self.assertFalse(any("@openai/codex" in command for command in calls))
        self.assertEqual(calls.count((codex_entrypoint, "--version")), 2)
        self.assertIn((codex_entrypoint, "update"), calls)

    @patch.dict(os.environ, {"CODEX_CLI_PATH": "", "CLAUDE_CLI_PATH": ""})
    @patch("shutil.which")
    def test_claude_from_path_updates_same_entrypoint_without_npm(self, which) -> None:
        claude_entrypoint = "/opt/homebrew/bin/claude"
        which.side_effect = {"codex": None, "claude": claude_entrypoint}.get
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            calls.append(command)
            match command:
                case (entrypoint, "--version") if entrypoint == claude_entrypoint:
                    return _completed(args, "2.1.207 (Claude Code)\n")
                case (entrypoint, "update") if entrypoint == claude_entrypoint:
                    return _completed(args, "already current\n")
                case ("npm", "view", "@openai/codex", "version"):
                    return _completed(args, "0.144.1\n")
                case (
                    "npm",
                    "install",
                    "-g",
                    "--fetch-timeout=300000",
                    "@openai/codex@0.144.1",
                ):
                    return _completed(args, "added 1 package\n")
                case ("codex", "--version"):
                    return _completed(args, "codex-cli 0.144.1\n")
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(result.tool_versions["claude"]["action"], "already_current")
        self.assertNotIn(("npm", "view", "@anthropic-ai/claude-code", "version"), calls)
        self.assertFalse(any("@anthropic-ai/claude-code" in command for command in calls))
        self.assertIn((claude_entrypoint, "update"), calls)

    @patch.dict(
        os.environ,
        {
            "CODEX_CLI_PATH": "/stale/codex",
            "CLAUDE_CLI_PATH": "/active/claude",
        },
    )
    @patch("shutil.which")
    def test_stale_configured_path_fails_closed_before_other_updates(self, which) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            calls.append(tuple(args))
            return _completed(args, "", returncode=127)

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertEqual(calls, [("/stale/codex", "--version")])
        self.assertNotIn(("/active/claude", "update"), calls)
        which.assert_not_called()

    @patch.dict(os.environ, {"CODEX_CLI_PATH": "", "CLAUDE_CLI_PATH": ""})
    @patch("shutil.which", return_value=None)
    def test_both_absent_bootstrap_through_npm(self, _which) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            calls.append(command)
            match command:
                case ("npm", "view", "@openai/codex", "version"):
                    return _completed(args, "0.144.1\n")
                case ("npm", "view", "@anthropic-ai/claude-code", "version"):
                    return _completed(args, "2.1.207\n")
                case ("npm", "install", "-g", "--fetch-timeout=300000", *_packages):
                    return _completed(args, "added 2 packages\n")
                case ("codex", "--version"):
                    return _completed(args, "codex-cli 0.144.1\n")
                case ("claude", "--version"):
                    return _completed(args, "2.1.207 (Claude Code)\n")
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(
            result.installed_packages,
            ("@openai/codex@0.144.1", "@anthropic-ai/claude-code@2.1.207"),
        )
        self.assertNotIn(("codex", "--version"), calls[:2])
        self.assertNotIn(("claude", "--version"), calls[:2])

    @patch.dict(os.environ, {"CODEX_CLI_PATH": "", "CLAUDE_CLI_PATH": ""})
    @patch("shutil.which", return_value=None)
    def test_absent_npm_install_failure_returns_failed_verification_status(self, _which) -> None:
        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            match tuple(args):
                case ("npm", "view", "@openai/codex", "version"):
                    return _completed(args, "0.144.1\n")
                case ("npm", "view", "@anthropic-ai/claude-code", "version"):
                    return _completed(args, "2.1.207\n")
                case ("npm", "install", "-g", "--fetch-timeout=300000", *_packages):
                    return _completed(args, "install failed\n", returncode=1)
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertIn("npm install failed", result.error)
        self.assertEqual(
            result.installed_packages,
            ("@openai/codex@0.144.1", "@anthropic-ai/claude-code@2.1.207"),
        )

    @patch.dict(
        os.environ,
        {"CODEX_CLI_PATH": "/active/codex", "CLAUDE_CLI_PATH": "/active/claude"},
    )
    def test_present_update_nonzero_fails_after_all_prechecks(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            calls.append(command)
            if command == ("/active/codex", "--version"):
                return _completed(args, "codex-cli 0.142.4\n")
            if command == ("/active/claude", "--version"):
                return _completed(args, "2.1.206 (Claude Code)\n")
            if command == ("/active/codex", "update"):
                return _completed(args, "update failed\n", returncode=1)
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertEqual(
            calls[:2],
            [("/active/codex", "--version"), ("/active/claude", "--version")],
        )
        self.assertNotIn(("/active/claude", "update"), calls)

    @patch.dict(
        os.environ,
        {"CODEX_CLI_PATH": "/active/codex", "CLAUDE_CLI_PATH": "/active/claude"},
    )
    def test_present_update_timeout_is_reported_as_failure(self) -> None:
        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            if command == ("/active/codex", "--version"):
                return _completed(args, "codex-cli 0.142.4\n")
            if command == ("/active/claude", "--version"):
                return _completed(args, "2.1.206 (Claude Code)\n")
            if command == ("/active/codex", "update"):
                raise subprocess.TimeoutExpired(args, timeout_s)
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertIn("codex update failed", result.error)

    @patch.dict(
        os.environ,
        {"CODEX_CLI_PATH": "/active/codex", "CLAUDE_CLI_PATH": "/active/claude"},
    )
    def test_invalid_post_update_version_fails_verification(self) -> None:
        codex_checks = 0

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            nonlocal codex_checks
            command = tuple(args)
            if command == ("/active/codex", "--version"):
                codex_checks += 1
                output = "codex-cli 0.142.4\n" if codex_checks == 1 else "unknown\n"
                return _completed(args, output)
            if command == ("/active/claude", "--version"):
                return _completed(args, "2.1.207 (Claude Code)\n")
            if command == ("/active/codex", "update"):
                return _completed(args, "updated\n")
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertIn("codex verification failed", result.error)

    @patch.dict(
        os.environ,
        {
            "CODEX_CLI_PATH": "/Users/hector/.local/bin/codex",
            "CLAUDE_CLI_PATH": "/opt/homebrew/bin/claude",
        },
    )
    def test_configured_logical_symlink_string_is_not_canonicalized(self) -> None:
        logical_codex = "/Users/hector/.local/bin/codex"
        calls: list[tuple[str, ...]] = []

        def runner(args: list[str], *, timeout_s: float, **_kwargs):
            command = tuple(args)
            calls.append(command)
            if command == (logical_codex, "--version"):
                return _completed(args, "codex-cli 0.144.1\n")
            if command == ("/opt/homebrew/bin/claude", "--version"):
                return _completed(args, "2.1.207 (Claude Code)\n")
            if command in {
                (logical_codex, "update"),
                ("/opt/homebrew/bin/claude", "update"),
            }:
                return _completed(args, "already current\n")
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(calls.count((logical_codex, "--version")), 2)
        self.assertIn((logical_codex, "update"), calls)
        self.assertFalse(any("standalone/current" in part for command in calls for part in command))


if __name__ == "__main__":
    unittest.main()
