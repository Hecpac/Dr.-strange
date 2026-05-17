from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from claw_v2.approval import ApprovalManager
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.metrics import MetricsTracker
from claw_v2.morning_brief import (
    MorningBriefService,
    MorningBriefSettings,
    format_spanish_date,
    should_send_morning_brief,
)
from claw_v2.observe import ObserveStream
from claw_v2.task_board import TaskBoard
from claw_v2.task_ledger import TaskLedger


class _AutoResearchStub:
    def list_agents(self) -> list[str]:
        return ["perf-optimizer", "researcher"]

    def inspect(self, name: str) -> dict:
        if name == "perf-optimizer":
            return {"paused": True, "pause_reason": "codex_timeout"}
        return {"paused": False}


class _PipelineStub:
    def list_active(self) -> list[object]:
        return [SimpleNamespace(issue_id="HEC-1", status="awaiting_approval", branch_name="feat/hec-1")]


class MorningBriefTests(unittest.TestCase):
    def test_should_send_only_in_configured_hour_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stamp = Path(tmpdir) / "sent.txt"
            self.assertFalse(
                should_send_morning_brief(datetime(2026, 4, 27, 4, 59), stamp, hour=5)
            )
            self.assertTrue(
                should_send_morning_brief(datetime(2026, 4, 27, 5, 0), stamp, hour=5)
            )
            stamp.write_text("2026-04-27", encoding="utf-8")
            self.assertFalse(
                should_send_morning_brief(datetime(2026, 4, 27, 5, 30), stamp, hour=5)
            )

    def test_formats_spanish_date_without_locale_dependency(self) -> None:
        self.assertEqual(
            format_spanish_date(datetime(2026, 4, 27, 8, 0)),
            "lunes 27 de abril de 2026",
        )

    def test_run_if_due_sends_brief_and_records_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            observe = ObserveStream(root / "claw.db")
            ledger = TaskLedger(root / "claw.db")
            jobs = JobService(root / "claw.db")
            board = TaskBoard(root / "board")
            metrics = MetricsTracker()
            metrics.record(lane="brain", provider="anthropic", model="test", cost=0.25, degraded_mode=False)
            ledger.create(
                task_id="task-1",
                session_id="tg-123",
                objective="Cerrar auditoria del agente",
                runtime="coordinator",
                status="running",
            )
            jobs.enqueue(kind="notebooklm.research", job_id="job-1")
            board.publish("Revisar propuesta", "verificar alcance", created_by="kairos", priority=10)

            service = MorningBriefService(
                settings=MorningBriefSettings(
                    hour=8,
                    weather_location="Dallas, TX",
                    email_command="email-digest",
                    calendar_command="calendar-digest",
                    stamp_path=root / "morning.txt",
                ),
                notify=sent.append,
                observe=observe,
                metrics=metrics,
                auto_research=_AutoResearchStub(),
                task_ledger=ledger,
                job_service=jobs,
                task_board=board,
                pipeline=_PipelineStub(),
                clock=lambda: datetime(2026, 4, 27, 8, 5),
                weather_fetcher=lambda location, timeout: f"{location}: 72F despejado",
                command_runner=lambda command, timeout: {
                    "email-digest": "3 correos importantes",
                    "calendar-digest": "2 eventos hoy",
                }[command],
            )

            message = service.run_if_due()
            duplicate = service.run_if_due()

            self.assertIsNotNone(message)
            self.assertIsNone(duplicate)
            self.assertEqual(len(sent), 1)
            self.assertIn("Hoy es lunes 27 de abril de 2026", sent[0])
            self.assertIn("Clima: Dallas, TX: 72F despejado", sent[0])
            self.assertIn("Agenda: 2 eventos hoy", sent[0])
            self.assertIn("Correo: 3 correos importantes", sent[0])
            self.assertIn("Task task-1: running", sent[0])
            self.assertIn("Job job-1: queued - notebooklm.research", sent[0])
            self.assertIn("Board", sent[0])
            self.assertIn("Pipeline HEC-1: awaiting_approval", sent[0])
            self.assertIn("perf-optimizer: pausado", sent[0])
            self.assertEqual((root / "morning.txt").read_text(encoding="utf-8"), "2026-04-27")
            self.assertEqual(observe.recent_events(limit=1)[0]["event_type"], "morning_brief_sent")

    def test_brief_includes_real_operational_context_not_only_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            observe = ObserveStream(root / "claw.db")
            ledger = TaskLedger(root / "claw.db")
            approvals = ApprovalManager(root / "approvals", "secret")
            memory = MemoryStore(root / "memory.db")
            approvals.create(
                "social_publish",
                "Publicar post de HealthSherpa",
                metadata={"risk_tier": "medium", "mission_id": "mission-social"},
            )
            ledger.create(
                task_id="task-failed",
                session_id="tg-123",
                objective="Revisar correo de Tatiana sobre HealthSherpa",
                runtime="coordinator",
                status="failed",
            )
            ledger.create(
                task_id="task-done",
                session_id="tg-123",
                objective="Cerrar auditoria de Telegram router",
                runtime="coordinator",
                status="succeeded",
            )
            memory.update_session_state(
                "tg-123",
                current_goal="Arreglar briefs matutinos",
                pending_action="Revisar fuentes reales antes del siguiente brief",
                verification_status="pending",
            )
            observe.emit("brain_tooluse_ledger_failed", payload={"task_id": "task-failed"})
            observe.emit("telegram_actionable_no_match", payload={"candidate_action": "paste_prompt"})

            service = MorningBriefService(
                settings=MorningBriefSettings(stamp_path=root / "morning.txt"),
                notify=lambda _: None,
                observe=observe,
                task_ledger=ledger,
                approvals=approvals,
                memory=memory,
                clock=lambda: datetime(2026, 5, 16, 5, 0),
                weather_fetcher=lambda location, timeout: "auto: 70F",
                calendar_fetcher=lambda timeout: "sin eventos hoy",
                email_fetcher=lambda timeout: "0 sin leer",
            )

            message = service.build_message(datetime(2026, 5, 16, 5, 0))

            self.assertIn("Aprobaciones:", message)
            self.assertIn("Publicar post de HealthSherpa", message)
            self.assertIn("Atencion:", message)
            self.assertIn("task-failed", message)
            self.assertIn("Cerradas recientes:", message)
            self.assertIn("task-done", message)
            self.assertIn("Contexto activo:", message)
            self.assertIn("Arreglar briefs matutinos", message)
            self.assertIn("Alertas recientes:", message)
            self.assertIn("brain_tooluse_ledger_failed", message)
            self.assertIn("telegram_actionable_no_match", message)

    def test_brief_reports_source_provenance_and_low_signal_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            observe = ObserveStream(root / "claw.db")
            service = MorningBriefService(
                settings=MorningBriefSettings(stamp_path=root / "morning.txt"),
                notify=lambda _: None,
                observe=observe,
                clock=lambda: datetime(2026, 5, 16, 5, 0),
                weather_fetcher=lambda location, timeout: "auto: 70F",
                calendar_fetcher=lambda timeout: "sin eventos hoy",
                email_fetcher=lambda timeout: "0 sin leer",
            )

            message = service.build_message(datetime(2026, 5, 16, 5, 0))

            self.assertIn("Fuentes:", message)
            self.assertIn("clima=ok", message)
            self.assertIn("agenda=empty", message)
            self.assertIn("correo=empty", message)
            self.assertIn("baja senal", message)
            self.assertEqual(observe.recent_events(limit=1)[0]["event_type"], "morning_brief_low_signal")

    def test_missing_connectors_are_explicit_in_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = MorningBriefService(
                settings=MorningBriefSettings(stamp_path=Path(tmpdir) / "morning.txt"),
                notify=lambda _: None,
                clock=lambda: datetime(2026, 4, 27, 8, 0),
                weather_fetcher=lambda location, timeout: "auto: 70F",
                calendar_fetcher=lambda timeout: (_ for _ in ()).throw(RuntimeError("calendar denied")),
                email_fetcher=lambda timeout: (_ for _ in ()).throw(RuntimeError("mail denied")),
            )

            message = service.build_message(datetime(2026, 4, 27, 8, 0))

            self.assertIn("Agenda: no disponible (RuntimeError)", message)
            self.assertIn("Correo: no disponible (RuntimeError)", message)

    def test_calendar_and_email_collectors_are_automatic_when_no_command_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = MorningBriefService(
                settings=MorningBriefSettings(stamp_path=Path(tmpdir) / "morning.txt"),
                notify=lambda _: None,
                weather_fetcher=lambda location, timeout: "auto: 70F",
                calendar_fetcher=lambda timeout: "9:00 AM - Revision diaria",
                email_fetcher=lambda timeout: "4 sin leer\n- Cliente: propuesta",
            )

            message = service.build_message(datetime(2026, 4, 27, 8, 0))

            self.assertIn("Agenda: 9:00 AM - Revision diaria", message)
            self.assertIn("Correo: 4 sin leer", message)

    def test_evening_report_uses_independent_stamp_and_event_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sent: list[str] = []
            observe = ObserveStream(root / "claw.db")
            service = MorningBriefService(
                settings=MorningBriefSettings(
                    hour=21,
                    stamp_path=root / "evening.txt",
                    report_name="evening_brief",
                    greeting="Cierre del dia, Hector.",
                ),
                notify=sent.append,
                observe=observe,
                clock=lambda: datetime(2026, 4, 27, 21, 0),
                weather_fetcher=lambda location, timeout: "auto: 68F",
                calendar_fetcher=lambda timeout: "sin eventos restantes",
                email_fetcher=lambda timeout: "2 sin leer",
            )

            message = service.run_if_due()
            duplicate = service.run_if_due()

            self.assertIsNotNone(message)
            self.assertIsNone(duplicate)
            self.assertIn("Cierre del dia, Hector.", sent[0])
            self.assertEqual((root / "evening.txt").read_text(encoding="utf-8"), "2026-04-27")
            self.assertEqual(observe.recent_events(limit=1)[0]["event_type"], "evening_brief_sent")


class ConversationalBriefTests(unittest.TestCase):
    """C: brief renders via LLM router when available; falls back to template."""

    @staticmethod
    def _stub_router(content: str = "Cierre conversacional de prueba."):
        from claw_v2.types import LLMResponse

        class _StubRouter:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def ask(self, prompt, *, system_prompt=None, lane="brain", **kwargs):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "system_prompt": system_prompt,
                        "lane": lane,
                        **kwargs,
                    }
                )
                return LLMResponse(content=content, lane=lane, provider="stub", model="stub")

        return _StubRouter()

    @staticmethod
    def _central_ts(value: datetime | None) -> float | None:
        if value is None:
            return None
        return value.replace(tzinfo=ZoneInfo("America/Chicago")).timestamp()

    @classmethod
    def _set_task_times(
        cls,
        ledger: TaskLedger,
        task_id: str,
        *,
        created_at: datetime,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        with ledger._lock:
            ledger._conn.execute(
                """
                UPDATE agent_tasks
                SET created_at = ?,
                    started_at = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    cls._central_ts(created_at),
                    cls._central_ts(started_at),
                    cls._central_ts(completed_at),
                    cls._central_ts(updated_at or created_at),
                    task_id,
                ),
            )
            ledger._conn.commit()

    def _service(self, root: Path, **overrides):
        ledger = overrides.pop("ledger", TaskLedger(root / "claw.db"))
        settings_kwargs = {
            "hour": 21,
            "stamp_path": root / "evening.txt",
            "report_name": "evening_brief",
            "greeting": "Cierre del dia, Hector.",
        }
        settings_kwargs.update(overrides.pop("settings", {}))
        return MorningBriefService(
            settings=MorningBriefSettings(**settings_kwargs),
            notify=overrides.pop("notify", lambda _: None),
            observe=overrides.pop("observe", None),
            task_ledger=ledger,
            llm_router=overrides.pop("llm_router", None),
            clock=overrides.pop("clock", lambda: datetime(2026, 5, 16, 21, 0)),
            weather_fetcher=overrides.pop("weather_fetcher", lambda l, t: "auto: 85F sol"),
            calendar_fetcher=overrides.pop("calendar_fetcher", lambda t: "sin eventos"),
            email_fetcher=overrides.pop("email_fetcher", lambda t: "0 sin leer"),
            **overrides,
        )

    def test_brief_uses_llm_router_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            router = self._stub_router("Cierre del día narrativo del LLM.")
            service = self._service(root, llm_router=router)
            message = service.build_message(datetime(2026, 5, 16, 21, 0))
            self.assertEqual(message, "Cierre del día narrativo del LLM.")
            self.assertEqual(len(router.calls), 1)
            self.assertEqual(router.calls[0]["lane"], "judge")

    def test_morning_prompt_continues_previous_day_with_exact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = TaskLedger(root / "claw.db")
            ledger.create(
                task_id="task-brief-continuity",
                session_id="tg-123",
                objective="Auditar los briefs recientes y convertirlos en bitácora real",
                runtime="brain_fallback",
                status="running",
            )
            self._set_task_times(
                ledger,
                "task-brief-continuity",
                created_at=datetime(2026, 5, 16, 14, 30),
                started_at=datetime(2026, 5, 16, 14, 31),
                updated_at=datetime(2026, 5, 16, 20, 45),
            )
            router = self._stub_router("Apertura narrativa.")
            service = self._service(
                root,
                ledger=ledger,
                llm_router=router,
                settings={
                    "hour": 5,
                    "stamp_path": root / "morning.txt",
                    "report_name": "morning_brief",
                    "greeting": "Buenos dias, Hector.",
                },
                clock=lambda: datetime(2026, 5, 17, 5, 0),
            )

            message = service.build_message(datetime(2026, 5, 17, 5, 0))

            prompt_text = str(router.calls[0]["prompt"])
            system_text = str(router.calls[0]["system_prompt"] or "")
            self.assertIn("arrancando el día operativo", system_text)
            self.assertIn("continuidad precisa desde el día anterior", system_text)
            self.assertNotIn("cerrando el día operativo", system_text)
            self.assertIn("domingo 17 de mayo de 2026", prompt_text)
            self.assertIn("sabado 16 de mayo de 2026", prompt_text)
            self.assertIn("continuación de ayer", prompt_text)
            self.assertIn("Auditar los briefs recientes", prompt_text)
            self.assertTrue(message.startswith("Apertura narrativa."))
            self.assertIn("Bitácora verificada:", message)
            self.assertIn("Pendiente para retomar hoy: 1.", message)
            self.assertIn("Auditar los briefs recientes", message)

    def test_evening_prompt_is_day_cut_and_not_morning_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = TaskLedger(root / "claw.db")
            ledger.create(
                task_id="task-evening-cut",
                session_id="tg-123",
                objective="Cerrar auditoría de cambios externos",
                runtime="brain_fallback",
                status="succeeded",
            )
            self._set_task_times(
                ledger,
                "task-evening-cut",
                created_at=datetime(2026, 5, 16, 10, 0),
                started_at=datetime(2026, 5, 16, 10, 5),
                completed_at=datetime(2026, 5, 16, 20, 10),
                updated_at=datetime(2026, 5, 16, 20, 10),
            )
            router = self._stub_router("Corte narrativo.")
            service = self._service(root, ledger=ledger, llm_router=router)

            message = service.build_message(datetime(2026, 5, 16, 21, 0))

            prompt_text = str(router.calls[0]["prompt"])
            system_text = str(router.calls[0]["system_prompt"] or "")
            self.assertIn("cerrando el día operativo", system_text)
            self.assertIn("corte del día basado en bitácora real", system_text)
            self.assertNotIn("continuidad precisa desde el día anterior", system_text)
            self.assertIn("corte de hoy", prompt_text)
            self.assertIn("sabado 16 de mayo de 2026", prompt_text)
            self.assertIn("Cerrar auditoría de cambios externos", message)
            self.assertIn("Pendiente para mañana: 0 registrado.", message)

    def test_brief_falls_back_to_template_when_llm_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            observe = ObserveStream(root / "claw.db")

            class _RaisingRouter:
                def ask(self, *args, **kwargs):
                    raise RuntimeError("router unavailable")

            service = self._service(root, llm_router=_RaisingRouter(), observe=observe)
            message = service.build_message(datetime(2026, 5, 16, 21, 0))
            self.assertIn("Cierre del dia, Hector.", message)
            self.assertIn("Clima: auto: 85F sol", message)
            kinds = [ev["event_type"] for ev in observe.recent_events(limit=10)]
            self.assertIn("evening_brief_llm_failed", kinds)

    def test_llm_context_does_not_include_raw_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger = TaskLedger(root / "claw.db")
            secret_id = "tg-574707975:1778952862707620000"
            ledger.create(
                task_id=secret_id,
                session_id="tg-574707975",
                objective="Cerrar la auditoría del agente",
                runtime="brain_fallback",
                status="failed",
            )
            router = self._stub_router("Brief generado.")
            service = self._service(root, llm_router=router, ledger=ledger)
            service.build_message(datetime(2026, 5, 16, 21, 0))
            prompt_text = str(router.calls[0]["prompt"])
            system_text = str(router.calls[0]["system_prompt"] or "")
            combined = prompt_text + "\n" + system_text
            self.assertNotIn(secret_id, combined)
            self.assertIn("Cerrar la auditoría del agente", combined)

    def test_no_llm_router_preserves_existing_template_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = self._service(root, llm_router=None)
            message = service.build_message(datetime(2026, 5, 16, 21, 0))
            self.assertIn("Cierre del dia, Hector.", message)
            self.assertIn("Clima: auto: 85F sol", message)


if __name__ == "__main__":
    unittest.main()
