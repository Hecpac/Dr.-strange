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


def _run_ops_task(
    handler, session_id: str, objective: str, *, deliver_to_owner: bool = True
) -> str:
    # #2b: el dispatch daemon-side solo corre con deliver_to_owner=true (el flag
    # de la delegación). Las misiones de entrega lo pasan; los negativos no.
    md = {"origin": "brain_delegate_tool", "deliver_to_owner": True} if deliver_to_owner else None
    ack = handler.start_autonomous_task(session_id, objective, mode="ops", delegation_metadata=md)
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

    def test_ops_without_flag_is_byte_identical_no_cwd(self) -> None:
        # #2b: un ops SIN deliver_to_owner es byte-idéntico a pre-#2 — sin cwd,
        # sin git init, sin convención DELIVERABLES inyectada.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            coordinator = _DeliverCoordinator(root / "scratch", impl_content=IMPL_NO_TAIL)
            handler, _memory, _observe, _stored = _mk_handler(
                root, coordinator, _RecordingDelivery()
            )
            task_id = _run_ops_task(handler, "tg-1", OBJECTIVE, deliver_to_owner=False)

            impl_tasks = coordinator.calls[0].get("implementation_tasks") or []
            self.assertTrue(impl_tasks)
            self.assertIsNone(impl_tasks[0].cwd)
            self.assertNotIn("DELIVERABLES:", impl_tasks[0].instruction)
            self.assertFalse((root / "scratch" / task_id / "deliverables" / ".git").exists())

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
        self.assertIn("directorio de trabajo", DELIVERABLES_TAIL_INSTRUCTION.lower())
        # #2b: prohíbe envío in-band y rutas absolutas (causa del smoke negativo).
        low = DELIVERABLES_TAIL_INSTRUCTION.lower()
        self.assertIn("no ejecutes", low)
        self.assertIn("absolutas", low)


class ReviewFixTests(unittest.TestCase):
    """Locks de los 5 hallazgos del review adversarial del PR #187."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "777"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_publish_organic_tail_does_not_dispatch(self) -> None:
        # #1 MUST-FIX: un publish cuyo worker escribe orgánicamente un bloque
        # DELIVERABLES (sin contrato ni cwd) debe cerrar completed — el
        # dispatch está gateado a mode=ops.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(
                root / "scratch",
                impl_content=IMPL_NO_TAIL + "DELIVERABLES:\n- borrador_post.md\n",
                files_to_create=(),
            )
            handler, _memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            ack = handler.start_autonomous_task(
                "tg-777", "prepara el contenido del anuncio", mode="publish"
            )
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=10))

            self.assertEqual(delivery.calls, [])
            events = observe.recent_events(limit=200)
            types = [e["event_type"] for e in events]
            self.assertIn("autonomous_task_completed", types)
            self.assertNotIn("autonomous_task_deliverable_dispatch", types)

    def test_owner_env_missing_fails_closed(self) -> None:
        # #3: sin TELEGRAM_ALLOWED_USER_ID no hay destino autorizado — el
        # cross-check jamás se salta (fail-closed, no fail-open).
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            handler, _memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            with mock.patch.dict(os.environ):
                os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
                _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("destino_no_autorizado", failed["payload"]["error"])

    def test_cap_overflow_sends_nothing(self) -> None:
        # #4: cap violado ⇒ fail-closed ANTES de cualquier envío (0 efectos
        # externos) y evento de auditoría por CADA archivo declarado.
        names = tuple(f"informe_{i}.html" for i in range(6))
        tail = "DELIVERABLES:\n" + "".join(f"- {n}\n" for n in names)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(
                root / "scratch", impl_content=IMPL_NO_TAIL + tail, files_to_create=names
            )
            handler, _memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            events = observe.recent_events(limit=300)
            failed = next(e for e in events if e["event_type"] == "autonomous_task_failed")
            self.assertIn("cap_archivos_excedido", failed["payload"]["error"])
            dispatch_events = [
                e for e in events if e["event_type"] == "autonomous_task_deliverable_dispatch"
            ]
            self.assertEqual(len(dispatch_events), 6)
            self.assertFalse(any(e["payload"]["ok"] for e in dispatch_events))

    def test_nul_name_rejected_and_delivery_record_survives(self) -> None:
        # #2: un nombre con NUL no revienta el runner con ValueError — se
        # rechaza como nombre_invalido y el registro de deliveries (incluido
        # el message_id del archivo que SÍ se envió) sobrevive al checkpoint.
        tail = "DELIVERABLES:\n- informe_1.html\n- mal\x00o.html\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(
                root / "scratch",
                impl_content=IMPL_NO_TAIL + tail,
                files_to_create=("informe_1.html",),
            )
            handler, memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "tg-777", OBJECTIVE)

            self.assertEqual(len(delivery.calls), 1)
            events = observe.recent_events(limit=200)
            types = [e["event_type"] for e in events]
            self.assertNotIn("autonomous_task_completed", types)
            failed = next(e for e in events if e["event_type"] == "autonomous_task_failed")
            self.assertIn("nombre_invalido", failed["payload"]["error"])
            deliveries = (memory.get_session_state("tg-777").get("last_checkpoint") or {}).get(
                "deliveries"
            ) or []
            self.assertEqual(len(deliveries), 2)
            sent_ok = [d for d in deliveries if d.get("ok")]
            self.assertEqual(len(sent_ok), 1)
            self.assertTrue(sent_ok[0].get("message_id"))

    def test_non_tg_missing_file_is_honest(self) -> None:
        # #5: el camino no-tg valida igual — un archivo declarado inexistente
        # jamás se registra ok:True (claim confabulado) y degrada honesto.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(root / "scratch", files_to_create=())
            handler, memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "web-1", OBJECTIVE)

            self.assertEqual(delivery.calls, [])
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("archivo_no_encontrado", failed["payload"]["error"])
            deliveries = (memory.get_session_state("web-1").get("last_checkpoint") or {}).get(
                "deliveries"
            ) or []
            self.assertTrue(deliveries)
            self.assertFalse(any(d.get("ok") for d in deliveries))


class _FakeSDK:
    """Captura el schema y el handler que build_delegation_mcp_server registra."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name, description, schema):
        def deco(fn):
            self.tools[name] = {"description": description, "schema": schema, "fn": fn}
            return fn

        return deco

    def create_sdk_mcp_server(self, **kwargs):
        return kwargs


class DeliverToOwnerSchemaTests(unittest.TestCase):
    """#2b: el flag deliver_to_owner en el schema de delegate_task + su propagación."""

    def _build(self, captured: list):
        from types import SimpleNamespace

        from claw_v2.adapters.anthropic_options import build_delegation_mcp_server

        def handler(payload: dict) -> dict:
            captured.append(payload)
            return {"ack": "Tarea iniciada `t1`"}

        # build_delegation_mcp_server solo lee request.delegation_handler.
        req = SimpleNamespace(delegation_handler=handler)
        sdk = _FakeSDK()
        build_delegation_mcp_server(sdk, req)
        return sdk

    def test_schema_has_deliver_to_owner_boolean(self) -> None:
        sdk = self._build([])
        props = sdk.tools["delegate_task"]["schema"]["properties"]
        self.assertIn("deliver_to_owner", props)
        self.assertEqual(props["deliver_to_owner"]["type"], "boolean")

    def test_flag_propagates_to_handler_payload(self) -> None:
        import asyncio

        captured: list = []
        sdk = self._build(captured)
        fn = sdk.tools["delegate_task"]["fn"]
        asyncio.new_event_loop().run_until_complete(
            fn({"objective": "crea 2 HTML", "mode": "ops", "deliver_to_owner": True})
        )
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["deliver_to_owner"], True)

    def test_flag_defaults_false_when_absent(self) -> None:
        import asyncio

        captured: list = []
        sdk = self._build(captured)
        fn = sdk.tools["delegate_task"]["fn"]
        asyncio.new_event_loop().run_until_complete(fn({"objective": "solo investiga"}))
        self.assertIs(captured[0]["deliver_to_owner"], False)


class DeliverToOwnerContractTests(unittest.TestCase):
    """#2b: el DELEGATION_CONTRACT instruye produce-sin-envío (anclas bilingües B2.0)."""

    def test_contract_mentions_deliver_to_owner_and_no_send(self) -> None:
        from claw_v2.brain import DELEGATION_CONTRACT

        self.assertIn("deliver_to_owner", DELEGATION_CONTRACT)
        low = DELEGATION_CONTRACT.lower()
        # Ancla bilingüe del verbo de entrega (envíame / mándame / pásame).
        self.assertTrue(any(tok in low for tok in ("enví", "mánda", "pásame")))
        # La instrucción de NO incluir pasos de envío en el objective.
        self.assertIn("do not put any send", low)


class DeliverToOwnerGateTests(unittest.TestCase):
    """#2b: el gate del dispatch (y del wiring cwd) exige el flag."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "777"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flag_present_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            handler, _memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _run_ops_task(handler, "tg-777", OBJECTIVE, deliver_to_owner=True)
            self.assertEqual(len(delivery.calls), 2)
            types = [e["event_type"] for e in observe.recent_events(limit=200)]
            self.assertIn("autonomous_task_completed", types)

    def test_no_flag_never_dispatches_even_with_organic_tail(self) -> None:
        # Un ops SIN flag cuyo worker igualmente emite un tail DELIVERABLES
        # orgánico: el gate lo ignora (0 envíos), completa normal.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            # Sin flag el wiring no corre, pero el fake escribe el tail igual;
            # el checkpoint NO lo lleva porque el gate del wiring no inyectó cwd.
            # Aun si lo llevara, el dispatch exige el flag.
            handler, _memory, observe, _stored = _mk_handler(
                root, _DeliverCoordinator(root / "scratch"), delivery
            )
            _run_ops_task(handler, "tg-777", OBJECTIVE, deliver_to_owner=False)
            self.assertEqual(delivery.calls, [])
            types = [e["event_type"] for e in observe.recent_events(limit=200)]
            self.assertIn("autonomous_task_completed", types)
            self.assertNotIn("autonomous_task_deliverable_dispatch", types)


class SmokeNegativeContainmentTests(unittest.TestCase):
    """#2b: la ruta absoluta del smoke negativo declarada ⇒ rechazada por containment."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "777"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_absolute_path_from_smoke_rejected(self) -> None:
        # Fixture literal del smoke negativo 2026-07-02: el worker escribió aquí.
        abs_name = "/Users/hector/srv/claw-daemon/resumen_claw.html"
        self.assertIsNotNone(parse_deliverables_tail(f"DELIVERABLES:\n- {abs_name}\n"))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _RecordingDelivery()
            coordinator = _DeliverCoordinator(
                root / "scratch",
                impl_content=IMPL_NO_TAIL + f"DELIVERABLES:\n- {abs_name}\n",
                files_to_create=(),
            )
            handler, _memory, observe, _stored = _mk_handler(root, coordinator, delivery)
            _run_ops_task(handler, "tg-777", OBJECTIVE, deliver_to_owner=True)

            self.assertEqual(delivery.calls, [])
            failed = next(
                e
                for e in observe.recent_events(limit=200)
                if e["event_type"] == "autonomous_task_failed"
            )
            self.assertIn("nombre_invalido", failed["payload"]["error"])


if __name__ == "__main__":
    unittest.main()
