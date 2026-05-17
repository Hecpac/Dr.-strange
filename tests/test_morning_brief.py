from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
