"""Slice #2 — despacho daemon-side de entregables (invariante deliverable_dispatch_daemon_side).

El worker ops produce archivos LOCALMENTE en <scratch>/<task_id>/deliverables/
(su cwd; git init como trust-marker del codex CLI) y los declara con un tail
`DELIVERABLES:` fail-closed. El DAEMON — nunca el worker — hace el envío por
Telegram POST verification=passed y POST governor: un fallo de envío degrada a
terminal failed honesto y jamás re-conduce la tarea. Destino restringido por
código al chat de origen del dueño. Diseño: recon+decisiones 2026-07-02
(memoria autonomy-c1-redrive-feasibility-gate → par #1→#2).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claw_v2.bot_helpers import (
    DELIVERABLES_TAIL_INSTRUCTION,
    _coordinator_checkpoint,
    parse_deliverables_tail,
)
from claw_v2.coordinator import CoordinatorResult, CoordinatorService, WorkerResult, WorkerTask
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger

PASSED_VERDICT = "Verification Status: passed\nCLASE_BLOCKER: ninguna\nBLOCKERS:\n"

IMPL_WITH_TAIL = (
    "## Actions\n- generado el informe mensual\n"
    "## Verify\n- check: archivos presentes -> ok\n"
    "## Evidence\n- none\n"
    "DELIVERABLES:\n"
    "- informe_1.html\n"
    "- informe_2.html\n"
)

IMPL_NO_TAIL = "## Actions\n- operacion ejecutada\n## Verify\n- check: ok\n## Evidence\n- none\n"


class ParseDeliverablesTailTests(unittest.TestCase):
    def test_parses_declared_files(self) -> None:
        self.assertEqual(
            parse_deliverables_tail(IMPL_WITH_TAIL),
            ("informe_1.html", "informe_2.html"),
        )

    def test_missing_header_returns_none(self) -> None:
        self.assertIsNone(parse_deliverables_tail(IMPL_NO_TAIL))
        self.assertIsNone(parse_deliverables_tail(""))
        self.assertIsNone(parse_deliverables_tail(None))

    def test_header_without_items_returns_none(self) -> None:
        self.assertIsNone(parse_deliverables_tail("trabajo hecho\nDELIVERABLES:\n"))
        self.assertIsNone(parse_deliverables_tail("DELIVERABLES:\ntexto libre sin item"))

    def test_last_header_wins(self) -> None:
        text = "DELIVERABLES:\n- viejo.txt\n\nrevision posterior\nDELIVERABLES:\n- nuevo.txt\n"
        self.assertEqual(parse_deliverables_tail(text), ("nuevo.txt",))

    def test_list_stops_at_first_non_item_line(self) -> None:
        text = "DELIVERABLES:\n- a.html\n- b.html\nNota final del worker\n- c.html\n"
        self.assertEqual(parse_deliverables_tail(text), ("a.html", "b.html"))

    def test_raw_names_are_not_sanitized_here(self) -> None:
        # La validación de containment vive en el dispatch, no en el parser.
        self.assertEqual(
            parse_deliverables_tail("DELIVERABLES:\n- ../escape.txt\n"),
            ("../escape.txt",),
        )


class CheckpointDeliverablesTests(unittest.TestCase):
    def _result(self, impl_content: str | None, verify_content: str) -> CoordinatorResult:
        phases: dict[str, list[WorkerResult]] = {
            "verification": [
                WorkerResult(
                    task_name="verify_operation", content=verify_content, duration_seconds=0.1
                )
            ]
        }
        if impl_content is not None:
            phases["implementation"] = [
                WorkerResult(
                    task_name="execute_operation", content=impl_content, duration_seconds=0.1
                )
            ]
        return CoordinatorResult(task_id="t1", phase_results=phases, synthesis="plan")

    def test_checkpoint_carries_deliverables_from_implementation(self) -> None:
        checkpoint = _coordinator_checkpoint(
            self._result(IMPL_WITH_TAIL, PASSED_VERDICT), objective="obj"
        )
        self.assertEqual(checkpoint.get("deliverables"), ["informe_1.html", "informe_2.html"])

    def test_no_tail_means_no_key(self) -> None:
        checkpoint = _coordinator_checkpoint(
            self._result(IMPL_NO_TAIL, PASSED_VERDICT), objective="obj"
        )
        self.assertNotIn("deliverables", checkpoint)

    def test_tail_in_verification_text_is_ignored(self) -> None:
        # Solo la fase que PRODUCE los archivos puede declararlos.
        checkpoint = _coordinator_checkpoint(
            self._result(IMPL_NO_TAIL, PASSED_VERDICT + "DELIVERABLES:\n- fake.html\n"),
            objective="obj",
        )
        self.assertNotIn("deliverables", checkpoint)


class _ObsNoop:
    def emit(self, *args, **kwargs):  # pragma: no cover - trivial
        return None


class _AskRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def ask(self, instruction, **kwargs):
        self.calls.append(dict(kwargs))
        from claw_v2.types import LLMResponse

        return LLMResponse(
            content="ok",
            lane=kwargs.get("lane", "worker"),
            provider="codex",
            model="m",
        )


class WorkerTaskCwdTests(unittest.TestCase):
    def test_execute_worker_passes_cwd_to_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = _AskRecorder()
            svc = CoordinatorService(router=recorder, observe=_ObsNoop(), scratch_root=Path(tmpdir))
            svc._execute_worker(WorkerTask(name="t", instruction="i", lane="worker", cwd="/x"))
            self.assertEqual(recorder.calls[0].get("cwd"), "/x")

    def test_execute_worker_without_cwd_omits_kwarg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = _AskRecorder()
            svc = CoordinatorService(router=recorder, observe=_ObsNoop(), scratch_root=Path(tmpdir))
            svc._execute_worker(WorkerTask(name="t", instruction="i", lane="worker"))
            self.assertNotIn("cwd", recorder.calls[0])


class _DeliverCoordinator:
    """Fake coordinator: crea los archivos del worker en deliverables/ y
    devuelve implementation+verification con el contenido parametrizado."""

    def __init__(
        self,
        scratch_root: Path,
        *,
        impl_content: str = IMPL_WITH_TAIL,
        files_to_create: tuple[str, ...] = ("informe_1.html", "informe_2.html"),
        file_bytes: bytes = b"<html>ok</html>",
    ) -> None:
        self.scratch_root = Path(scratch_root)
        self.impl_content = impl_content
        self.files_to_create = files_to_create
        self.file_bytes = file_bytes
        self.calls: list[dict] = []

    def run(self, task_id, objective, research_tasks, **kwargs):
        self.calls.append({"task_id": task_id, "objective": objective, **kwargs})
        deliverables = self.scratch_root / str(task_id) / "deliverables"
        deliverables.mkdir(parents=True, exist_ok=True)
        for name in self.files_to_create:
            (deliverables / name).write_bytes(self.file_bytes)
        return CoordinatorResult(
            task_id=task_id,
            phase_results={
                "implementation": [
                    WorkerResult(
                        task_name="execute_operation",
                        content=self.impl_content,
                        duration_seconds=0.1,
                    )
                ],
                "verification": [
                    WorkerResult(
                        task_name="verify_operation",
                        content=PASSED_VERDICT,
                        duration_seconds=0.1,
                    )
                ],
            },
            synthesis="operacion ejecutada",
        )


class _RecordingDelivery:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    def __call__(self, path, *, chat_id, caption, **kwargs):
        self.calls.append({"path": Path(path), "chat_id": chat_id, "caption": caption})
        if self.ok:
            return {
                "ok": True,
                "method": "sendDocument",
                "telegram_message_id": 4200 + len(self.calls),
            }
        return {
            "ok": False,
            "method": "sendDocument",
            "telegram_message_id": None,
            "error": "http_400: chat not found",
        }


def _mk_handler(root: Path, coordinator, delivery):
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
        file_delivery=delivery,
    )
    return handler, memory, observe, stored


def _run_ops_task(handler, session_id: str, objective: str) -> str:
    ack = handler.start_autonomous_task(session_id, objective, mode="ops")
    task_id = ack.split("`", 2)[1]
    assert handler.wait_for_task(task_id, timeout=10)
    return task_id


OBJECTIVE = "genera el informe mensual en HTML y enviamelo"


class OpsDeliverablesWiringTests(unittest.TestCase):
    def test_ops_implementation_gets_cwd_and_convention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _DeliverCoordinator(root / "scratch")
            handler, _memory, _observe, _stored = _mk_handler(
                root, coordinator, _RecordingDelivery()
            )
            task_id = _run_ops_task(handler, "tg-1", OBJECTIVE)

            impl_tasks = coordinator.calls[0].get("implementation_tasks") or []
            self.assertTrue(impl_tasks)
            expected_cwd = root / "scratch" / task_id / "deliverables"
            self.assertEqual(impl_tasks[0].cwd, str(expected_cwd))
            self.assertIn("DELIVERABLES:", impl_tasks[0].instruction)
            # git init = trust-marker del codex CLI (probe 2026-07-02).
            self.assertTrue((expected_cwd / ".git").exists())

    def test_research_mode_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _DeliverCoordinator(root / "scratch", impl_content=IMPL_NO_TAIL)
            handler, _memory, _observe, _stored = _mk_handler(
                root, coordinator, _RecordingDelivery()
            )
            ack = handler.start_autonomous_task("tg-1", "resume launchd", mode="research")
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=10))
            self.assertIsNone(coordinator.calls[0].get("implementation_tasks"))
            # El fake crea el directorio; el trust-marker .git solo lo crea
            # _prepare_deliverables_dir, que research jamás debe invocar.
            self.assertFalse((root / "scratch" / task_id / "deliverables" / ".git").exists())

    def test_publish_mode_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _DeliverCoordinator(root / "scratch", impl_content=IMPL_NO_TAIL)
            handler, _memory, _observe, _stored = _mk_handler(
                root, coordinator, _RecordingDelivery()
            )
            ack = handler.start_autonomous_task(
                "tg-1", "prepara el contenido del anuncio", mode="publish"
            )
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=10))
            impl_tasks = coordinator.calls[0].get("implementation_tasks") or []
            self.assertTrue(impl_tasks)
            self.assertIsNone(impl_tasks[0].cwd)
            self.assertNotIn("DELIVERABLES:", impl_tasks[0].instruction)


class DeliverableDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Guard de destino determinista: el chat de la sesión ES el owner.
        patcher = mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "777"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_chat_must_match_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            handler, _memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _run_ops_task(handler, "tg-999", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("destino_no_autorizado", failed["payload"]["error"])

    def test_declared_files_are_sent_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            handler, memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _task_id = _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(len(delivery.calls), 2)
            self.assertEqual(delivery.calls[0]["chat_id"], "777")
            self.assertEqual(delivery.calls[0]["caption"], "informe_1.html")

            events = observe.recent_events(limit=200)
            completed = next(e for e in events if e["event_type"] == "autonomous_task_completed")
            self.assertIn("Entregado por Telegram", completed["payload"]["response"])
            self.assertIn("informe_1.html", completed["payload"]["response"])
            dispatch_events = [
                e for e in events if e["event_type"] == "autonomous_task_deliverable_dispatch"
            ]
            self.assertEqual(len(dispatch_events), 2)
            self.assertTrue(all(e["payload"]["ok"] for e in dispatch_events))

            deliveries = (memory.get_session_state("tg-777").get("last_checkpoint") or {}).get(
                "deliveries"
            ) or []
            self.assertEqual(len(deliveries), 2)
            self.assertTrue(all(d.get("ok") for d in deliveries))
            self.assertTrue(all(d.get("message_id") for d in deliveries))

    def test_send_failure_downgrades_to_honest_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery(ok=False)
            handler, memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            events = observe.recent_events(limit=200)
            types = [e["event_type"] for e in events]
            self.assertNotIn("autonomous_task_completed", types)
            failed = next(e for e in events if e["event_type"] == "autonomous_task_failed")
            self.assertIn("deliverable_send_failed", failed["payload"]["error"])
            # El artefacto sigue en scratch: la muerte es honesta, no muda.
            deliveries = (memory.get_session_state("tg-777").get("last_checkpoint") or {}).get(
                "deliveries"
            ) or []
            self.assertEqual(len(deliveries), 2)
            self.assertFalse(any(d.get("ok") for d in deliveries))
            # Un fallo de envío jamás arma redrive (post-governor por posición).
            self.assertNotIn("autonomous_task_redrive_decision", types)

    def test_missing_declared_file_fails_without_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(root / "scratch", files_to_create=("informe_1.html",))
            handler, _memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            # Solo el archivo existente se intenta enviar.
            self.assertEqual(len(delivery.calls), 1)
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("informe_2.html", failed["payload"]["error"])

    def test_traversal_and_symlink_names_rejected_without_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            impl = "DELIVERABLES:\n- ../escape.html\n- enlace.html\n"
            coordinator = _DeliverCoordinator(
                root / "scratch", impl_content=IMPL_NO_TAIL + impl, files_to_create=()
            )
            handler, _memory, observe, _stored = _mk_handler(root, coordinator, delivery)

            # El symlink apunta FUERA del directorio de entregables.
            secret = root / "secreto.txt"
            secret.write_text("no salgas de aqui")
            original_run = coordinator.run

            def run_and_symlink(task_id, objective, research_tasks, **kwargs):
                result = original_run(task_id, objective, research_tasks, **kwargs)
                link = self_root / str(task_id) / "deliverables" / "enlace.html"
                link.symlink_to(secret)
                return result

            self_root = root / "scratch"
            coordinator.run = run_and_symlink  # type: ignore[method-assign]
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("deliverable_send_failed", failed["payload"]["error"])

    def test_non_telegram_session_lists_paths_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            handler, _memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _run_ops_task(handler, "web-1", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            completed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_completed"
            )
            self.assertIn("informe_1.html", completed["payload"]["response"])

    def test_no_tail_is_byte_identical_no_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(
                root / "scratch", impl_content=IMPL_NO_TAIL, files_to_create=()
            )
            handler, memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            events = observe.recent_events(limit=200)
            types = [e["event_type"] for e in events]
            self.assertIn("autonomous_task_completed", types)
            self.assertNotIn("autonomous_task_deliverable_dispatch", types)
            checkpoint = memory.get_session_state("tg-777").get("last_checkpoint") or {}
            self.assertNotIn("deliveries", checkpoint)

    def test_instruction_constant_mentions_tail_and_cwd(self) -> None:
        self.assertIn("DELIVERABLES:", DELIVERABLES_TAIL_INSTRUCTION)
        self.assertIn("directorio de trabajo", DELIVERABLES_TAIL_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
