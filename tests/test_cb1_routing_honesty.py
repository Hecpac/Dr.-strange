from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.adapters.anthropic_hooks import (
    _BROWSER_DRIVE_NUDGE,
    _BROWSER_DRIVE_REASON,
    _COMPUTER_USE_DRIVE_NUDGE,
    _COMPUTER_USE_DRIVE_REASON,
    _inline_browser_drive_reason,
)
from claw_v2.bot_helpers import (
    _build_coordinator_tasks,
    _looks_like_desktop_gui_objective,
)
from claw_v2.computer_handler import (
    MISSING_DOMAIN_GRANT_DELEGATED_MSG,
    MISSING_DOMAIN_GRANT_INTERACTIVE_MSG,
)
from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.task_handler import _NO_DESKTOP_LANE_BLOCKER, TaskHandler
from claw_v2.task_ledger import TaskLedger

# CB1 (ADR CB0, 2026-07-07): computer-use routing honesty. Locks the three
# outcomes the ADR requires for a computer-use / desktop-GUI ask:
#   1. browser-signal objectives keep their VALID executor (browser executor);
#   2. an unambiguous delegated desktop-GUI objective is DECLINED synchronously
#      with a user-safe blocker + a telemetry reason — never silently landed in
#      the GUI-less Codex coordinator;
#   3. no prompt surface (DELEGATION_CONTRACT, ops coordinator flavor, PreToolUse
#      backstop nudge) advertises a delegated desktop executor, because none
#      exists (cb0_computer_use_has_no_delegation_home).


def _event_payload(event: dict) -> dict:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


class _NeverRunCoordinator:
    """Coordinator stub for declined objectives: run() must never be reached."""

    def __init__(self) -> None:
        self.ran = threading.Event()

    def run(self, task_id, objective, research_tasks, **kwargs):
        self.ran.set()
        raise AssertionError("coordinator must not run for a declined desktop objective")


class _ReleasingCoordinator:
    """Minimal happy-path coordinator (mirrors test_task_handler's stub)."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def run(
        self,
        task_id,
        objective,
        research_tasks,
        implementation_tasks=None,
        verification_tasks=None,
        lane_overrides=None,
        **kwargs,
    ):
        self.started.set()
        return CoordinatorResult(
            task_id=task_id,
            phase_results={
                "verification": [
                    WorkerResult(
                        task_name="verify_operation",
                        content="Verification Status: passed",
                        duration_seconds=0.1,
                    )
                ]
            },
            synthesis="done",
        )


class DesktopGuiObjectiveRecognizerTests(unittest.TestCase):
    def test_unambiguous_desktop_objectives_match(self) -> None:
        for objective in (
            "usa computer-use para abrir la Calculadora",
            "usa computer use para abrir la Calculadora",
            "abre la app Calculadora y dime qué ves",
            "usa el escritorio para abrir Notas y escribir un recordatorio",
            "toma un screenshot del escritorio",
            "open the app Notes on the desktop and read the first note",
        ):
            self.assertTrue(_looks_like_desktop_gui_objective(objective), objective)

    def test_non_desktop_objectives_fall_through(self) -> None:
        # A miss must fall through to today's behavior (coordinator), never to
        # a false decline: filesystem/terminal ops are coordinator-executable.
        for objective in (
            "guarda el archivo en el escritorio",
            "escribe un archivo de notas en el escritorio",
            "corre el script de backup y reporta el resultado",
            "publica el post con el resumen de hoy",
            "draft a launch plan for the product",
        ):
            self.assertFalse(_looks_like_desktop_gui_objective(objective), objective)


class DesktopDelegationGuardTests(unittest.TestCase):
    def _handler(self, coordinator, root: Path) -> tuple[TaskHandler, MemoryStore, ObserveStream]:
        memory = MemoryStore(root / "claw.db")
        observe = ObserveStream(root / "observe.db")
        ledger = TaskLedger(root / "claw.db", observe=observe)
        jobs = JobService(root / "claw.db", observe=observe)
        handler = TaskHandler(
            coordinator=coordinator,
            observe=observe,
            task_ledger=ledger,
            job_service=jobs,
            get_session_state=memory.get_session_state,
            update_session_state=memory.update_session_state,
            workspace_root=root,
        )
        return handler, memory, observe

    def test_delegated_desktop_objective_is_declined_with_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _NeverRunCoordinator()
            handler, memory, observe = self._handler(coordinator, root)

            reply = handler.start_autonomous_task(
                "s1", "usa computer-use para abrir la Calculadora", mode="ops"
            )

            self.assertEqual(reply, _NO_DESKTOP_LANE_BLOCKER)
            self.assertIn("/computer", reply)
            events = observe.recent_events(limit=20)
            event_types = [event["event_type"] for event in events]
            self.assertIn("delegated_desktop_objective_blocked", event_types)
            self.assertNotIn("autonomous_task_started", event_types)
            blocked = next(
                event
                for event in events
                if event["event_type"] == "delegated_desktop_objective_blocked"
            )
            payload = _event_payload(blocked)
            self.assertEqual(payload.get("reason"), "no_desktop_delegation_lane")
            self.assertEqual(payload.get("mode"), "ops")
            self.assertFalse(coordinator.ran.is_set())
            # No task state was created for the declined objective.
            state = memory.get_session_state("s1")
            active = (state.get("active_object") or {}).get("active_task")
            self.assertIsNone(active)

    def test_app_launch_phrasing_is_declined_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _NeverRunCoordinator()
            handler, _memory, observe = self._handler(coordinator, root)

            reply = handler.start_autonomous_task(
                "s1",
                "abre la app Calculadora del escritorio y toma un screenshot",
                mode="ops",
            )

            self.assertEqual(reply, _NO_DESKTOP_LANE_BLOCKER)
            self.assertFalse(coordinator.ran.is_set())
            event_types = [event["event_type"] for event in observe.recent_events(limit=20)]
            self.assertIn("delegated_desktop_objective_blocked", event_types)

    def test_terminal_ops_objective_still_starts_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _ReleasingCoordinator()
            handler, _memory, observe = self._handler(coordinator, root)

            reply = handler.start_autonomous_task(
                "s1", "corre el script de backup y reporta el resultado", mode="ops"
            )

            self.assertIn("Tarea autónoma iniciada", reply)
            self.assertTrue(coordinator.started.wait(timeout=2))
            event_types = [event["event_type"] for event in observe.recent_events(limit=30)]
            self.assertIn("autonomous_task_started", event_types)
            self.assertNotIn("delegated_desktop_objective_blocked", event_types)
            task_id = reply.split("`", 2)[1]
            handler.wait_for_task(task_id, timeout=5)

    def test_guard_skips_browser_signal_and_non_ops_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            handler, _memory, _observe = self._handler(_NeverRunCoordinator(), root)

            # Browser signal keeps its valid browser-executor route (outcome 1).
            self.assertFalse(
                handler._reject_desktop_objective_without_lane(
                    "navega a x.com y publica el post",
                    session_id="s1",
                    mode="ops",
                    source="test",
                )
            )
            # browse mode always belongs to the browser executor.
            self.assertFalse(
                handler._reject_desktop_objective_without_lane(
                    "usa computer-use para abrir la Calculadora",
                    session_id="s1",
                    mode="browse",
                    source="test",
                )
            )
            # Guard is scoped to the modes whose prompts used to claim desktop.
            self.assertFalse(
                handler._reject_desktop_objective_without_lane(
                    "usa computer-use para abrir la Calculadora",
                    session_id="s1",
                    mode="research",
                    source="test",
                )
            )
            self.assertTrue(
                handler._reject_desktop_objective_without_lane(
                    "usa computer-use para abrir la Calculadora",
                    session_id="s1",
                    mode="publish",
                    source="test",
                )
            )


class PromptSurfaceHonestyTests(unittest.TestCase):
    def test_delegation_contract_does_not_advertise_desktop_delegation(self) -> None:
        from claw_v2.brain import DELEGATION_CONTRACT

        self.assertNotIn("Computer-use or desktop-GUI control", DELEGATION_CONTRACT)
        self.assertNotIn("desktop/terminal automation", DELEGATION_CONTRACT)
        self.assertNotIn("drives Chrome/CDP or computer-use", DELEGATION_CONTRACT)
        self.assertIn("NO delegated lane", DELEGATION_CONTRACT)
        self.assertIn("/computer", DELEGATION_CONTRACT)
        self.assertIn("`ops` (terminal automation", DELEGATION_CONTRACT)

    def test_ops_coordinator_flavor_does_not_claim_desktop_automation(self) -> None:
        _research, implementation, _verification = _build_coordinator_tasks(
            "ops", "corre el script de backup y reporta el resultado"
        )
        self.assertIsNotNone(implementation)
        instruction = implementation[0].instruction
        self.assertNotIn("desktop/computer automation", instruction)
        self.assertIn("shell scripts and local CLIs", instruction)

    def test_backstop_reason_splits_computer_use_from_browser(self) -> None:
        self.assertEqual(
            _inline_browser_drive_reason("Bash", {"command": "python3 -m computer_use --demo"}),
            _COMPUTER_USE_DRIVE_REASON,
        )
        self.assertEqual(
            _inline_browser_drive_reason(
                "Bash", {"command": "npx playwright open https://instagram.com"}
            ),
            _BROWSER_DRIVE_REASON,
        )

    def test_backstop_nudges_are_honest_per_class(self) -> None:
        # Browser drive IS delegable: the nudge instructs delegation.
        self.assertIn("Delegate it", _BROWSER_DRIVE_NUDGE)
        # Desktop computer-use is NOT: the nudge must never instruct delegating
        # the work; it names the inline computer tools and the honest refusal.
        self.assertNotIn("Delegate it", _COMPUTER_USE_DRIVE_NUDGE)
        self.assertIn("inline with the computer tools", _COMPUTER_USE_DRIVE_NUDGE)
        self.assertIn("delegate_task refuses", _COMPUTER_USE_DRIVE_NUDGE)


class MissingDomainGrantGuidanceTests(unittest.TestCase):
    def test_delegated_message_names_cause_and_action(self) -> None:
        # Contract kept: tests elsewhere assert this exact cause phrase.
        self.assertIn("falta un dominio aprobado", MISSING_DOMAIN_GRANT_DELEGATED_MSG)
        # Actionable: tells the user the lever (put the site/URL in the objective).
        self.assertIn("Reintenta la delegación", MISSING_DOMAIN_GRANT_DELEGATED_MSG)
        self.assertIn("URL", MISSING_DOMAIN_GRANT_DELEGATED_MSG)
        self.assertIn("ejemplo.com", MISSING_DOMAIN_GRANT_DELEGATED_MSG)

    def test_interactive_message_names_cause_and_action(self) -> None:
        self.assertIn("no menciona ningún", MISSING_DOMAIN_GRANT_INTERACTIVE_MSG)
        self.assertIn("Reenvía la instrucción", MISSING_DOMAIN_GRANT_INTERACTIVE_MSG)
        self.assertIn("instagram.com", MISSING_DOMAIN_GRANT_INTERACTIVE_MSG)
        # It must not be the old vague "needs approval" copy with no lever.
        self.assertNotIn("needs approval", MISSING_DOMAIN_GRANT_INTERACTIVE_MSG)


if __name__ == "__main__":
    unittest.main()
