"""Stop notifier — push a brief message to Telegram when an autonomous task ends.

Hector launches long-running tasks (NotebookLM Deep Research, video pipelines,
Computer Use sessions, scheduled jobs) via the bot, then closes the chat and
goes back to whatever he was doing. Today he has to actively poll to know if
something finished. This module flips that: when an autonomous task reaches
a terminal state, we push a one-line summary to his Telegram chat so he sees
it without checking.

Design:
- Fire-and-forget. Any failure pushing the notification must NOT crash the
  bot or block the event loop. Errors are swallowed and logged.
- Gating. Only emit for tasks that ran long enough to matter (default 60s)
  OR are explicitly tagged. Conversational turns and skills that already
  surfaced their own response are skipped.
- Dedupe. Same task_id within a short window emits at most one notification
  to avoid double pings when both sub_agent_skill and autonomous_task_completed
  events fire for the same task.

Network is done via stdlib urllib in a daemon thread so we avoid pulling in
new deps and keep this module decoupled from the python-telegram-bot
event loop.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_LONG_RUNNING_SEC = 60.0
_DEFAULT_DEDUPE_WINDOW_SEC = 120.0
_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_NOTIFY_TIMEOUT_SEC = 15.0


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    """Synchronously POST a plain-text message to a Telegram chat.

    Self-contained (stdlib urllib, no event loop) so background-thread callers
    like the recovery-job drainer can notify the operator directly. Raises on a
    missing token/chat or any transport failure — callers that must not lose a
    promise (notify-then-act) rely on the raise to retry later.
    """
    if not token or not chat_id:
        raise ValueError("Telegram token and chat_id are required to send a message")
    url = _TELEGRAM_API_URL.format(token=token)
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=_NOTIFY_TIMEOUT_SEC) as response:
        response.read()


@dataclass(slots=True)
class StopNotifier:
    """Pushes a one-line stop notification to Telegram when an autonomous task ends.

    The notifier is intentionally idempotent and side-effect-light: every call
    is safe even if the underlying network is unreachable or the token is
    misconfigured. Errors are logged at WARNING and swallowed so the bot's
    main flow is never blocked.
    """

    token: str
    default_chat_id: str
    enabled: bool = True
    long_running_sec: float = _DEFAULT_LONG_RUNNING_SEC
    dedupe_window_sec: float = _DEFAULT_DEDUPE_WINDOW_SEC
    _recent_notifications: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def notify_completion(
        self,
        *,
        task_id: str,
        kind: str,
        status: str,
        summary: str,
        duration_sec: float | None = None,
        chat_id: str | None = None,
        force: bool = False,
    ) -> bool:
        """Queue a stop notification.

        Returns True if a notification was queued, False if it was skipped
        (gated, deduped, or notifier disabled). Always returns quickly; the
        actual HTTP send happens on a daemon thread.
        """
        if not self.enabled or not self.token:
            return False
        if not force and not self._is_long_running(duration_sec):
            return False
        with self._lock:
            if self._is_dedup_skip(task_id):
                return False
            self._recent_notifications[task_id] = time.time()
            self._gc_recent()
        target_chat = chat_id or self.default_chat_id
        if not target_chat:
            return False
        text = self._format_message(
            kind=kind,
            status=status,
            summary=summary,
            duration_sec=duration_sec,
        )
        thread = threading.Thread(
            target=self._send_safely,
            args=(target_chat, text, task_id),
            daemon=True,
            name=f"stop-notifier-{task_id[:12]}",
        )
        thread.start()
        return True

    def _is_long_running(self, duration_sec: float | None) -> bool:
        if duration_sec is None:
            return False
        return duration_sec >= self.long_running_sec

    def _is_dedup_skip(self, task_id: str) -> bool:
        seen_at = self._recent_notifications.get(task_id)
        if seen_at is None:
            return False
        return (time.time() - seen_at) < self.dedupe_window_sec

    def _gc_recent(self) -> None:
        cutoff = time.time() - self.dedupe_window_sec
        for tid in [t for t, ts in self._recent_notifications.items() if ts < cutoff]:
            self._recent_notifications.pop(tid, None)

    @staticmethod
    def _format_message(
        *,
        kind: str,
        status: str,
        summary: str,
        duration_sec: float | None,
    ) -> str:
        emoji = "✅" if status == "succeeded" else ("⚠️" if status in {"blocked", "interrupted"} else "❌")
        kind_label = kind.replace("_", " ").strip() or "tarea"
        duration_label = ""
        if duration_sec is not None:
            if duration_sec >= 3600:
                duration_label = f" · {duration_sec / 3600:.1f}h"
            elif duration_sec >= 60:
                duration_label = f" · {duration_sec / 60:.1f}m"
            else:
                duration_label = f" · {int(duration_sec)}s"
        # Trim summary defensively.
        cleaned = (summary or "").strip().splitlines()
        first_line = cleaned[0] if cleaned else ""
        if len(first_line) > 220:
            first_line = first_line[:217] + "..."
        return f"{emoji} {kind_label} → {status}{duration_label}\n{first_line}".strip()

    def _send_safely(self, chat_id: str, text: str, task_id: str) -> None:
        try:
            url = _TELEGRAM_API_URL.format(token=self.token)
            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "disable_notification": False,
                "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_NOTIFY_TIMEOUT_SEC) as resp:
                resp.read(64)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning(
                "stop_notifier send failed task_id=%s err=%s",
                task_id,
                exc,
            )
        except Exception:
            logger.exception("stop_notifier send unexpected failure task_id=%s", task_id)


def build_stop_notifier(
    *,
    config: Any,
    enabled: bool | None = None,
) -> StopNotifier | None:
    """Build a StopNotifier from runtime config. Returns None if disabled or unconfigured.

    Looks for `telegram_bot_token` and `telegram_allowed_user_id` on the config
    object. If either is missing the notifier is not built and the bot keeps
    operating without stop notifications.
    """
    token = getattr(config, "telegram_bot_token", None) or ""
    chat_id = getattr(config, "telegram_allowed_user_id", None) or ""
    if not token or not chat_id:
        return None
    if enabled is None:
        enabled = bool(getattr(config, "stop_notifier_enabled", True))
    return StopNotifier(
        token=str(token),
        default_chat_id=str(chat_id),
        enabled=bool(enabled),
    )
