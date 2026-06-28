"""P0-2: the live daemon must DETECT when its shared checkout has been
stranded on a wrong branch and stop claiming jobs (safe mode), without ever
mutating git or tripping on a non-affirmative reading.

The crux is the false-positive surface: a wrong trip is a self-inflicted
outage. These tests pin the affirmative-only trip semantics (fail OPEN on
detached HEAD, in-progress rebase/bisect, and any HEAD-read error) and the
in-process claim-block wiring.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.cron import CronScheduler
from claw_v2.daemon import BRANCH_INTEGRITY_CLAIM_BLOCK_REASON, ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService

_DETACHED_SHA = "0123456789abcdef0123456789abcdef01234567"


def _heartbeat_stub() -> MagicMock:
    hb = MagicMock()
    hb.collect.return_value = HeartbeatSnapshot(
        timestamp="2026-01-01T00:00:00",
        pending_approvals=0,
        pending_approval_ids=[],
        agents={},
        lane_metrics={},
    )
    return hb


def _write_main_checkout(root: Path, head: str) -> None:
    """Write a normal (non-worktree) ``.git/HEAD`` — what the production
    daemon reads when it runs from the main checkout."""
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head, encoding="utf-8")


def _enqueue_one(jobs: JobService, kind: str = "k") -> str:
    return jobs.enqueue(kind=kind, payload={"x": 1}).job_id


class _BranchIntegrityBase(unittest.TestCase):
    def _daemon(
        self,
        repo_root: Path,
        *,
        job_service: JobService | None = None,
        observe: MagicMock | None = None,
        enabled: bool = True,
        **kw,
    ) -> tuple[ClawDaemon, MagicMock]:
        observe = observe if observe is not None else MagicMock()
        daemon = ClawDaemon(
            scheduler=CronScheduler(),
            heartbeat=_heartbeat_stub(),
            observe=observe,
            job_service=job_service,
            branch_integrity_check_enabled=enabled,
            repo_root=repo_root,
            **kw,
        )
        return daemon, observe

    @staticmethod
    def _emitted(observe: MagicMock, name: str) -> list[dict]:
        return [
            c.kwargs.get("payload", {}) for c in observe.emit.call_args_list if c.args[0] == name
        ]


class WrongBranchTripsTests(_BranchIntegrityBase):
    def test_wrong_branch_trips_safe_mode_and_emits_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            jobs = JobService(root / "claw.db", observe=MagicMock())
            job_id = _enqueue_one(jobs)
            # Same observe sink for daemon + job_service so the claim-block
            # event reason is assertable.
            observe = MagicMock()
            jobs.observe = observe
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe)

            daemon.tick(now=1_000_000)

            violations = self._emitted(observe, "daemon_branch_integrity_violation")
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0], {"expected": "main", "actual": "feature-x"})

            # Safe mode is wired into the actual claim site: the queued job is
            # NOT claimable while stranded.
            claimed = jobs.claim_next(worker_id="w", kinds=("k",))
            self.assertIsNone(claimed)
            blocked = self._emitted(observe, "job_claim_blocked")
            self.assertTrue(blocked)
            self.assertEqual(blocked[-1]["reason"], BRANCH_INTEGRITY_CLAIM_BLOCK_REASON)
            # Job still present and unclaimed (detection, never mutation).
            self.assertEqual(jobs.get(job_id).status, "queued")

    def test_branch_name_with_slash_is_preserved(self) -> None:
        # A namespaced branch (fix/daemon-branch-integrity) must compare whole,
        # not just the last path segment.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/fix/daemon-branch-integrity\n")
            daemon, observe = self._daemon(root)
            daemon.tick(now=1_000_000)
            violations = self._emitted(observe, "daemon_branch_integrity_violation")
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0]["actual"], "fix/daemon-branch-integrity")

    def test_worktree_gitdir_pointer_is_resolved(self) -> None:
        # A worktree's .git is a FILE ('gitdir: <path>'); the real HEAD lives
        # in the per-worktree git dir. The resolver must follow it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_git = Path(tmp) / "real-git" / "worktrees" / "wt"
            real_git.mkdir(parents=True, exist_ok=True)
            (real_git / "HEAD").write_text("ref: refs/heads/stray\n", encoding="utf-8")
            (root / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
            daemon, observe = self._daemon(root)
            daemon.tick(now=1_000_000)
            violations = self._emitted(observe, "daemon_branch_integrity_violation")
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0]["actual"], "stray")

    def test_expected_branch_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/main\n")
            with patch.dict(os.environ, {"CLAW_EXPECTED_BRANCH": "release"}, clear=False):
                daemon, observe = self._daemon(root)
            daemon.tick(now=1_000_000)
            violations = self._emitted(observe, "daemon_branch_integrity_violation")
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0], {"expected": "release", "actual": "main"})


class OnMainNoTripTests(_BranchIntegrityBase):
    def test_on_main_allows_normal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/main\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe)

            daemon.tick(now=1_000_000)

            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            claimed = jobs.claim_next(worker_id="w", kinds=("k",))
            self.assertIsNotNone(claimed)

    def test_deploy_ff_keeps_head_on_main_does_not_trip(self) -> None:
        # A fast-forward deploy advances the working tree/commit but leaves
        # HEAD as 'ref: refs/heads/main'. P0-2 is BRANCH-ONLY, so a deploy
        # that stays on main must never trip — even though the checkout is
        # legitimately "dirty"/changed mid-deploy.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/main\n")
            # Mimic deploy churn that a dirty/revert detector would react to;
            # the branch check ignores it by construction.
            (root / ".git" / "ORIG_HEAD").write_text(_DETACHED_SHA + "\n", encoding="utf-8")
            (root / "reverted_file.py").write_text("# stale\n", encoding="utf-8")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe)

            daemon.tick(now=1_000_000)

            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))


class FailOpenTests(_BranchIntegrityBase):
    def test_head_read_error_fails_open(self) -> None:
        # No .git at all -> HEAD read raises -> FAIL OPEN: no trip, claim works.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe)

            daemon.tick(now=1_000_000)

            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))

    def test_detached_head_fails_open(self) -> None:
        # HEAD is a raw SHA (not 'ref: refs/heads/...') -> indeterminate -> no trip.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, _DETACHED_SHA + "\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe)

            daemon.tick(now=1_000_000)

            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))

    def test_rebase_in_progress_fails_open_even_with_branch_ref(self) -> None:
        # During a rebase a 'rebase-merge' marker exists; HEAD may transiently
        # read as a branch ref. Treat in-progress git ops as indeterminate so
        # the daemon never trips on a mid-rebase snapshot.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            (root / ".git" / "rebase-merge").mkdir()
            daemon, observe = self._daemon(root)
            daemon.tick(now=1_000_000)
            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            self.assertFalse(daemon._branch_integrity_safe_mode)


class LatchAndRecoveryTests(_BranchIntegrityBase):
    def test_recovery_to_main_clears_safe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(
                root, job_service=jobs, observe=observe, branch_integrity_interval=300.0
            )

            daemon.tick(now=1_000_000)
            self.assertTrue(daemon._branch_integrity_safe_mode)
            self.assertIsNone(jobs.claim_next(worker_id="w", kinds=("k",)))

            # Operator runs `git checkout main`; next throttled check recovers.
            _write_main_checkout(root, "ref: refs/heads/main\n")
            daemon.tick(now=1_000_400)

            self.assertFalse(daemon._branch_integrity_safe_mode)
            self.assertEqual(len(self._emitted(observe, "daemon_branch_integrity_restored")), 1)
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))

    def test_transient_error_does_not_clear_confirmed_trip(self) -> None:
        # A confirmed wrong-branch trip must NOT be un-latched by a later
        # non-affirmative reading (e.g. a transient HEAD-read error). Only an
        # affirmative on-main reading recovers.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(
                root, job_service=jobs, observe=observe, branch_integrity_interval=300.0
            )

            daemon.tick(now=1_000_000)
            self.assertTrue(daemon._branch_integrity_safe_mode)

            # HEAD becomes unreadable (transient) -> fail open, but the trip stays.
            (root / ".git" / "HEAD").unlink()
            daemon.tick(now=1_000_400)

            self.assertTrue(daemon._branch_integrity_safe_mode)
            self.assertIsNone(jobs.claim_next(worker_id="w", kinds=("k",)))


class CadenceTests(_BranchIntegrityBase):
    def test_per_tick_throttle_skips_within_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            daemon, observe = self._daemon(root, branch_integrity_interval=300.0)

            # First tick (startup, _last=0.0) runs the check -> trips.
            daemon.tick(now=1_000_000)
            self.assertEqual(len(self._emitted(observe, "daemon_branch_integrity_violation")), 1)

            # Within the interval the HEAD flips to main, but the throttled
            # check does NOT re-read -> still tripped, no new events.
            _write_main_checkout(root, "ref: refs/heads/main\n")
            daemon.tick(now=1_000_100)
            self.assertTrue(daemon._branch_integrity_safe_mode)
            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_restored"), [])

            # After the interval it re-checks and recovers.
            daemon.tick(now=1_000_400)
            self.assertFalse(daemon._branch_integrity_safe_mode)

    def test_startup_check_runs_on_first_tick(self) -> None:
        # _last_branch_integrity_check_at starts at 0.0, so the very first tick
        # (post-boot) performs the check -- this is the STARTUP guard. The
        # incident strand happened post-boot, so per-tick re-checks are what
        # matter, but startup must not be a blind spot either.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            daemon, observe = self._daemon(root)
            self.assertEqual(daemon._last_branch_integrity_check_at, 0.0)
            daemon.tick(now=1_000_000)
            self.assertTrue(daemon._branch_integrity_safe_mode)


class DisabledByDefaultTests(_BranchIntegrityBase):
    def test_disabled_constructor_default_never_checks(self) -> None:
        # The constructor default is OFF so every existing synthetic daemon in
        # the suite (built from a feature-branch worktree) is unaffected.
        # Production opts in explicitly via main.py.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon, observe = self._daemon(root, job_service=jobs, observe=observe, enabled=False)

            daemon.tick(now=1_000_000)

            self.assertEqual(self._emitted(observe, "daemon_branch_integrity_violation"), [])
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))


class JobServiceSafeModeWiringTests(unittest.TestCase):
    def test_safe_mode_reason_blocks_every_claim_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observe = MagicMock()
            jobs = JobService(Path(tmp) / "claw.db", observe=observe)
            queued = jobs.enqueue(kind="k", payload={})

            jobs.set_safe_mode_reason(BRANCH_INTEGRITY_CLAIM_BLOCK_REASON)
            self.assertIsNone(jobs.claim_next(worker_id="w", kinds=("k",)))
            self.assertIsNone(jobs.claim(queued.job_id, worker_id="w"))
            self.assertIsNone(jobs.acquire_lease(queued.job_id, worker_id="w"))
            self.assertIsNone(jobs.acquire_next_lease(worker_id="w", kinds=("k",)))

            # Clearing the latch restores claims.
            jobs.set_safe_mode_reason(None)
            self.assertIsNotNone(jobs.claim_next(worker_id="w", kinds=("k",)))

    def test_in_process_reason_takes_precedence_over_env_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observe = MagicMock()
            jobs = JobService(Path(tmp) / "claw.db", observe=observe)
            jobs.enqueue(kind="k", payload={})
            jobs.set_safe_mode_reason(BRANCH_INTEGRITY_CLAIM_BLOCK_REASON)
            with patch.dict(os.environ, {"CLAW_MAINTENANCE_MODE": "1"}, clear=False):
                self.assertIsNone(jobs.claim_next(worker_id="w", kinds=("k",)))
                blocked = [
                    c.kwargs["payload"]
                    for c in observe.emit.call_args_list
                    if c.args[0] == "job_claim_blocked"
                ]
                self.assertEqual(blocked[-1]["reason"], BRANCH_INTEGRITY_CLAIM_BLOCK_REASON)


class RunLoopStartupCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_loop_trips_safe_mode_before_spawning_claim_loops(self) -> None:
        # The startup check runs synchronously at the top of run_loop, BEFORE
        # any claim loop is spawned, so a boot-stranded daemon enters safe mode
        # before a worker can claim. shutdown is pre-set so the while loop never
        # runs -- only the pre-spawn startup check executes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_main_checkout(root, "ref: refs/heads/feature-x\n")
            observe = MagicMock()
            jobs = JobService(root / "claw.db", observe=observe)
            _enqueue_one(jobs)
            daemon = ClawDaemon(
                scheduler=CronScheduler(),
                heartbeat=_heartbeat_stub(),
                observe=observe,
                job_service=jobs,
                branch_integrity_check_enabled=True,
                repo_root=root,
            )
            shutdown = asyncio.Event()
            shutdown.set()
            await daemon.run_loop(shutdown, interval=0.01)
            self.assertTrue(daemon._branch_integrity_safe_mode)
            self.assertIsNone(jobs.claim_next(worker_id="w", kinds=("k",)))


class ProductionWiringTests(unittest.TestCase):
    def test_main_wires_branch_integrity_enablement_helper(self) -> None:
        # Default-OFF at the constructor is only safe because production turns it
        # ON. If this regresses, P0-2 ships dead -- exactly the undetected-failure
        # mode it exists to prevent.
        source = (Path(__file__).resolve().parents[1] / "claw_v2" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("branch_integrity_check_enabled=_branch_integrity_check_enabled(", source)

    def test_enablement_helper_arms_only_a_real_main_checkout(self) -> None:
        from claw_v2.main import _branch_integrity_check_enabled

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Worktree-style: .git is a FILE -> OFF (this is why the whole test
            # suite's build_runtime daemons never trip on the feature branch).
            (root / ".git").write_text("gitdir: /somewhere\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAW_BRANCH_INTEGRITY_CHECK", None)
                self.assertFalse(_branch_integrity_check_enabled(root))

            # Production main checkout: .git is a DIRECTORY -> ON by default.
            (root / ".git").unlink()
            (root / ".git").mkdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAW_BRANCH_INTEGRITY_CHECK", None)
                self.assertTrue(_branch_integrity_check_enabled(root))

            # Ops kill-switch wins even on a real checkout.
            with patch.dict(os.environ, {"CLAW_BRANCH_INTEGRITY_CHECK": "0"}, clear=False):
                self.assertFalse(_branch_integrity_check_enabled(root))


if __name__ == "__main__":
    unittest.main()
