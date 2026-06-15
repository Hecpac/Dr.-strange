"""Bloqueante review #1: ``_self_improve_handler`` must close over
``approvals``.

Before this fix, ``_setup_scheduler`` did NOT accept ``approvals`` as a
parameter, so the nested ``_self_improve_handler`` referenced a name
that was never bound in any enclosing scope. The reference compiled as
``LOAD_GLOBAL approvals`` and the daemon's first self_improve cron tick
would raise ``NameError`` — silently swallowed by ``_wrap_job_handler``
into a ``scheduled_job_error`` event with no operational signal that
the handler is permanently broken.

We pin three properties:
  1. ``_setup_scheduler`` declares ``approvals: ApprovalManager``.
  2. The compiled ``_self_improve_handler`` captures ``approvals`` as a
     free variable from the enclosing scope (proves the closure path).
  3. Invoking the registered self_improve cron handler does not produce a
     ``scheduled_job_error`` whose payload mentions ``NameError`` or
     ``approvals`` — i.e. the handler now reaches its real logic.
"""

from __future__ import annotations

import inspect
import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.approval import ApprovalManager
from claw_v2.main import _setup_scheduler


class SchedulerSelfImproveWiringTests(unittest.TestCase):
    def test_setup_scheduler_signature_includes_approvals(self) -> None:
        sig = inspect.signature(_setup_scheduler)
        self.assertIn(
            "approvals",
            sig.parameters,
            "approvals must be a keyword parameter of _setup_scheduler so that the "
            "nested _self_improve_handler can call should_pause_self_improve(approvals, ...).",
        )
        # `from __future__ import annotations` keeps annotations as strings;
        # resolve via get_type_hints so we compare the actual class.
        hints = typing.get_type_hints(_setup_scheduler)
        self.assertEqual(
            hints.get("approvals"),
            ApprovalManager,
            "approvals must be typed ApprovalManager",
        )

    def test_self_improve_handler_closure_captures_approvals(self) -> None:
        """Pre-fix the handler's ``co_freevars`` does NOT contain
        ``approvals`` (it became a LOAD_GLOBAL that fails at runtime).
        Post-fix it captures ``approvals`` from the enclosing scope."""
        handler_code: types.CodeType | None = None
        for const in _setup_scheduler.__code__.co_consts:
            if isinstance(const, types.CodeType) and const.co_name == "_self_improve_handler":
                handler_code = const
                break
        self.assertIsNotNone(handler_code, "_self_improve_handler nested code not found")
        assert handler_code is not None  # for type-checker
        self.assertIn(
            "approvals",
            handler_code.co_freevars,
            "_self_improve_handler does not capture `approvals` from the enclosing "
            "scope — _setup_scheduler must accept approvals and the closure must "
            "reach should_pause_self_improve through the captured name.",
        )

    def test_invoking_registered_self_improve_does_not_NameError(self) -> None:
        """Drive _setup_scheduler with minimal mocks, capture the
        registered self_improve cron job, invoke its (wrapped) handler,
        and assert no ``scheduled_job_error`` with NameError leaks.
        Pre-fix: ``approvals`` UnboundLocal → wrap-handler emits a
        scheduled_job_error mentioning NameError. Post-fix: handler runs
        through and either completes or hits a benign early-return."""
        captured_jobs: dict[str, object] = {}

        class _FakeScheduler:
            def __init__(self, **_: object) -> None:
                pass

            def register(self, job: object) -> None:
                captured_jobs[getattr(job, "name", "")] = job

            def restore(self) -> None:  # exercised by _setup_scheduler post-register
                return None

            def start(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            approvals = ApprovalManager(tmp / "approvals", secret="testsecret")

            # Patch CronScheduler so we can capture registered jobs without
            # bringing up persistence threads.
            with (
                patch("claw_v2.main.CronScheduler", _FakeScheduler),
                patch(
                    "claw_v2.main._resolve_pytest_command",
                    return_value=(["true"], None),
                ),
                patch("claw_v2.main.subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                config = MagicMock()
                config.eval_on_self_improve = True
                config.autonomous_maintenance_enabled = True
                config.pipeline_repo_root = tmp
                config.workspace_root = tmp
                config.self_improve_test_timeout_seconds = 30
                config.daily_metrics_enabled = False

                # Capture all observe.emit calls so we can inspect the error
                # bucket after invocation.
                emits: list[tuple[str, dict]] = []
                observe = MagicMock()

                def _capture_emit(event_type: str, *, payload: dict | None = None, **_kw) -> None:
                    emits.append((event_type, payload or {}))

                observe.emit.side_effect = _capture_emit

                # All other deps can be MagicMock — _setup_scheduler does
                # not run their methods during scheduler wiring.
                auto_research = MagicMock()
                auto_research.list_agents.return_value = ["self-improve"]
                auto_research.inspect.return_value = {"paused": False}
                auto_research.run_loop.return_value = MagicMock(
                    experiments_run=0, paused=False, reason="ok", last_metric=None
                )

                agent_store = MagicMock()
                # state_path(name).exists() must return True so create_agent
                # is skipped (it would call into the LLM stack otherwise).
                agent_store.state_path.return_value.exists.return_value = True

                startup_health = MagicMock()
                startup_health.degraded_capabilities.return_value = {}

                _setup_scheduler(
                    config=config,
                    system_prompt="x",
                    memory=MagicMock(),
                    observe=observe,
                    metrics=MagicMock(),
                    heartbeat=MagicMock(),
                    kairos=MagicMock(),
                    buddy=MagicMock(),
                    auto_research=auto_research,
                    agent_store=agent_store,
                    learning=MagicMock(),
                    router=MagicMock(),
                    task_board=MagicMock(),
                    sub_agents=MagicMock(),
                    bot=MagicMock(),
                    task_ledger=MagicMock(),
                    pipeline=MagicMock(),
                    startup_health=startup_health,
                    approvals=approvals,
                )

                self.assertIn("self_improve", captured_jobs)
                job = captured_jobs["self_improve"]
                # Invoke the wrapped handler — pre-fix this catches a NameError
                # inside _wrap_job_handler and emits scheduled_job_error.
                job.handler()  # type: ignore[attr-defined]

            errors = [
                payload for event_type, payload in emits if event_type == "scheduled_job_error"
            ]
            offending = [e for e in errors if "NameError" in str(e) or "approvals" in str(e)]
            self.assertEqual(
                offending,
                [],
                f"self_improve handler raised NameError/approvals after fix: {offending!r}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
