from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from claw_v2.container import ContainerPolicy, sandboxed_run, _docker_run


class SandboxedRunTests(unittest.TestCase):
    def test_runs_command_with_timeout(self) -> None:
        policy = ContainerPolicy(timeout_seconds=5)
        result = sandboxed_run("echo hello", cwd="/tmp", policy=policy)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)

    def test_timeout_kills_process(self) -> None:
        policy = ContainerPolicy(timeout_seconds=1)
        with self.assertRaises(subprocess.TimeoutExpired):
            sandboxed_run("sleep 30", cwd="/tmp", policy=policy)

    def test_default_policy_used(self) -> None:
        result = sandboxed_run("echo default", cwd="/tmp")
        self.assertIn("default", result.stdout)


class DockerRunTests(unittest.TestCase):
    @patch("claw_v2.container.subprocess.run")
    def test_docker_command_construction(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        policy = ContainerPolicy(
            cpu_seconds=60, memory_mb=256, max_processes=32,
            network_enabled=False, docker_image="python:3.12-slim",
            timeout_seconds=120,
        )
        _docker_run("pytest -x", cwd="/tmp/worktree", policy=policy)
        args = mock_run.call_args[0][0]
        self.assertIn("docker", args)
        self.assertIn("--network=none", args)
        self.assertIn("--memory=256m", args)
        self.assertIn("python:3.12-slim", args)
        self.assertIn("pytest -x", args)


if __name__ == "__main__":
    unittest.main()
