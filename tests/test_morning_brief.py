from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from claw_v2.jobs import JobService
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
                should_send_morning_brief(datetime(2026, 4, 27, 7, 59), stamp, hour=8)
            )
            self.assertTrue(
                should_send_morning_brief(datetime(2026, 4, 27, 8, 0), stamp, hour=8)
            )
            stamp.write_text("2026-04-27", encoding="utf-8")
            self.assertFalse(
                should_send_morning_brief(datetime(2026, 4, 27, 8, 30), stamp, hour=8)
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

    def test_missing_connectors_are_explicit_in_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = MorningBriefService(
                settings=MorningBriefSettings(stamp_path=Path(tmpdir) / "morning.txt"),
                notify=lambda _: None,
                clock=lambda: datetime(2026, 4, 27, 8, 0),
                weather_fetcher=lambda location, timeout: "auto: 70F",
            )

            message = service.build_message(datetime(2026, 4, 27, 8, 0))

            self.assertIn("Agenda: sin conector configurado", message)
            self.assertIn("Correo: sin conector configurado", message)


if __name__ == "__main__":
    unittest.main()
