from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claw_v2.main import (
    _is_git_repo,
    _probe_writable,
    _sanitize_job_name,
)


class IsGitRepoTests(unittest.TestCase):
    def test_returns_false_when_git_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.object(shutil, "which", return_value=None):
                self.assertFalse(_is_git_repo(tmpdir))

    def test_returns_false_on_timeout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.object(shutil, "which", return_value="/usr/bin/git"):
                with patch.object(
                    subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
                ):
                    self.assertFalse(_is_git_repo(tmpdir))

    def test_returns_false_on_filenotfound(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.object(shutil, "which", return_value="/usr/bin/git"):
                with patch.object(subprocess, "run", side_effect=FileNotFoundError):
                    self.assertFalse(_is_git_repo(tmpdir))

    def test_returns_false_for_non_repo_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            self.assertFalse(_is_git_repo(tmpdir))


class ProbeWritableTests(unittest.TestCase):
    def test_no_residual_probe_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            _probe_writable(path)
            self.assertFalse(list(path.glob(".claw_write_probe_*")))

    def test_concurrent_probes_do_not_collide(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: _probe_writable(path), range(8)))

            self.assertEqual(results, [None] * 8)
            self.assertFalse(list(path.glob(".claw_write_probe_*")))


class SanitizeJobNameTests(unittest.TestCase):
    def test_distinct_inputs_produce_distinct_names(self) -> None:
        a = _sanitize_job_name("foo-bar")
        b = _sanitize_job_name("foo_bar")
        c = _sanitize_job_name("foo/bar")
        d = _sanitize_job_name("foo bar")
        self.assertEqual(len({a, b, c, d}), 4)

    def test_starts_with_lowercase_slug(self) -> None:
        name = _sanitize_job_name("My-Cool/Job 1")
        self.assertTrue(name.startswith("my_cool_job_1_"))

    def test_empty_input_uses_job_default_with_hash(self) -> None:
        name = _sanitize_job_name("")
        self.assertTrue(name.startswith("job_"))

    def test_deterministic(self) -> None:
        self.assertEqual(_sanitize_job_name("foo-bar"), _sanitize_job_name("foo-bar"))


class DailyCostLimitTests(unittest.TestCase):
    def test_none_uses_default_gate(self) -> None:
        import os
        from claw_v2.adapters.base import LLMRequest, LLMResponse
        from claw_v2.main import build_runtime

        def fake(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            env_clear = {**os.environ, **env}
            env_clear.pop("DAILY_COST_LIMIT", None)
            with patch.dict(os.environ, env_clear, clear=True):
                runtime = build_runtime(anthropic_executor=fake)
                self.assertIsNone(runtime.config.daily_cost_limit)
                self.assertGreaterEqual(len(runtime.router.pre_hooks), 1)

    def test_zero_rejected(self) -> None:
        import os
        from claw_v2.adapters.base import LLMRequest, LLMResponse
        from claw_v2.main import build_runtime

        def fake(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "DAILY_COST_LIMIT": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(ValueError):
                    build_runtime(anthropic_executor=fake)

    def test_negative_rejected(self) -> None:
        import os
        from claw_v2.adapters.base import LLMRequest, LLMResponse
        from claw_v2.main import build_runtime

        def fake(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "DAILY_COST_LIMIT": "-1.5",
            }
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(ValueError):
                    build_runtime(anthropic_executor=fake)


class AutonomousMaintenanceFlagTests(unittest.TestCase):
    def test_maintenance_disabled_skips_noisy_jobs(self) -> None:
        import os
        from claw_v2.adapters.base import LLMRequest, LLMResponse
        from claw_v2.main import build_runtime

        def fake(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake)
                self.assertFalse(runtime.config.autonomous_maintenance_enabled)

                jobs = {job.name: job for job in runtime.scheduler.list_jobs()}
                # Run every noisy job and check skip event.
                for name in (
                    "perf_optimizer",
                    "wiki_research",
                    "wiki_scrape",
                    "skill_expand",
                    "auto_dream",
                    "learning_soul_suggestions",
                    "self_improve",
                ):
                    if name not in jobs:
                        continue
                    jobs[name].handler()

                events = runtime.observe.recent_events(limit=80)
                skipped = {
                    e["payload"]["job"]
                    for e in events
                    if e["event_type"] == "scheduled_job_skipped"
                    and e["payload"].get("reason") == "autonomous_maintenance_disabled"
                }
                # Heartbeat/pipeline_poll/buddy must NOT appear in skipped set
                self.assertNotIn("heartbeat", skipped)
                self.assertNotIn("pipeline_poll", skipped)
                self.assertNotIn("buddy_tick", skipped)
                # At least the maintenance jobs that exist were skipped.
                self.assertTrue(skipped, msg="expected some maintenance jobs to be skipped")


if __name__ == "__main__":
    unittest.main()
