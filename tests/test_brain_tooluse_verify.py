from claw_v2.bot_helpers import _extract_verification_status, verify_brain_tooluse
from claw_v2.coordinator import WorkerResult


class _FakeCoordinator:
    def __init__(self, content: str = "", error: str = "") -> None:
        self._content = content
        self._error = error
        self.captured = None

    def _dispatch_parallel(self, tasks, trace_context=None, *, lane_overrides=None):
        self.captured = tasks
        return [
            WorkerResult(
                task_name=tasks[0].name,
                content=self._content,
                duration_seconds=0.0,
                error=self._error,
            )
        ]


def test_parser_exact_line_backcompat():
    assert _extract_verification_status("Verification Status: passed") == "passed"


def test_parser_markdown_and_trailing():
    assert _extract_verification_status("**Verification Status:** passed.") == "passed"


def test_parser_dash_separator_failed():
    assert _extract_verification_status("- verification status — failed") == "failed"


def test_parser_spanish_ok():
    assert _extract_verification_status("Verificado: ok") == "passed"


def test_parser_pending_prose():
    assert _extract_verification_status("Verification status pending") == "pending"


def test_parser_no_status_returns_none():
    assert _extract_verification_status("all good, the task is done") is None


def test_parser_does_not_match_prose_with_words_between():
    assert _extract_verification_status("I verified that the files passed lint") is None


def test_verify_passed():
    coordinator = _FakeCoordinator(content="Review.\nVerification Status: passed")

    assert (
        verify_brain_tooluse(
            coordinator,
            task_id="t",
            objective="o",
            files_written=["a.py"],
            commands_run=[],
        )
        == "passed"
    )


def test_verify_failed():
    coordinator = _FakeCoordinator(content="Verification Status: failed")

    assert (
        verify_brain_tooluse(
            coordinator,
            task_id="t",
            objective="o",
            files_written=[],
            commands_run=["pytest"],
        )
        == "failed"
    )


def test_verify_no_status_defaults_pending():
    coordinator = _FakeCoordinator(content="looks fine to me")

    assert (
        verify_brain_tooluse(
            coordinator,
            task_id="t",
            objective="o",
            files_written=["a.py"],
            commands_run=[],
        )
        == "pending"
    )


def test_verify_error_defaults_pending():
    coordinator = _FakeCoordinator(content="", error="boom")

    assert (
        verify_brain_tooluse(
            coordinator,
            task_id="t",
            objective="o",
            files_written=["a.py"],
            commands_run=[],
        )
        == "pending"
    )


def test_verify_dispatch_raises_defaults_pending():
    class _Boom:
        def _dispatch_parallel(self, tasks, trace_context=None, *, lane_overrides=None):
            raise RuntimeError("dispatch down")

    assert (
        verify_brain_tooluse(
            _Boom(),
            task_id="t",
            objective="o",
            files_written=["a.py"],
            commands_run=[],
        )
        == "pending"
    )


def test_verify_task_is_verifier_lane_and_carries_artifacts():
    coordinator = _FakeCoordinator(content="Verification Status: passed")

    verify_brain_tooluse(
        coordinator,
        task_id="t",
        objective="ship X",
        files_written=["claw_v2/bot.py"],
        commands_run=["pytest -q"],
    )

    task = coordinator.captured[0]
    assert task.lane == "verifier"
    assert "claw_v2/bot.py" in task.instruction
    assert "pytest -q" in task.instruction
