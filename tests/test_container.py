from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from claw_v2.container import ContainerPolicy, sandboxed_run, _docker_run


class SandboxedRunTests(unittest.TestCase):
    def test_default_policy_disables_network_and_uses_host_sanitized(self) -> None:
        policy = ContainerPolicy()
        self.assertFalse(policy.network_enabled)
        self.assertEqual(policy.isolation_mode, "host_sanitized")

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

    @patch("claw_v2.container.subprocess.run")
    def test_limited_run_uses_sanitized_child_env(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        sandboxed_run("echo ok", cwd="/tmp", policy=ContainerPolicy())
        env = mock_run.call_args.kwargs["env"]
        self.assertIn("PATH", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    @patch("claw_v2.container.subprocess.run")
    def test_sandboxed_run_emits_env_counts_without_values(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        observe = MagicMock()
        sandboxed_run("echo ok", cwd="/tmp", policy=ContainerPolicy(), observe=observe)
        payload = observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["runner"], "container.sandboxed_run")
        self.assertIn("preserved_count", payload)
        self.assertIn("dropped_count", payload)
        self.assertNotIn("env", payload)


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
