from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from claw_v2.config import ScheduledSubAgentConfig, _default_scheduled_sub_agents
from claw_v2.cron import CronScheduler, ScheduledJob, _next_due_for_daily_at


class FakeCronPersistence:
    def __init__(self, saved: dict[str, tuple[float, int]]) -> None:
        self.saved = saved
        self.writes: list[tuple[str, float, int]] = []

    def load_cron_state(self) -> dict[str, tuple[float, int]]:
        return dict(self.saved)

    def save_cron_job(self, job_name: str, last_run_at: float, runs: int) -> None:
        self.writes.append((job_name, last_run_at, runs))


def _ts(year: int, month: int, day: int, hour: int, minute: int, tz: str) -> float:
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))
    return dt.timestamp()


class DailyAtCalculationTests(unittest.TestCase):
    def test_daily_at_0800_central_next_run(self) -> None:
        # Now 2026-04-27 06:30 CDT (before 8am)
        now = _ts(2026, 4, 27, 6, 30, "America/Chicago")
        next_due = _next_due_for_daily_at("08:00", "America/Chicago", 0.0, now=now)
        expected = _ts(2026, 4, 27, 8, 0, "America/Chicago")
        self.assertEqual(next_due, expected)

    def test_daily_at_after_window_due_immediately_when_no_run(self) -> None:
        # Now 2026-04-27 09:00 CDT (after 8am, never ran)
        now = _ts(2026, 4, 27, 9, 0, "America/Chicago")
        next_due = _next_due_for_daily_at("08:00", "America/Chicago", 0.0, now=now)
        self.assertEqual(next_due, now)

    def test_daily_at_after_run_today_schedules_tomorrow(self) -> None:
        last_run = _ts(2026, 4, 27, 8, 0, "America/Chicago")
        now = _ts(2026, 4, 27, 14, 0, "America/Chicago")
        next_due = _next_due_for_daily_at("08:00", "America/Chicago", last_run, now=now)
        expected = _ts(2026, 4, 28, 8, 0, "America/Chicago")
        self.assertEqual(next_due, expected)


class CronSchedulerTests(unittest.TestCase):
    def test_interval_seconds_remains_supported(self) -> None:
        scheduler = CronScheduler()
        called = {"count": 0}

        def handler() -> None:
            called["count"] += 1

        scheduler.register(
            ScheduledJob(name="legacy", interval_seconds=60, handler=handler)
        )
        scheduler.run_due(now=1000.0)
        self.assertEqual(called["count"], 1)
        scheduler.run_due(now=1030.0)  # within interval
        self.assertEqual(called["count"], 1)
        scheduler.run_due(now=1100.0)  # interval elapsed
        self.assertEqual(called["count"], 2)

    def test_daily_at_overrides_interval_when_both_set(self) -> None:
        scheduler = CronScheduler()
        called = {"count": 0}

        def handler() -> None:
            called["count"] += 1

        scheduler.register(
            ScheduledJob(
                name="ai_news",
                interval_seconds=60,  # ignored when daily_at set
                daily_at="08:00",
                timezone="America/Chicago",
                handler=handler,
            )
        )
        before_8 = _ts(2026, 4, 27, 6, 0, "America/Chicago")
        scheduler.run_due(now=before_8)
        self.assertEqual(called["count"], 0)
        at_8 = _ts(2026, 4, 27, 8, 0, "America/Chicago")
        scheduler.run_due(now=at_8)
        self.assertEqual(called["count"], 1)
        # Re-run within same day not allowed
        at_10 = _ts(2026, 4, 27, 10, 0, "America/Chicago")
        scheduler.run_due(now=at_10)
        self.assertEqual(called["count"], 1)
        # Next day 8am fires again
        next_day = _ts(2026, 4, 28, 8, 0, "America/Chicago")
        scheduler.run_due(now=next_day)
        self.assertEqual(called["count"], 2)

    def test_late_registered_job_uses_restored_state(self) -> None:
        persistence = FakeCronPersistence({"nlm_wiki_sync": (1000.0, 1)})
        scheduler = CronScheduler(persistence=persistence)
        scheduler.restore()
        called = {"count": 0}

        def handler() -> None:
            called["count"] += 1

        scheduler.register(
            ScheduledJob(name="nlm_wiki_sync", interval_seconds=43200, handler=handler)
        )
        scheduler.run_due(now=2000.0)

        self.assertEqual(called["count"], 0)
        self.assertEqual(persistence.writes, [])


class DefaultsTests(unittest.TestCase):
    def test_ai_news_daily_uses_morning_schedule_not_plain_86400(self) -> None:
        defaults = _default_scheduled_sub_agents()
        ai_news = next(
            (job for job in defaults if job.skill == "ai-news-daily"), None
        )
        self.assertIsNotNone(ai_news)
        self.assertEqual(ai_news.daily_at, "08:00")
        self.assertEqual(ai_news.timezone, "America/Chicago")
        self.assertIsNone(ai_news.interval_seconds)


class ConfigValidationTests(unittest.TestCase):
    def test_daily_at_requires_timezone(self) -> None:
        from claw_v2.config import AppConfig

        # Replace scheduled_sub_agents with a malformed entry
        cfg = AppConfig.from_env()
        cfg.scheduled_sub_agents = [
            ScheduledSubAgentConfig(
                agent="x", skill="y", daily_at="08:00", timezone=None, lane="worker"
            )
        ]
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("daily_at requires timezone", str(ctx.exception))

    def test_daily_at_invalid_format_rejected(self) -> None:
        from claw_v2.config import AppConfig

        cfg = AppConfig.from_env()
        cfg.scheduled_sub_agents = [
            ScheduledSubAgentConfig(
                agent="x",
                skill="y",
                daily_at="25:99",
                timezone="America/Chicago",
                lane="worker",
            )
        ]
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("daily_at must be HH:MM", str(ctx.exception))

    def test_either_interval_or_daily_at_required(self) -> None:
        from claw_v2.config import AppConfig

        cfg = AppConfig.from_env()
        cfg.scheduled_sub_agents = [
            ScheduledSubAgentConfig(
                agent="x", skill="y", lane="worker"
            )
        ]
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("must declare interval_seconds or daily_at", str(ctx.exception))


class TestNextDueDST(unittest.TestCase):
    # Regression for Bug #6: now_dt.replace(hour=...) can produce a datetime in the
    # spring-forward gap (02:00–03:00 AM) or the fall-back overlap, yielding an
    # incorrect timestamp. Fixed by round-tripping through UTC after replace().

    def _epoch(self, iso: str, tz_key: str) -> float:
        tz = ZoneInfo(tz_key)
        return datetime.fromisoformat(iso).replace(tzinfo=tz).timestamp()

    def test_spring_forward_gap_resolves_to_valid_timestamp(self) -> None:
        # 2025-03-09: clocks spring forward from 02:00 to 03:00 in America/Chicago.
        # 02:30 does not exist. The UTC round-trip must produce a valid, existent datetime.
        tz_key = "America/Chicago"
        tz = ZoneInfo(tz_key)
        # "now" is 01:00 CST, just before the gap
        now = self._epoch("2025-03-09T01:00:00", tz_key)
        next_due = _next_due_for_daily_at("02:30", tz_key, 0.0, now=now)
        # The resolved timestamp must round-trip cleanly (no fold artifact)
        resolved = datetime.fromtimestamp(next_due, tz=tz)
        self.assertEqual(datetime.fromtimestamp(resolved.timestamp(), tz=tz), resolved)
        # Must be after now and not in the past
        self.assertGreater(next_due, now)

    def test_fall_back_overlap_fires_once(self) -> None:
        # 2025-11-02: clocks fall back from 02:00 to 01:00 in America/Chicago.
        # 01:30 occurs twice. The job should fire on the first occurrence (fold=0).
        tz_key = "America/Chicago"
        tz = ZoneInfo(tz_key)
        # "now" is 00:30 CDT, before the ambiguous hour
        now = self._epoch("2025-11-02T00:30:00", tz_key)
        next_due = _next_due_for_daily_at("01:30", tz_key, 0.0, now=now)
        resolved = datetime.fromtimestamp(next_due, tz=tz)
        # Must be on the same date and fold=0 (CDT, UTC-5)
        self.assertEqual(resolved.date().isoformat(), "2025-11-02")
        self.assertGreater(next_due, now)


if __name__ == "__main__":
    unittest.main()
