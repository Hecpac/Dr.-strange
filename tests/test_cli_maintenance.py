from __future__ import annotations

import subprocess
import unittest
from collections import defaultdict

from claw_v2.cli_maintenance import run_cli_maintenance_update


def _completed(
    args: list[str], stdout: str, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class CliMaintenanceTests(unittest.TestCase):
    def test_updates_only_outdated_cli_and_verifies_versions(self) -> None:
        outputs = defaultdict(list)
        outputs[("codex", "--version")] = [
            "codex-cli 0.142.3\n",
            "codex-cli 0.142.4\n",
        ]
        outputs[("claude", "--version")] = [
            "2.1.195 (Claude Code)\n",
            "2.1.195 (Claude Code)\n",
        ]
        outputs[("npm", "view", "@openai/codex", "version")] = ["0.142.4\n"]
        outputs[("npm", "view", "@anthropic-ai/claude-code", "version")] = ["2.1.195\n"]
        calls: list[tuple[str, ...]] = []

        def runner(
            args: list[str], *, timeout_s: float, check: bool = False, **_kwargs
        ) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(args))
            if tuple(args) == (
                "npm",
                "install",
                "-g",
                "--fetch-timeout=300000",
                "@openai/codex@0.142.4",
            ):
                return _completed(args, "changed 1 package\n")
            return _completed(args, outputs[tuple(args)].pop(0))

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(result.installed_packages, ("@openai/codex@0.142.4",))
        self.assertIn(
            (
                "npm",
                "install",
                "-g",
                "--fetch-timeout=300000",
                "@openai/codex@0.142.4",
            ),
            calls,
        )
        self.assertNotIn(
            (
                "npm",
                "install",
                "-g",
                "--fetch-timeout=300000",
                "@anthropic-ai/claude-code@2.1.195",
            ),
            calls,
        )
        self.assertEqual(result.tool_versions["codex"]["verified"], "0.142.4")
        self.assertEqual(result.tool_versions["claude"]["verified"], "2.1.195")

    def test_install_failure_returns_failed_verification_status(self) -> None:
        def runner(
            args: list[str], *, timeout_s: float, check: bool = False, **_kwargs
        ) -> subprocess.CompletedProcess[str]:
            match tuple(args):
                case ("codex", "--version"):
                    return _completed(args, "codex-cli 0.142.3\n")
                case ("claude", "--version"):
                    return _completed(args, "2.1.194 (Claude Code)\n")
                case ("npm", "view", "@openai/codex", "version"):
                    return _completed(args, "0.142.4\n")
                case ("npm", "view", "@anthropic-ai/claude-code", "version"):
                    return _completed(args, "2.1.195\n")
                case ("npm", "install", "-g", "--fetch-timeout=300000", *_packages):
                    return _completed(args, "", returncode=1)
            raise AssertionError(f"unexpected command: {args!r}")

        result = run_cli_maintenance_update(runner=runner)

        self.assertEqual(result.verification_status, "failed")
        self.assertIn("npm install failed", result.error)
        self.assertIn("@openai/codex@0.142.4", result.installed_packages)
        self.assertIn("@anthropic-ai/claude-code@2.1.195", result.installed_packages)


if __name__ == "__main__":
    unittest.main()
