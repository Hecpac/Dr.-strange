from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from claw_v2.redaction import redact_sensitive

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
ATTENTION_TASK_STATUSES = ("failed", "timed_out", "lost")
RECENT_DONE_TASK_STATUSES = ("succeeded", "cancelled")
ATTENTION_VERIFICATION_STATUSES = ("blocked", "missing_evidence", "pending", "failed")
OPEN_OR_PROBLEM_VERIFICATION_STATUSES = (
    "blocked",
    "failed",
    "interrupted",
    "missing_evidence",
    "pending",
)
JOURNAL_TASK_LIMIT = 100
JOURNAL_DISPLAY_LIMIT = 100
INTERNAL_RUNTIMES = {"brain_fallback", "brain_tooluse", "brain_tool_use"}
INTERNAL_OBJECTIVE_PREFIXES = (
    "brain fallback tool-use turn",
    "brain tool-use turn",
    "brain fallback turn",
)
ALERT_EVENT_KEYWORDS = (
    "error",
    "fail",
    "failed",
    "blocked",
    "lost",
    "timeout",
    "quality_guard",
    "actionable_no_match",
    "approval_required",
)
EMPTY_CALENDAR_MARKERS = ("sin eventos", "sin novedades", "0 eventos")
EMPTY_EMAIL_MARKERS = ("0 sin leer", "sin correos", "sin novedades")


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
        approvals: Any | None = None,
        memory: Any | None = None,
        llm_router: Any | None = None,
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
        self.approvals = approvals
        self.memory = memory
        self.llm_router = llm_router
        self.clock = clock or self._local_now
        self.weather_fetcher = weather_fetcher or fetch_weather_summary
        self.command_runner = command_runner or run_external_summary_command
        self.email_fetcher = email_fetcher or fetch_mail_summary
        self.calendar_fetcher = calendar_fetcher or fetch_calendar_summary
        self._source_records: list[dict[str, Any]] = []
        self._brief_counts: dict[str, int] = {}
        self._last_brief_diagnostics: dict[str, Any] = {}

    def _reset_brief_capture(self) -> None:
        self._source_records = []
        self._brief_counts = {
            "work_items": 0,
            "approval_items": 0,
            "context_items": 0,
            "alert_items": 0,
            "journal_tasks": 0,
            "journal_jobs": 0,
            "journal_pending": 0,
        }

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
                **self._last_brief_diagnostics,
            },
        )
        return message

    def build_message(self, now: datetime) -> str:
        self._reset_brief_capture()
        if self.llm_router is not None:
            try:
                rendered = self._render_via_llm(now)
                if rendered.strip():
                    self._emit(
                        f"{self.settings.report_name}_llm_rendered",
                        {"chars": len(rendered)},
                    )
                    return rendered
            except Exception as exc:
                logger.exception("brief LLM render failed; falling back to template")
                self._emit(
                    f"{self.settings.report_name}_llm_failed",
                    {"reason": "llm_render_failed", "error": str(exc)[:300]},
                )
        return self._build_template_message(now)

    def _build_template_message(self, now: datetime) -> str:
        self._reset_brief_capture()
        date_line = format_spanish_date(now)
        weather_line = self._weather_line()
        calendar_line = self._calendar_line()
        email_line = self._email_line()
        journal = self._build_agent_journal(now)
        sections = [
            f"{self.settings.greeting}\nHoy es {date_line}.",
            f"Clima: {weather_line}" if weather_line else "",
            f"Agenda: {calendar_line}" if calendar_line else "",
            f"Correo: {email_line}" if email_line else "",
            self._journal_section(journal),
            self._work_section(now=now),
            self._approval_section(now),
            self._session_context_section(),
            self._agent_section(),
            self._system_section(),
            self._source_section(),
        ]
        message = "\n\n".join(section for section in sections if section.strip())
        diagnostics = self._brief_diagnostics()
        self._last_brief_diagnostics = diagnostics
        if diagnostics["low_signal"]:
            message = (
                f"{message}\n\n"
                "Diagnostico: brief de baja senal. No encontre tareas activas, "
                "aprobaciones, contexto de sesion ni alertas recientes; revisa "
                "conectores si esperabas agenda/correo con contenido."
            )
            self._emit(f"{self.settings.report_name}_low_signal", diagnostics)
        return message

    def _render_via_llm(self, now: datetime) -> str:
        facts = self._extract_brief_facts(now)
        system_prompt = self._brief_system_prompt(facts)
        user_prompt = self._brief_user_prompt(facts)
        response = self.llm_router.ask(
            user_prompt,
            system_prompt=system_prompt,
            lane="judge",
            evidence_pack={
                "report": self.settings.report_name,
                "date": facts.get("date", ""),
                "journal": facts.get("journal", {}),
            },
            max_budget=0.40,
            timeout=60.0,
        )
        rendered = str(getattr(response, "content", "") or "").strip()
        journal_text = self._journal_section(facts.get("journal") or {}, hard_evidence=True)
        self._last_brief_diagnostics = self._brief_diagnostics()
        if not journal_text:
            return rendered
        if not rendered:
            return journal_text
        return f"{rendered}\n\n{journal_text}"

    def _extract_brief_facts(self, now: datetime) -> dict[str, Any]:
        """Build a sanitized snapshot for LLM rendering.

        Never includes raw task_id / session_id / job_id values — only
        objective/summary text. Unavailable fuentes are omitted, not
        rendered as `no disponible (RuntimeError)`.
        """
        facts: dict[str, Any] = {
            "greeting": self.settings.greeting,
            "date": format_spanish_date(now),
            "report_name": self.settings.report_name,
            "report_kind": self._report_kind(),
        }
        try:
            weather = self.weather_fetcher(
                self.settings.weather_location, self.settings.command_timeout_seconds
            )
            if weather and weather.strip():
                facts["weather"] = _trim(weather, 220)
        except Exception:
            pass
        cal = self._safe_external_value(
            self.settings.calendar_command, self.calendar_fetcher
        )
        if cal:
            facts["calendar"] = cal
        mail = self._safe_external_value(
            self.settings.email_command, self.email_fetcher
        )
        if mail:
            facts["email"] = mail
        work = self._safe_work_facts()
        if work:
            facts["work"] = work
        journal = self._build_agent_journal(now)
        facts["journal"] = journal
        approvals = self._safe_approval_facts(now)
        if approvals:
            facts["approvals"] = approvals
        cost = self._safe_cost()
        if cost is not None:
            facts["cost_usd"] = round(cost, 4)
        return facts

    def _safe_external_value(
        self, command: str | None, fetcher: Callable[[float], str]
    ) -> str | None:
        if command:
            try:
                value = self.command_runner(command, self.settings.command_timeout_seconds)
            except Exception:
                return None
        else:
            try:
                value = fetcher(self.settings.command_timeout_seconds)
            except Exception:
                return None
        text = (value or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if any(marker in lowered for marker in EMPTY_CALENDAR_MARKERS):
            return None
        if any(marker in lowered for marker in EMPTY_EMAIL_MARKERS):
            return None
        if lowered.startswith("error ") or lowered.startswith("no disponible"):
            return None
        return _trim(text, 400)

    def _safe_work_facts(self) -> dict[str, Any] | None:
        if self.task_ledger is None:
            return None
        try:
            tasks = self.task_ledger.list(limit=20)
        except Exception:
            return None
        attention = [
            _trim(getattr(t, "error", "") or getattr(t, "summary", "") or t.objective, 120)
            for t in tasks
            if str(getattr(t, "status", "")) in ATTENTION_TASK_STATUSES
            or str(getattr(t, "verification_status", "")) in ATTENTION_VERIFICATION_STATUSES
        ][:5]
        done = [
            _trim(getattr(t, "summary", "") or t.objective, 120)
            for t in tasks
            if str(getattr(t, "status", "")) in RECENT_DONE_TASK_STATUSES
        ][:5]
        active = [
            _trim(t.objective, 120)
            for t in tasks
            if str(getattr(t, "status", "")) in ACTIVE_TASK_STATUSES
        ][:5]
        if not (attention or done or active):
            return None
        return {"attention": attention, "recent_done": done, "active": active}

    def _safe_approval_facts(self, now: datetime) -> dict[str, int] | None:
        if self.approvals is None:
            return None
        try:
            approvals = self.approvals.list_pending()
        except Exception:
            return None
        if not approvals:
            return {"total": 0}
        audit = self._classify_pending_approvals(approvals, now=now)
        return {
            "total": len(approvals),
            "still_needed": int(audit.get("still_needed", 0)),
            "stale": int(audit.get("stale", 0)),
            "duplicate": int(audit.get("duplicate", 0)),
            "expired": int(audit.get("expired", 0)),
        }

    def _safe_cost(self) -> float | None:
        if self.metrics is None:
            return None
        try:
            snapshot = self.metrics.snapshot()
        except Exception:
            return None
        return _metrics_total_cost(snapshot)

    def _brief_system_prompt(self, facts: dict[str, Any]) -> str:
        if str(facts.get("report_kind") or "") == "evening":
            frame = (
                "Eres Dr. Strange cerrando el día operativo de Hector por "
                "Telegram. Es un corte del día basado en bitácora real."
            )
            timing = (
                "Ventana: desde el inicio de hoy hasta este momento. Cierra "
                "con lo que queda pendiente para mañana si existe evidencia."
            )
        else:
            frame = (
                "Eres Dr. Strange arrancando el día operativo de Hector por "
                "Telegram. Es continuidad precisa desde el día anterior."
            )
            timing = (
                "Ventana: ayer completo y el estado abierto al iniciar hoy. "
                "Debe quedar claro qué se retoma hoy y con qué fecha exacta."
            )
        return (
            f"{frame}\n"
            f"{timing}\n"
            "\n"
            "No escribes un prompt diario ni un boletín inventado: escribes "
            "pensamiento operacional observable del agente. Eso significa "
            "objetivo, decisión, evidencia, resultado, bloqueo y siguiente "
            "acción cuando existan en la bitácora. No reveles cadena interna "
            "de pensamiento.\n"
            "\n"
            "Estilo OBLIGATORIO:\n"
            "- Tono mensaje de chat, no de boletín. Español neutral LatAm (tú).\n"
            "- Fechas precisas. No uses 'ayer' o 'hoy' sin que también exista "
            "la fecha completa en el contexto o la respuesta.\n"
            "- Usa los NÚMEROS y NOMBRES literales del contexto. Si te digo "
            "'48492 tokens', escribe '48k tokens' o '48492 tokens'. NO digas "
            "'tamaño grande' ni 'algunos archivos'. Concreto, no abstracto.\n"
            "- Si hay tareas en la sección DIARIO OPERACIONAL VERIFICADO, no "
            "las omitas. Puedes agrupar solo si mantienes conteo y nombres.\n"
            "\n"
            "PROHIBIDO:\n"
            "- Frases de reporte: 'te escribo', 'este brief', 'hoy tuvimos', "
            "'varios problemas', 'hay que', 'debemos', 'me preocupa'.\n"
            "- Cierres formulaicos: 'cuídate', 'descansa bien', 'buenas noches'.\n"
            "- Transiciones de ensayo: 'Además', 'Por otro lado', 'En cuanto a'.\n"
            "- Bullets, listas, headers, asteriscos, markdown.\n"
            "- Frases vagas: 'estuvo bien', 'varios', 'sigue siendo', 'problema "
            "serio'. Demasiado abstracto.\n"
            "- Inventar datos que no estén en el contexto. Si una fuente está "
            "vacía o ausente, NO la menciones.\n"
            "\n"
            "El ledger manda. Si no hay evidencia de una tarea, no afirmes que "
            "ocurrió."
        )

    def _brief_user_prompt(self, facts: dict[str, Any]) -> str:
        lines: list[str] = [
            f"Apertura sugerida: {facts.get('greeting', '')}",
            f"Hoy: {facts.get('date', '')}",
            f"Tipo de reporte: {facts.get('report_name', '')}",
        ]
        journal_text = self._format_journal_for_prompt(facts.get("journal") or {})
        if journal_text:
            lines.append(journal_text)
        if facts.get("weather"):
            lines.append(f"Clima ahora: {facts['weather']}")
        if facts.get("calendar"):
            lines.append(f"Eventos hoy: {facts['calendar']}")
        if facts.get("email"):
            lines.append(f"Mail relevante: {facts['email']}")
        work = facts.get("work") or {}
        if work.get("attention"):
            lines.append("Lo que se rompió o quedó pendiente:")
            for obj in work["attention"]:
                lines.append(f"  · {obj}")
        if work.get("recent_done"):
            if str(facts.get("report_kind") or "") == "evening":
                lines.append("Lo que cerró durante el corte de hoy:")
            else:
                lines.append("Lo que cerró recientemente y afecta la continuidad:")
            for obj in work["recent_done"]:
                lines.append(f"  · {obj}")
        if work.get("active"):
            lines.append("Sigue corriendo ahora:")
            for obj in work["active"]:
                lines.append(f"  · {obj}")
        approvals = facts.get("approvals") or {}
        if approvals.get("total"):
            extras = []
            if approvals.get("stale"):
                extras.append(f"{approvals['stale']} stale")
            if approvals.get("duplicate"):
                extras.append(f"{approvals['duplicate']} duplicadas")
            if approvals.get("expired"):
                extras.append(f"{approvals['expired']} expiradas")
            qual = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"Aprobaciones esperando: {approvals['total']}{qual}")
        if "cost_usd" in facts and facts["cost_usd"] > 0:
            lines.append(f"Gasto LLM hoy: ${facts['cost_usd']:.2f}")
        lines.append(
            "\nRedacta solo con la evidencia anterior. No conviertas esto en "
            "prompt diario. Si hay pendientes, deben quedar explícitos con "
            "continuación concreta."
        )
        return "\n".join(lines)

    def _report_kind(self) -> str:
        return "evening" if self.settings.report_name == "evening_brief" else "morning"

    def _localize_datetime(self, value: datetime) -> datetime:
        tz = ZoneInfo(self.settings.timezone)
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    def _journal_window(self, now: datetime) -> dict[str, Any]:
        local_now = self._localize_datetime(now)
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        kind = self._report_kind()
        if kind == "evening":
            start = today_start
            end = local_now
            label = "corte de hoy"
            continuation = "mañana"
        else:
            start = today_start - timedelta(days=1)
            end = today_start
            label = "continuación de ayer"
            continuation = "retomar hoy"
        return {
            "kind": kind,
            "now": local_now,
            "start": start,
            "end": end,
            "label": label,
            "continuation_label": continuation,
            "today_date": format_spanish_date(local_now),
            "start_date": format_spanish_date(start),
            "end_date": format_spanish_date(end),
            "timezone": self.settings.timezone,
        }

    def _build_agent_journal(self, now: datetime) -> dict[str, Any]:
        window = self._journal_window(now)
        start_ts = float(window["start"].timestamp())
        end_ts = float(window["end"].timestamp())
        tasks = [task for task in self._journal_task_records() if self._task_is_user_visible(task)]
        touched_tasks = [
            self._task_entry(task, now=window["now"])
            for task in tasks
            if self._task_touched_in_window(task, start_ts=start_ts, end_ts=end_ts)
        ]
        carryover_tasks = [
            self._task_entry(task, now=window["now"])
            for task in tasks
            if self._task_is_open_or_problem(task)
        ]
        jobs = [
            self._job_entry(job, now=window["now"])
            for job in self._journal_job_records()
            if self._job_touched_in_window(job, start_ts=start_ts, end_ts=end_ts)
            or str(getattr(job, "status", "")) in ACTIVE_JOB_STATUSES
        ]
        sessions = self._journal_session_entries()
        self._brief_counts["journal_tasks"] = len(touched_tasks)
        self._brief_counts["journal_pending"] = len(carryover_tasks)
        self._brief_counts["journal_jobs"] = len(jobs)
        self._brief_counts["work_items"] = max(
            int(self._brief_counts.get("work_items", 0)),
            len(touched_tasks) + len(carryover_tasks) + len(jobs),
        )
        return {
            "kind": window["kind"],
            "label": window["label"],
            "continuation_label": window["continuation_label"],
            "today_date": window["today_date"],
            "start_date": window["start_date"],
            "end_date": window["end_date"],
            "timezone": window["timezone"],
            "window_start": window["start"].isoformat(),
            "window_end": window["end"].isoformat(),
            "tasks_touched": touched_tasks[:JOURNAL_DISPLAY_LIMIT],
            "tasks_touched_total": len(touched_tasks),
            "tasks_touched_omitted": max(0, len(touched_tasks) - JOURNAL_DISPLAY_LIMIT),
            "carryover_tasks": carryover_tasks[:JOURNAL_DISPLAY_LIMIT],
            "carryover_total": len(carryover_tasks),
            "carryover_omitted": max(0, len(carryover_tasks) - JOURNAL_DISPLAY_LIMIT),
            "jobs": jobs[:JOURNAL_DISPLAY_LIMIT],
            "jobs_total": len(jobs),
            "jobs_omitted": max(0, len(jobs) - JOURNAL_DISPLAY_LIMIT),
            "session_continuity": sessions,
        }

    def _journal_task_records(self) -> list[Any]:
        if self.task_ledger is None:
            return []
        try:
            return self.task_ledger.list(limit=JOURNAL_TASK_LIMIT)
        except Exception:
            return []

    def _journal_job_records(self) -> list[Any]:
        if self.job_service is None:
            return []
        try:
            return self.job_service.list(limit=JOURNAL_TASK_LIMIT)
        except Exception:
            return []

    def _task_touched_in_window(self, task: Any, *, start_ts: float, end_ts: float) -> bool:
        return any(
            start_ts <= ts < end_ts
            for ts in self._record_timestamps(
                task, ("created_at", "started_at", "completed_at", "updated_at")
            )
        )

    def _job_touched_in_window(self, job: Any, *, start_ts: float, end_ts: float) -> bool:
        return any(
            start_ts <= ts < end_ts
            for ts in self._record_timestamps(
                job, ("created_at", "started_at", "completed_at", "updated_at")
            )
        )

    @staticmethod
    def _record_timestamps(record: Any, names: tuple[str, ...]) -> list[float]:
        values: list[float] = []
        for name in names:
            raw = getattr(record, name, None)
            if raw is None:
                continue
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                continue
            if ts > 0:
                values.append(ts)
        return values

    def _task_is_open_or_problem(self, task: Any) -> bool:
        status = str(getattr(task, "status", "") or "")
        verification = str(getattr(task, "verification_status", "") or "")
        return (
            status in ACTIVE_TASK_STATUSES
            or status in ATTENTION_TASK_STATUSES
            or verification in OPEN_OR_PROBLEM_VERIFICATION_STATUSES
        )

    @staticmethod
    def _task_is_user_visible(task: Any) -> bool:
        objective = str(getattr(task, "objective", "") or "").strip().lower()
        if any(objective.startswith(prefix) for prefix in INTERNAL_OBJECTIVE_PREFIXES):
            return False
        return True

    def _task_entry(self, task: Any, *, now: datetime) -> dict[str, str]:
        detail = getattr(task, "error", "") or getattr(task, "summary", "")
        timestamps = self._record_timestamps(
            task, ("updated_at", "completed_at", "started_at", "created_at")
        )
        touched_at = max(timestamps) if timestamps else 0.0
        return {
            "objective": _safe_text(getattr(task, "objective", ""), 180),
            "status": _safe_text(getattr(task, "status", "unknown"), 40),
            "verification": _safe_text(getattr(task, "verification_status", "unknown"), 60),
            "runtime": _safe_text(getattr(task, "runtime", "unknown"), 60),
            "detail": _safe_text(detail, 220),
            "touched": self._format_local_timestamp(touched_at, now=now),
        }

    def _job_entry(self, job: Any, *, now: datetime) -> dict[str, str]:
        detail = getattr(job, "error", "") or getattr(job, "result", "") or getattr(job, "checkpoint", "")
        timestamps = self._record_timestamps(
            job, ("updated_at", "completed_at", "started_at", "created_at")
        )
        touched_at = max(timestamps) if timestamps else 0.0
        return {
            "kind": _safe_text(getattr(job, "kind", "job"), 120),
            "status": _safe_text(getattr(job, "status", "unknown"), 40),
            "detail": _safe_text(detail, 180),
            "touched": self._format_local_timestamp(touched_at, now=now),
        }

    def _format_local_timestamp(self, ts: float, *, now: datetime) -> str:
        if ts <= 0:
            return ""
        try:
            value = datetime.fromtimestamp(ts, tz=now.tzinfo)
        except Exception:
            return ""
        return value.strftime("%Y-%m-%d %H:%M")

    def _journal_session_entries(self) -> list[dict[str, str]]:
        if self.memory is None:
            return []
        try:
            states = self.memory.list_session_states(limit=8)
        except Exception:
            return []
        entries: list[dict[str, str]] = []
        for state in states:
            current_goal = _safe_text(state.get("current_goal"), 160)
            pending_action = _safe_text(state.get("pending_action"), 160)
            verification = _safe_text(state.get("verification_status"), 60)
            task_queue = state.get("task_queue") if isinstance(state.get("task_queue"), list) else []
            if not (current_goal or pending_action or task_queue or verification not in {"", "unknown"}):
                continue
            entries.append(
                {
                    "goal": current_goal,
                    "pending": pending_action,
                    "verification": verification,
                    "queue": str(len(task_queue)) if task_queue else "",
                }
            )
        return entries[:8]

    def _journal_has_signal(self, journal: dict[str, Any]) -> bool:
        return bool(
            journal.get("tasks_touched")
            or journal.get("carryover_tasks")
            or journal.get("jobs")
            or journal.get("session_continuity")
        )

    def _journal_section(self, journal: dict[str, Any], *, hard_evidence: bool = False) -> str:
        if not journal:
            return ""
        if hard_evidence and not self._journal_has_signal(journal):
            return ""
        title = "Bitácora verificada" if hard_evidence else "Diario operacional del agente"
        lines = [
            f"{title}:",
            (
                f"- Fecha: {journal.get('today_date', '')}. "
                f"Ventana: {journal.get('label', '')}."
            ),
        ]
        touched = list(journal.get("tasks_touched") or [])
        carryover = list(journal.get("carryover_tasks") or [])
        sessions = list(journal.get("session_continuity") or [])
        touched_total = int(journal.get("tasks_touched_total", len(touched)))
        carryover_total = int(journal.get("carryover_total", len(carryover)))
        if touched_total:
            preview = self._summarize_task_objectives(touched, limit=4)
            extra = touched_total - min(len(preview), 4) if preview else touched_total
            suffix = f" y {extra} más" if extra > 0 else ""
            preview_text = ("; ".join(preview) + suffix) if preview else f"{touched_total} en total"
            lines.append(f"- Ejecutadas en la ventana ({touched_total}): {preview_text}.")
        if carryover_total:
            preview = self._summarize_task_objectives(carryover, limit=4)
            extra = carryover_total - min(len(preview), 4) if preview else carryover_total
            suffix = f" y {extra} más" if extra > 0 else ""
            preview_text = ("; ".join(preview) + suffix) if preview else f"{carryover_total} en total"
            label = journal.get("continuation_label", "continuar")
            lines.append(f"- Para {label} ({carryover_total}): {preview_text}.")
        if sessions:
            session_goals = [s.get("goal", "") for s in sessions if s.get("goal")]
            if session_goals:
                lines.append("- Hilos abiertos: " + "; ".join(session_goals[:3]) + ".")
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _summarize_task_objectives(entries: list[dict[str, str]], *, limit: int) -> list[str]:
        seen: list[str] = []
        for entry in entries:
            objective = (entry.get("objective") or "").strip()
            if not objective:
                continue
            if objective in seen:
                continue
            seen.append(objective)
            if len(seen) >= limit:
                break
        return seen

    def _format_journal_for_prompt(self, journal: dict[str, Any]) -> str:
        if not journal:
            return ""
        lines = [
            "\nDIARIO OPERACIONAL VERIFICADO",
            f"Fecha exacta: {journal.get('today_date', '')}",
            (
                f"Ventana: {journal.get('label', '')} "
                f"({journal.get('start_date', '')} -> {journal.get('end_date', '')}, "
                f"{journal.get('timezone', '')})"
            ),
            "Regla: cada tarea listada debe aparecer o quedar agrupada con conteo y nombre.",
        ]
        for heading, key, total_key in (
            ("Tareas ejecutadas/tocadas en la ventana", "tasks_touched", "tasks_touched_total"),
            ("Pendiente/continuación", "carryover_tasks", "carryover_total"),
        ):
            items = list(journal.get(key) or [])
            lines.append(f"{heading}: {journal.get(total_key, len(items))}")
            if not items:
                lines.append("  - ninguna registrada")
                continue
            lines.extend(self._format_task_entries(items, indent="  - "))
        jobs = list(journal.get("jobs") or [])
        lines.append(f"Jobs/agentes: {journal.get('jobs_total', len(jobs))}")
        if jobs:
            lines.extend(self._format_job_entries(jobs, indent="  - "))
        sessions = list(journal.get("session_continuity") or [])
        if sessions:
            lines.append("Continuidad de sesión:")
            for item in sessions[:5]:
                bits = [value for value in (item.get("goal"), item.get("pending")) if value]
                if item.get("queue"):
                    bits.append(f"cola={item['queue']}")
                if item.get("verification") and item["verification"] != "unknown":
                    bits.append(f"verificación={item['verification']}")
                if bits:
                    lines.append("  - " + "; ".join(bits[:4]))
        return "\n".join(lines)

    def _format_task_entries(self, entries: list[dict[str, str]], *, indent: str = "  - ") -> list[str]:
        lines: list[str] = []
        for entry in entries:
            objective = entry.get("objective") or "(sin objetivo)"
            status = entry.get("status") or "unknown"
            verification = entry.get("verification") or "unknown"
            detail = entry.get("detail") or ""
            touched = entry.get("touched") or ""
            suffix = f"; {detail}" if detail else ""
            when = f"; tocada {touched}" if touched else ""
            lines.append(f"{indent}{status} / {verification} - {objective}{suffix}{when}")
        return lines

    def _format_job_entries(self, entries: list[dict[str, str]], *, indent: str = "  - ") -> list[str]:
        lines: list[str] = []
        for entry in entries:
            kind = entry.get("kind") or "job"
            status = entry.get("status") or "unknown"
            detail = entry.get("detail") or ""
            touched = entry.get("touched") or ""
            suffix = f"; {detail}" if detail else ""
            when = f"; tocado {touched}" if touched else ""
            lines.append(f"{indent}{status} - {kind}{suffix}{when}")
        return lines

    def _local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.timezone))

    def _weather_line(self) -> str:
        try:
            result = self.weather_fetcher(self.settings.weather_location, self.settings.command_timeout_seconds)
        except Exception as exc:
            logger.warning("morning brief weather unavailable: %s", exc)
            self._record_source("clima", "wttr.in", "unavailable", type(exc).__name__)
            return ""
        status = "empty" if not str(result or "").strip() else "ok"
        self._record_source("clima", "wttr.in", status, self.settings.weather_location or "auto")
        return result or ""

    def _external_line(self, command: str | None, *, default: str, name: str) -> str:
        if not command:
            self._record_source(name, "not_configured", "empty", default)
            return ""
        try:
            result = self.command_runner(command, self.settings.command_timeout_seconds)
        except Exception as exc:
            logger.warning("morning brief command failed: %s", exc)
            self._record_source(name, f"command:{_trim(command, 80)}", "unavailable", type(exc).__name__)
            return ""
        value = (result or "").strip()
        if not value:
            self._record_source(name, f"command:{_trim(command, 80)}", "empty", "")
            return ""
        self._record_source(name, f"command:{_trim(command, 80)}", self._source_status(name, value), "")
        return value

    def _calendar_line(self) -> str:
        if self.settings.calendar_command:
            return self._external_line(self.settings.calendar_command, default="", name="agenda")
        try:
            result = self.calendar_fetcher(self.settings.command_timeout_seconds) or ""
        except Exception as exc:
            logger.warning("morning brief calendar unavailable: %s", exc)
            self._record_source("agenda", "apple_calendar", "unavailable", type(exc).__name__)
            return ""
        status = self._source_status("agenda", result)
        self._record_source("agenda", "apple_calendar", status, "")
        if status != "ok":
            return ""
        return result

    def _email_line(self) -> str:
        if self.settings.email_command:
            return self._external_line(self.settings.email_command, default="", name="correo")
        try:
            result = self.email_fetcher(self.settings.command_timeout_seconds) or ""
        except Exception as exc:
            logger.warning("morning brief email unavailable: %s", exc)
            self._record_source("correo", "apple_mail", "unavailable", type(exc).__name__)
            return ""
        status = self._source_status("correo", result)
        self._record_source("correo", "apple_mail", status, "")
        if status != "ok":
            return ""
        return result

    def _pending_work_section(self) -> str:
        return self._work_section()

    def _work_section(self, *, now: datetime | None = None) -> str:
        cutoff_ts = 0.0
        if now is not None:
            cutoff_ts = float(now.timestamp()) - (48 * 3600)
        lines: list[str] = []
        work_items = 0
        active_descriptions: list[str] = []
        attention_descriptions: list[str] = []
        done_descriptions: list[str] = []
        if self.task_ledger is not None:
            try:
                tasks = self.task_ledger.list(limit=20)
            except Exception:
                tasks = []
            for task in tasks:
                if not self._task_is_user_visible(task):
                    continue
                status = str(getattr(task, "status", ""))
                verification = str(getattr(task, "verification_status", ""))
                objective = _safe_text(getattr(task, "objective", ""), 90)
                if not objective:
                    continue
                if status in ACTIVE_TASK_STATUSES:
                    active_descriptions.append(objective)
                    continue
                touched = max(self._record_timestamps(task, ("updated_at", "completed_at", "started_at", "created_at")) or [0.0])
                if touched < cutoff_ts:
                    continue
                if status in ATTENTION_TASK_STATUSES or verification in ATTENTION_VERIFICATION_STATUSES:
                    attention_descriptions.append(objective)
                elif status in RECENT_DONE_TASK_STATUSES:
                    done_descriptions.append(objective)
            active_descriptions = active_descriptions[:5]
            attention_descriptions = attention_descriptions[:5]
            done_descriptions = done_descriptions[:5]
        if active_descriptions:
            lines.append("Corriendo ahora: " + "; ".join(active_descriptions) + ".")
            work_items += len(active_descriptions)
        if attention_descriptions:
            lines.append("Quedaron sin cerrar en las últimas 48h: " + "; ".join(attention_descriptions) + ".")
            work_items += len(attention_descriptions)
        if done_descriptions:
            lines.append("Cerradas recientes: " + "; ".join(done_descriptions) + ".")
            work_items += len(done_descriptions)
        if self.job_service is not None:
            try:
                jobs = self.job_service.list(statuses=ACTIVE_JOB_STATUSES, limit=5)
            except Exception:
                jobs = []
            job_descriptions = [
                _safe_text(getattr(job, "kind", "job"), 90)
                for job in jobs
                if _safe_text(getattr(job, "kind", "job"), 90)
            ]
            if job_descriptions:
                lines.append("Jobs en curso: " + "; ".join(job_descriptions) + ".")
                work_items += len(job_descriptions)
        if self.task_board is not None:
            try:
                board_tasks = [*self.task_board.pending()[:3], *self.task_board.active()[:3]]
            except Exception:
                board_tasks = []
            board_descriptions = [
                _safe_text(getattr(task, "title", ""), 90)
                for task in board_tasks
                if _safe_text(getattr(task, "title", ""), 90)
            ]
            if board_descriptions:
                lines.append("En board: " + "; ".join(board_descriptions) + ".")
                work_items += len(board_descriptions)
        if self.pipeline is not None:
            try:
                runs = self.pipeline.list_active()
            except Exception:
                runs = []
            pipeline_descriptions = [
                _safe_text(getattr(run, "branch_name", "") or getattr(run, "issue_id", ""), 90)
                for run in runs[:5]
                if _safe_text(getattr(run, "branch_name", "") or getattr(run, "issue_id", ""), 90)
            ]
            if pipeline_descriptions:
                lines.append("Pipelines activos: " + "; ".join(pipeline_descriptions) + ".")
                work_items += len(pipeline_descriptions)
        self._brief_counts["work_items"] = max(
            int(self._brief_counts.get("work_items", 0)),
            work_items,
        )
        if not lines:
            return ""
        return "Trabajo:\n" + "\n".join(lines)

    def _approval_section(self, now: datetime) -> str:
        if self.approvals is None:
            return ""
        try:
            approvals = self.approvals.list_pending()
        except Exception as exc:
            logger.warning("morning brief approvals unavailable: %s", exc)
            return f"Aprobaciones: no disponible ({type(exc).__name__})."
        self._brief_counts["approval_items"] = len(approvals)
        if not approvals:
            return "Aprobaciones: 0 pendientes."
        audit = self._classify_pending_approvals(approvals, now=now)
        lines = [
            (
                "Aprobaciones: "
                f"{len(approvals)} pendientes; "
                f"{audit['still_needed']} activas; "
                f"{audit['stale']} stale; "
                f"{audit['expired']} expiradas; "
                f"{audit['duplicate']} duplicadas."
            )
        ]
        seen_actions: set[str] = set()
        for item in approvals[:5]:
            summary = _safe_text(item.get("summary") or item.get("action") or "aprobacion pendiente", 160)
            age_hours = self._approval_age_hours(item, now=now)
            classification = self._classify_approval(item, now=now, seen_actions=seen_actions)
            related = self._approval_related_context(item)
            suffix = f"; {related}" if related else ""
            lines.append(
                f"- {classification}: {summary} (edad ~{age_hours:.1f}h{suffix}). "
                "No auto-apruebo."
            )
        return "\n".join(lines)

    def _session_context_section(self) -> str:
        if self.memory is None:
            return ""
        try:
            states = self.memory.list_session_states(limit=5)
        except Exception as exc:
            logger.warning("morning brief session context unavailable: %s", exc)
            return f"Contexto activo: no disponible ({type(exc).__name__})."
        lines = ["Contexto activo:"]
        for state in states:
            details: list[str] = []
            current_goal = _safe_text(state.get("current_goal"), 120)
            pending_action = _safe_text(state.get("pending_action"), 120)
            verification = _safe_text(state.get("verification_status"), 60)
            active_object = state.get("active_object") if isinstance(state.get("active_object"), dict) else {}
            mission = active_object.get("active_mission") or active_object.get("_mission") or {}
            mission_goal = ""
            if isinstance(mission, dict):
                mission_goal = _safe_text(mission.get("last_user_goal") or mission.get("objective"), 120)
            task_queue = state.get("task_queue") if isinstance(state.get("task_queue"), list) else []
            if current_goal:
                details.append(f"objetivo={current_goal}")
            if pending_action:
                details.append(f"pendiente={pending_action}")
            if mission_goal:
                details.append(f"mision={mission_goal}")
            if task_queue:
                details.append(f"queue={len(task_queue)}")
            if verification and verification != "unknown":
                details.append(f"verificacion={verification}")
            if not details:
                continue
            session_id = _safe_text(state.get("session_id"), 40) or "session"
            lines.append(f"- {session_id}: " + "; ".join(details[:4]))
        context_items = len(lines) - 1
        self._brief_counts["context_items"] = context_items
        if context_items == 0:
            lines.append("- Sin objetivo, pendiente o mision activa en session_state reciente.")
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
            alert_counts = self._alert_counts(recent)
            alert_total = sum(alert_counts.values())
            self._brief_counts["alert_items"] = alert_total
            if alert_counts:
                shown = ", ".join(f"{name}={count}" for name, count in list(alert_counts.items())[:5])
                parts.append(f"Alertas recientes: {shown}")
            else:
                parts.append("0 alertas recientes")
        if not parts:
            return ""
        return "Sistema: " + "; ".join(parts) + "."

    def _source_section(self) -> str:
        if not self._source_records:
            return ""
        parts = []
        for item in self._source_records:
            detail = str(item.get("detail") or "").strip()
            source = str(item.get("source") or "unknown").strip()
            suffix = f" ({_trim(detail, 80)})" if detail else ""
            parts.append(f"{item['name']}={item['status']}:{source}{suffix}")
        return "Fuentes: " + "; ".join(parts) + "."

    def _brief_diagnostics(self) -> dict[str, Any]:
        source_statuses = {item["name"]: item["status"] for item in self._source_records}
        operational_signal = (
            int(self._brief_counts.get("work_items", 0))
            + int(self._brief_counts.get("approval_items", 0))
            + int(self._brief_counts.get("context_items", 0))
            + int(self._brief_counts.get("alert_items", 0))
            + int(self._brief_counts.get("journal_tasks", 0))
            + int(self._brief_counts.get("journal_jobs", 0))
            + int(self._brief_counts.get("journal_pending", 0))
        )
        agenda_empty = source_statuses.get("agenda") in {"empty", "unavailable"}
        correo_empty = source_statuses.get("correo") in {"empty", "unavailable"}
        low_signal = operational_signal == 0 and agenda_empty and correo_empty
        return {
            "source_statuses": source_statuses,
            "work_items": int(self._brief_counts.get("work_items", 0)),
            "approval_items": int(self._brief_counts.get("approval_items", 0)),
            "context_items": int(self._brief_counts.get("context_items", 0)),
            "alert_items": int(self._brief_counts.get("alert_items", 0)),
            "journal_tasks": int(self._brief_counts.get("journal_tasks", 0)),
            "journal_jobs": int(self._brief_counts.get("journal_jobs", 0)),
            "journal_pending": int(self._brief_counts.get("journal_pending", 0)),
            "low_signal": low_signal,
        }

    def _record_source(self, name: str, source: str, status: str, detail: str) -> None:
        self._source_records.append(
            {
                "name": name,
                "source": _safe_text(source, 120),
                "status": status,
                "detail": _safe_text(detail, 120),
            }
        )

    def _source_status(self, name: str, value: str) -> str:
        lowered = str(value or "").strip().lower()
        if not lowered:
            return "empty"
        markers = EMPTY_EMAIL_MARKERS if name == "correo" else EMPTY_CALENDAR_MARKERS
        if any(marker in lowered for marker in markers):
            return "empty"
        if lowered.startswith("error ") or lowered.startswith("no disponible"):
            return "unavailable"
        return "ok"

    def _alert_counts(self, events: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            lowered = event_type.lower()
            if not any(keyword in lowered for keyword in ALERT_EVENT_KEYWORDS):
                continue
            counts[event_type] = counts.get(event_type, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def _approval_age_hours(self, item: dict[str, Any], *, now: datetime) -> float:
        try:
            created_at = float(item.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0
        if created_at <= 0:
            return 0.0
        return max(0.0, (now.timestamp() - created_at) / 3600.0)

    def _approval_risk_tier(self, item: dict[str, Any]) -> str:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        raw = str(metadata.get("risk_tier") or item.get("risk_tier") or "").lower()
        if "critical" in raw or "tier_3" in raw or "tier3" in raw:
            return "critical"
        if "medium" in raw or "tier_2" in raw or "tier2" in raw:
            return "medium"
        action = str(item.get("action") or item.get("summary") or "").lower()
        if any(token in action for token in ("deploy", "publish", "publicar", "delete", "borrar", "merge", "push")):
            return "critical"
        return "low"

    def _classify_approval(self, item: dict[str, Any], *, now: datetime, seen_actions: set[str]) -> str:
        status = str(item.get("status") or "")
        if status == "expired":
            return "expired"
        action_key = str(item.get("action") or item.get("summary") or "").strip()
        if action_key in seen_actions:
            return "duplicate"
        seen_actions.add(action_key)
        age_hours = self._approval_age_hours(item, now=now)
        risk = self._approval_risk_tier(item)
        if risk == "low" and age_hours >= 24:
            return "stale"
        if risk == "medium" and age_hours >= 72:
            return "stale"
        if risk == "critical" and age_hours >= 72:
            return "stale"
        return "still_needed"

    def _classify_pending_approvals(self, approvals: list[dict[str, Any]], *, now: datetime) -> dict[str, int]:
        counts = {
            "still_needed": 0,
            "stale": 0,
            "superseded": 0,
            "blocked": 0,
            "expired": 0,
            "duplicate": 0,
        }
        seen_actions: set[str] = set()
        for item in approvals:
            classification = self._classify_approval(item, now=now, seen_actions=seen_actions)
            counts[classification] = counts.get(classification, 0) + 1
        return counts

    def _approval_related_context(self, item: dict[str, Any]) -> str:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        for key in ("task_id", "mission_id", "session_id"):
            value = _safe_text(metadata.get(key) or item.get(key), 80)
            if value:
                return f"{key}={value}"
        return ""

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


def _safe_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(redact_sensitive(str(value), limit=limit)).strip()
