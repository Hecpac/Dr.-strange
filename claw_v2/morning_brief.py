from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SPANISH_WEEKDAYS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)

SPANISH_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

ACTIVE_TASK_STATUSES = ("queued", "running")
ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_approval", "retrying")


@dataclass(slots=True)
class MorningBriefSettings:
    enabled: bool = True
    hour: int = 5
    timezone: str = "America/Chicago"
    weather_location: str = ""
    email_command: str | None = None
    calendar_command: str | None = None
    stamp_path: Path = Path.home() / ".claw" / "morning_brief_last_sent.txt"
    command_timeout_seconds: float = 10.0
    report_name: str = "morning_brief"
    greeting: str = "Buenos dias, Hector."


class MorningBriefService:
    def __init__(
        self,
        *,
        settings: MorningBriefSettings,
        notify: Callable[[str], None],
        observe: Any | None = None,
        metrics: Any | None = None,
        auto_research: Any | None = None,
        task_ledger: Any | None = None,
        job_service: Any | None = None,
        task_board: Any | None = None,
        pipeline: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        weather_fetcher: Callable[[str, float], str] | None = None,
        command_runner: Callable[[str, float], str] | None = None,
        email_fetcher: Callable[[float], str] | None = None,
        calendar_fetcher: Callable[[float], str] | None = None,
    ) -> None:
        self.settings = settings
        self.notify = notify
        self.observe = observe
        self.metrics = metrics
        self.auto_research = auto_research
        self.task_ledger = task_ledger
        self.job_service = job_service
        self.task_board = task_board
        self.pipeline = pipeline
        self.clock = clock or self._local_now
        self.weather_fetcher = weather_fetcher or fetch_weather_summary
        self.command_runner = command_runner or run_external_summary_command
        self.email_fetcher = email_fetcher or fetch_mail_summary
        self.calendar_fetcher = calendar_fetcher or fetch_calendar_summary

    def run_if_due(self) -> str | None:
        now = self.clock()
        if not should_send_morning_brief(
            now,
            self.settings.stamp_path,
            hour=self.settings.hour,
            enabled=self.settings.enabled,
        ):
            return None
        message = self.build_message(now)
        try:
            self.notify(message)
        except Exception as exc:
            logger.exception("morning brief notification failed")
            self._emit(
                f"{self.settings.report_name}_failed",
                {
                    "reason": "notify_failed",
                    "error": str(exc)[:500],
                    "date": now.strftime("%Y-%m-%d"),
                },
            )
            return None
        self._mark_sent(now)
        self._emit(
            f"{self.settings.report_name}_sent",
            {
                "date": now.strftime("%Y-%m-%d"),
                "hour": now.hour,
                "message_chars": len(message),
                "weather_location": self.settings.weather_location or "auto",
                "email_configured": bool(self.settings.email_command),
                "calendar_configured": bool(self.settings.calendar_command),
            },
        )
        return message

    def build_message(self, now: datetime) -> str:
        date_line = format_spanish_date(now)
        sections = [
            f"{self.settings.greeting}\nHoy es {date_line}.",
            f"Clima: {self._weather_line()}",
            f"Agenda: {self._calendar_line()}",
            f"Correo: {self._email_line()}",
            self._pending_work_section(),
            self._agent_section(),
            self._system_section(),
        ]
        return "\n\n".join(section for section in sections if section.strip())

    def _local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def _weather_line(self) -> str:
        try:
            return self.weather_fetcher(self.settings.weather_location, self.settings.command_timeout_seconds)
        except Exception as exc:
            logger.warning("morning brief weather unavailable: %s", exc)
            return f"no disponible ({type(exc).__name__})"

    def _external_line(self, command: str | None, *, default: str) -> str:
        if not command:
            return default
        try:
            result = self.command_runner(command, self.settings.command_timeout_seconds)
        except Exception as exc:
            logger.warning("morning brief command failed: %s", exc)
            return f"error consultando conector ({type(exc).__name__})"
        return result or "sin novedades"

    def _calendar_line(self) -> str:
        if self.settings.calendar_command:
            return self._external_line(self.settings.calendar_command, default="sin novedades")
        try:
            return self.calendar_fetcher(self.settings.command_timeout_seconds) or "sin eventos hoy"
        except Exception as exc:
            logger.warning("morning brief calendar unavailable: %s", exc)
            return f"no disponible ({type(exc).__name__})"

    def _email_line(self) -> str:
        if self.settings.email_command:
            return self._external_line(self.settings.email_command, default="sin novedades")
        try:
            return self.email_fetcher(self.settings.command_timeout_seconds) or "sin correos prioritarios"
        except Exception as exc:
            logger.warning("morning brief email unavailable: %s", exc)
            return f"no disponible ({type(exc).__name__})"

    def _pending_work_section(self) -> str:
        lines: list[str] = ["Pendientes:"]
        if self.task_ledger is not None:
            try:
                tasks = self.task_ledger.list(statuses=ACTIVE_TASK_STATUSES, limit=5)
            except Exception:
                tasks = []
            lines.extend(
                f"- Task {task.task_id}: {task.status} - {_trim(task.objective, 90)}"
                for task in tasks
            )
        if self.job_service is not None:
            try:
                jobs = self.job_service.list(statuses=ACTIVE_JOB_STATUSES, limit=5)
            except Exception:
                jobs = []
            lines.extend(
                f"- Job {job.job_id}: {job.status} - {job.kind}"
                for job in jobs
            )
        if self.task_board is not None:
            try:
                board_tasks = [*self.task_board.pending()[:3], *self.task_board.active()[:3]]
            except Exception:
                board_tasks = []
            lines.extend(
                f"- Board {task.id}: {task.status.value} - {_trim(task.title, 90)}"
                for task in board_tasks
            )
        if self.pipeline is not None:
            try:
                runs = self.pipeline.list_active()
            except Exception:
                runs = []
            lines.extend(
                f"- Pipeline {run.issue_id}: {run.status} - {run.branch_name}"
                for run in runs[:5]
            )
        if len(lines) == 1:
            lines.append("- Sin tareas activas registradas.")
        return "\n".join(lines)

    def _agent_section(self) -> str:
        if self.auto_research is None:
            return ""
        lines = ["Agentes:"]
        try:
            names = self.auto_research.list_agents()
        except Exception:
            names = []
        paused = 0
        for name in names:
            try:
                state = self.auto_research.inspect(name)
            except Exception:
                continue
            if state.get("paused"):
                paused += 1
                reason = state.get("pause_reason") or state.get("last_action") or "paused"
                lines.append(f"- {name}: pausado ({_trim(str(reason), 80)})")
        if len(lines) == 1:
            lines.append(f"- {len(names)} activos, {paused} pausados.")
        return "\n".join(lines)

    def _system_section(self) -> str:
        parts: list[str] = []
        if self.metrics is not None:
            try:
                snapshot = self.metrics.snapshot()
            except Exception:
                snapshot = {}
            total_cost = _metrics_total_cost(snapshot)
            parts.append(f"costo estimado hoy ${float(total_cost or 0):.4f}")
        if self.observe is not None:
            try:
                recent = self.observe.recent_events(limit=50)
            except Exception:
                recent = []
            errors = [event for event in recent if "error" in str(event.get("event_type", "")).lower()]
            parts.append(f"{len(errors)} eventos de error recientes")
        if not parts:
            return ""
        return "Sistema: " + "; ".join(parts) + "."

    def _mark_sent(self, now: datetime) -> None:
        self.settings.stamp_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.stamp_path.write_text(now.strftime("%Y-%m-%d"), encoding="utf-8")

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("morning brief observe emit failed", exc_info=True)


def should_send_morning_brief(
    now: datetime,
    stamp_path: Path,
    *,
    hour: int,
    enabled: bool = True,
) -> bool:
    if not enabled:
        return False
    if now.hour != hour:
        return False
    today = now.strftime("%Y-%m-%d")
    if stamp_path.exists() and stamp_path.read_text(encoding="utf-8").strip() == today:
        return False
    return True


def format_spanish_date(value: datetime) -> str:
    weekday = SPANISH_WEEKDAYS[value.weekday()]
    month = SPANISH_MONTHS[value.month - 1]
    return f"{weekday} {value.day} de {month} de {value.year}"


def fetch_weather_summary(location: str, timeout_seconds: float = 10.0) -> str:
    target = quote(location.strip()) if location.strip() else ""
    url = f"https://wttr.in/{target}?format=%l:+%c+%t,+%C,+humedad+%h,+viento+%w"
    with urlopen(url, timeout=timeout_seconds) as response:
        text = response.read().decode("utf-8", errors="replace").strip()
    return _trim(text or "sin datos", 220)


def run_external_summary_command(command: str, timeout_seconds: float = 10.0) -> str:
    args = shlex.split(command)
    if not args:
        return ""
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return _trim(f"error exit {completed.returncode}: {output}", 300)
    return _trim(output, 1000)


def fetch_calendar_summary(timeout_seconds: float = 10.0) -> str:
    script = """
set todayStart to current date
set time of todayStart to 0
set todayEnd to todayStart + 1 * days
set eventLines to {}
tell application "Calendar"
    repeat with cal in calendars
        try
            set todaysEvents to every event of cal whose start date is greater than or equal to todayStart and start date is less than todayEnd
            repeat with ev in todaysEvents
                set eventTitle to summary of ev
                set eventStart to time string of (start date of ev)
                set end of eventLines to eventStart & " - " & eventTitle
            end repeat
        end try
    end repeat
end tell
if (count of eventLines) is 0 then return "sin eventos hoy"
set AppleScript's text item delimiters to linefeed
if (count of eventLines) > 5 then
    set shownLines to items 1 thru 5 of eventLines
    return (shownLines as text) & linefeed & "... " & ((count of eventLines) - 5) & " eventos mas"
end if
return eventLines as text
"""
    return _run_osascript(script, timeout_seconds=timeout_seconds, limit=1000)


def fetch_mail_summary(timeout_seconds: float = 10.0) -> str:
    script = """
set messageLines to {}
tell application "Mail"
    set unreadMessages to unread messages of inbox
    set unreadCount to count of unreadMessages
    set maxItems to unreadCount
    if maxItems > 5 then set maxItems to 5
    repeat with i from 1 to maxItems
        set msg to item i of unreadMessages
        set messageSubject to subject of msg
        set messageSender to sender of msg
        set end of messageLines to "- " & messageSender & ": " & messageSubject
    end repeat
end tell
if unreadCount is 0 then return "0 sin leer"
set AppleScript's text item delimiters to linefeed
return unreadCount & " sin leer" & linefeed & (messageLines as text)
"""
    return _run_osascript(script, timeout_seconds=timeout_seconds, limit=1000)


def _run_osascript(script: str, *, timeout_seconds: float, limit: int) -> str:
    completed = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        raise RuntimeError(_trim(output or f"osascript exit {completed.returncode}", 300))
    return _trim(output, limit)


def _metrics_total_cost(snapshot: dict[str, Any]) -> float:
    direct = snapshot.get("total_cost_usd") or snapshot.get("total_cost")
    if isinstance(direct, (int, float)):
        return float(direct)
    total = 0.0
    for value in snapshot.values():
        if isinstance(value, dict):
            raw = value.get("total_cost") or value.get("cost")
            if isinstance(raw, (int, float)):
                total += float(raw)
    return total


def _trim(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
