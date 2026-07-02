"""C1-Sβ — re-drive acotado (invariante task_redrive_bounded_and_classified).

Un blocker clase `formato` con tail estructurado re-conduce la tarea
(start_phase=synthesis) en vez de matarla; N≤CLAW_MAX_TASK_REDRIVES, mismo
ident jamás 2×, decision_usuario jamás consume intentos, attempt persistido
en active_task ANTES del enqueue, ventana congelada ⇒ no re-drive.
Diseño: memoria autonomy-beta-gamma-design-2026-07-02.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.bot_helpers import (
    _coordinator_checkpoint,
    normalize_blocker_ident,
    parse_verdict_tail,
)
from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.task_handler import TaskHandler, _failure_response_text
from claw_v2.task_ledger import TaskLedger

FORMATO_TAIL = (
    "Revisión operativa: el contenido es correcto pero el formato no cumple lo pedido.\n"
    "Verification Status: pending\n"
    "Siguiente paso: corregir el formato del entregable\n"
    "CLASE_BLOCKER: formato\n"
    "BLOCKERS:\n"
    "- formato-3-lineas: el entregable debe ser exactamente 3 líneas\n"
)

EVIDENCIA_TAIL = (
    "Verification Status: pending\n"
    "Siguiente paso: adjuntar la cita textual del man page\n"
    "CLASE_BLOCKER: evidencia_externa\n"
    "BLOCKERS:\n"
    "- cita-man-page: falta el output crudo de man launchd.plist\n"
)

DECISION_TAIL = (
    "Verification Status: pending\n"
    "Siguiente paso: confirmar con el dueño qué variante prefiere\n"
    "CLASE_BLOCKER: decision_usuario\n"
    "BLOCKERS:\n"
    "- confirmar-variante: el dueño debe elegir entre A y B\n"
)


class ParseVerdictTailTests(unittest.TestCase):
    def test_formato_tail(self) -> None:
        tail = parse_verdict_tail(FORMATO_TAIL)
        assert tail is not None
        self.assertEqual(tail.clase, "formato")
        self.assertEqual(tail.blockers[0][0], "formato-3-lineas")

    def test_evidencia_tail(self) -> None:
        tail = parse_verdict_tail(EVIDENCIA_TAIL)
        assert tail is not None
        self.assertEqual(tail.clase, "evidencia_externa")

    def test_decision_tail(self) -> None:
        tail = parse_verdict_tail(DECISION_TAIL)
        assert tail is not None
        self.assertEqual(tail.clase, "decision_usuario")

    def test_ninguna_allows_empty_blockers(self) -> None:
        tail = parse_verdict_tail(
            "Verification Status: passed\nCLASE_BLOCKER: ninguna\nBLOCKERS:\n"
        )
        assert tail is not None
        self.assertEqual(tail.clase, "ninguna")
        self.assertEqual(tail.blockers, ())

    def test_missing_class_returns_none(self) -> None:
        self.assertIsNone(parse_verdict_tail("Verification Status: pending\nSiguiente paso: x"))

    def test_class_without_blockers_returns_none(self) -> None:
        # fail-closed: una clase re-conducible sin blockers no es accionable.
        self.assertIsNone(parse_verdict_tail("CLASE_BLOCKER: formato\nBLOCKERS:\n"))

    def test_garbage_and_empty_return_none(self) -> None:
        self.assertIsNone(parse_verdict_tail(""))
        self.assertIsNone(parse_verdict_tail(None))  # type: ignore[arg-type]
        self.assertIsNone(parse_verdict_tail("texto libre sin contrato"))

    def test_last_class_line_wins(self) -> None:
        text = (
            "CLASE_BLOCKER: decision_usuario\nBLOCKERS:\n- a: b\n"
            "...revisión posterior...\n"
            "CLASE_BLOCKER: formato\nBLOCKERS:\n- c-d: arregla el formato\n"
        )
        tail = parse_verdict_tail(text)
        assert tail is not None
        self.assertEqual(tail.clase, "formato")
        self.assertEqual(tail.blockers[0][0], "c-d")

    def test_normalize_ident_stable(self) -> None:
        a = normalize_blocker_ident("formato", "Formato-3-Líneas")
        b = normalize_blocker_ident("formato", "formato-3-lineas")
        self.assertEqual(a, b)
        self.assertNotEqual(a, normalize_blocker_ident("evidencia_externa", "formato-3-lineas"))


class CheckpointBlockerFieldsTests(unittest.TestCase):
    def _result(self, content: str) -> CoordinatorResult:
        return CoordinatorResult(
            task_id="t1",
            phase_results={
                "verification": [
                    WorkerResult(task_name="verify_findings", content=content, duration_seconds=0.1)
                ]
            },
            synthesis="entrega",
        )

    def test_checkpoint_carries_blocker_class_and_slugs(self) -> None:
        checkpoint = _coordinator_checkpoint(self._result(FORMATO_TAIL), objective="obj")
        self.assertEqual(checkpoint.get("blocker_class"), "formato")
        self.assertIn("formato-3-lineas", (checkpoint.get("blockers") or [""])[0])

    def test_passed_checkpoint_has_no_blocker_class(self) -> None:
        checkpoint = _coordinator_checkpoint(
            self._result("Verification Status: passed\nCLASE_BLOCKER: ninguna\nBLOCKERS:\n"),
            objective="obj",
        )
        self.assertNotIn("blocker_class", checkpoint)


class _TailCoordinator:
    """Fake coordinator: devuelve un veredicto fijo y graba los kwargs de cada run."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def run(self, task_id, objective, research_tasks, **kwargs):
        self.calls.append({"objective": objective, **kwargs})
        return CoordinatorResult(
            task_id=task_id,
            phase_results={
                "verification": [
                    WorkerResult(
                        task_name="verify_findings", content=self.content, duration_seconds=0.1
                    )
                ]
            },
            synthesis="entrega inicial",
        )


def _mk_handler(root: Path, coordinator, frozen=None):
    memory = MemoryStore(root / "claw.db")
    observe = ObserveStream(root / "observe.db")
    ledger = TaskLedger(root / "claw.db", observe=observe)
    jobs = JobService(root / "claw.db", observe=observe)
    stored: list[tuple[str, str, str]] = []
    handler = TaskHandler(
        coordinator=coordinator,
        observe=observe,
        task_ledger=ledger,
        job_service=jobs,
        get_session_state=memory.get_session_state,
        update_session_state=memory.update_session_state,
        store_message=lambda sid, role, text: stored.append((sid, role, text)),
        workspace_root=root,
        redrive_budget_frozen=frozen,
    )
    return handler, memory, observe, jobs, stored


def _active_task(memory: MemoryStore, session_id: str) -> dict:
    state = memory.get_session_state(session_id)
    return dict((state.get("active_object") or {}).get("active_task") or {})


class RedriveIntegrationTests(unittest.TestCase):
    def test_formato_blocker_redrives_instead_of_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            handler, memory, observe, _jobs, stored = _mk_handler(
                root, _TailCoordinator(FORMATO_TAIL)
            )
            ack = handler.start_autonomous_task(
                "tg-1", "resume launchd en 3 líneas", mode="research"
            )
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=5))

            events = observe.recent_events(limit=200)
            types = [e["event_type"] for e in events]
            self.assertNotIn("autonomous_task_failed", types)
            decision = next(
                e for e in events if e["event_type"] == "autonomous_task_redrive_decision"
            )
            self.assertEqual(decision["payload"]["action"], "redrive")
            self.assertEqual(decision["payload"]["attempt"], 1)

            active = _active_task(memory, "tg-1")
            self.assertEqual(active.get("redrive_attempts"), 1)
            self.assertEqual(active.get("redrive_seen"), ["formato:formato-3-lineas"])
            pending = active.get("redrive_pending") or {}
            self.assertEqual(pending.get("start_phase"), "synthesis")
            self.assertIn("3 líneas", str(pending.get("verdict") or ""))

            assistant = [t for _s, r, t in stored if r == "assistant"]
            self.assertTrue(any("intento 1/" in t for t in assistant))

    def test_decision_usuario_terminal_immediate_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            handler, memory, observe, _jobs, _stored = _mk_handler(
                root, _TailCoordinator(DECISION_TAIL)
            )
            ack = handler.start_autonomous_task("tg-1", "elige la variante", mode="research")
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=5))

            events = observe.recent_events(limit=200)
            failed = next(e for e in events if e["event_type"] == "autonomous_task_failed")
            self.assertIn("/task_pending", failed["payload"]["response"])
            self.assertFalse(_active_task(memory, "tg-1").get("redrive_attempts"))

    def test_evidencia_pre_gamma_fails_closed_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            handler, _memory, observe, _jobs, _stored = _mk_handler(
                root, _TailCoordinator(EVIDENCIA_TAIL)
            )
            ack = handler.start_autonomous_task("tg-1", "cita el man page", mode="research")
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=5))
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("/task_pending", failed["payload"]["response"])


class RedriveDecisionUnitTests(unittest.TestCase):
    """_maybe_start_redrive por unidad: guards del governor."""

    def _handler_and_memory(self, root: Path, frozen=None):
        handler, memory, *_rest = _mk_handler(root, _TailCoordinator(FORMATO_TAIL), frozen=frozen)
        return handler, memory

    def _checkpoint(self) -> dict:
        return {
            "verification_status": "pending",
            "blocker_class": "formato",
            "blockers": ["formato-3-lineas: el entregable debe ser exactamente 3 líneas"],
            "summary": "resumen",
        }

    def _seed(self, memory: MemoryStore, task_id: str, **fields) -> dict:
        active_task = {"task_id": task_id, "status": "pending", **fields}
        memory.update_session_state("tg-1", active_object={"active_task": active_task})
        return active_task

    def test_fresh_formato_starts_redrive_and_persists_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir))
            active = self._seed(memory, "t-1")
            started = handler._maybe_start_redrive(
                session_id="tg-1",
                task_id="t-1",
                active_task=active,
                checkpoint=self._checkpoint(),
            )
            self.assertTrue(started)
            persisted = _active_task(memory, "tg-1")
            self.assertEqual(persisted.get("redrive_attempts"), 1)
            self.assertEqual(persisted.get("redrive_seen"), ["formato:formato-3-lineas"])

    def test_attempts_exhausted_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir))
            active = self._seed(memory, "t-1", redrive_attempts=2, redrive_seen=["formato:otro"])
            self.assertFalse(
                handler._maybe_start_redrive(
                    session_id="tg-1",
                    task_id="t-1",
                    active_task=active,
                    checkpoint=self._checkpoint(),
                )
            )

    def test_same_ident_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir))
            active = self._seed(
                memory, "t-1", redrive_attempts=1, redrive_seen=["formato:formato-3-lineas"]
            )
            self.assertFalse(
                handler._maybe_start_redrive(
                    session_id="tg-1",
                    task_id="t-1",
                    active_task=active,
                    checkpoint=self._checkpoint(),
                )
            )

    def test_frozen_window_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir), frozen=lambda: True)
            active = self._seed(memory, "t-1")
            self.assertFalse(
                handler._maybe_start_redrive(
                    session_id="tg-1",
                    task_id="t-1",
                    active_task=active,
                    checkpoint=self._checkpoint(),
                )
            )

    def test_knob_zero_disables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir))
            active = self._seed(memory, "t-1")
            with patch.dict("os.environ", {"CLAW_MAX_TASK_REDRIVES": "0"}):
                self.assertFalse(
                    handler._maybe_start_redrive(
                        session_id="tg-1",
                        task_id="t-1",
                        active_task=active,
                        checkpoint=self._checkpoint(),
                    )
                )

    def test_non_formato_class_never_redrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory = self._handler_and_memory(Path(tmpdir))
            active = self._seed(memory, "t-1")
            for clase in ("evidencia_externa", "decision_usuario", ""):
                checkpoint = {**self._checkpoint(), "blocker_class": clase}
                self.assertFalse(
                    handler._maybe_start_redrive(
                        session_id="tg-1",
                        task_id="t-1",
                        active_task=active,
                        checkpoint=checkpoint,
                    ),
                    clase,
                )


class RedriveReentryTests(unittest.TestCase):
    def test_consume_redrive_pending_forces_synthesis_and_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory, *_ = _mk_handler(Path(tmpdir), _TailCoordinator(FORMATO_TAIL))
            memory.update_session_state(
                "tg-1",
                active_object={
                    "active_task": {
                        "task_id": "t-1",
                        "status": "pending",
                        "redrive_attempts": 1,
                        "redrive_pending": {
                            "start_phase": "synthesis",
                            "verdict": "corrige el formato a 3 líneas",
                        },
                    }
                },
            )
            objective, start_phase = handler._consume_redrive_pending(
                session_id="tg-1", task_id="t-1", objective="obj base", start_phase=None
            )
            self.assertEqual(start_phase, "synthesis")
            self.assertIn("obj base", objective)
            self.assertIn("corrige el formato a 3 líneas", objective)
            # consumido: no debe re-aplicarse en el siguiente ciclo
            self.assertNotIn("redrive_pending", _active_task(memory, "tg-1"))

    def test_consume_noop_without_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, memory, *_ = _mk_handler(Path(tmpdir), _TailCoordinator(FORMATO_TAIL))
            memory.update_session_state(
                "tg-1", active_object={"active_task": {"task_id": "t-1", "status": "pending"}}
            )
            objective, start_phase = handler._consume_redrive_pending(
                session_id="tg-1", task_id="t-1", objective="obj", start_phase=None
            )
            self.assertEqual((objective, start_phase), ("obj", None))


class FailureTextHistoryTests(unittest.TestCase):
    def test_failure_text_includes_redrive_history(self) -> None:
        text = _failure_response_text(
            task_id="t1",
            checkpoint={
                "summary": "resumen",
                "redrive_history": ["formato:formato-3-lineas"],
            },
            error="waiting_for_user_input: confirma el formato",
            objective="obj",
        )
        self.assertIn("/task_pending", text)
        self.assertIn("Reintenté 1", text)
        self.assertIn("formato-3-lineas", text)

    def test_failure_text_without_history_unchanged(self) -> None:
        text = _failure_response_text(
            task_id="t1",
            checkpoint={"summary": "resumen"},
            error="waiting_for_user_input: confirma",
            objective="obj",
        )
        self.assertNotIn("Reintenté", text)
        self.assertIn("/task_pending", text)


if __name__ == "__main__":
    unittest.main()
