"""Debounced restart decision for the Claw watchdog.

The watchdog (`ops/claw-watchdog.sh`, a 300s LaunchAgent) used to restart the
daemon on a *single* `diagnostics` reading of `status=critical` + a restartable
sub-condition. Transient critical readings during a daemon's own bootstrap
(port not yet listening, process mid-restart) could therefore make the watchdog
kill a daemon that was merely starting up — and the restart left another daemon
starting up, so it could self-perpetuate into a restart loop (2026-06-13).

This module keeps the *exact* restartable condition (diagnostics critical is NOT
weakened) and only debounces the watchdog's *action*:

- **bootstrap grace** — never act while the daemon started less than
  `bootstrap_grace_s` ago; give it time to finish coming up.
- **N-strikes** — require `strikes_required` consecutive restartable readings
  (persisted across the watchdog's separate process invocations) before acting,
  so a one-off transient never triggers a restart.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path.home() / ".claw" / "watchdog_state.json"

RESTART = "restart"
HOLD = "hold"
OK = "ok"

DEFAULT_BOOTSTRAP_GRACE_S = 120.0
DEFAULT_STRIKES_REQUIRED = 2


def is_restartable(checks: Mapping[str, object]) -> bool:
    """Exact restartable condition the watchdog used inline before extraction.

    Unchanged on purpose: requires the DB to be readable (never restart on a
    broken DB) and at least one liveness sub-check to be failing.
    """
    return bool(
        checks.get("status") == "critical"
        and checks.get("database_readable")
        and (
            not checks.get("process_running")
            or not checks.get("port_listening")
            or checks.get("heartbeat_stale")
            or checks.get("web_transport_serving") is False
        )
    )


@dataclass(frozen=True)
class WatchdogConfig:
    bootstrap_grace_s: float = DEFAULT_BOOTSTRAP_GRACE_S
    strikes_required: int = DEFAULT_STRIKES_REQUIRED

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WatchdogConfig":
        env = os.environ if env is None else env
        strikes = _env_int(env, "CLAW_WATCHDOG_STRIKES", DEFAULT_STRIKES_REQUIRED)
        grace = _env_float(
            env, "CLAW_WATCHDOG_BOOTSTRAP_GRACE_S", DEFAULT_BOOTSTRAP_GRACE_S
        )
        return cls(
            # A negative grace would silently disable the bootstrap window
            # (uptime < grace is never true); treat it as a misconfiguration
            # and fall back to the safe default.
            bootstrap_grace_s=grace if grace >= 0.0 else DEFAULT_BOOTSTRAP_GRACE_S,
            # A non-positive strike count is a misconfiguration (you cannot
            # require zero readings); fall back to the safe default rather than
            # restarting on the first transient.
            strikes_required=strikes if strikes >= 1 else DEFAULT_STRIKES_REQUIRED,
        )


@dataclass(frozen=True)
class WatchdogState:
    consecutive_restartable: int = 0


@dataclass(frozen=True)
class WatchdogDecision:
    action: str
    reason: str
    state: WatchdogState


def decide(
    checks: Mapping[str, object],
    *,
    process_uptime_s: float | None,
    state: WatchdogState,
    config: WatchdogConfig,
) -> WatchdogDecision:
    """Return the debounced action for one watchdog cycle.

    ``process_uptime_s`` is the live uptime of the daemon process, or None when
    it cannot be determined (e.g. the process is down) — in which case the
    bootstrap grace does not apply.
    """
    if not is_restartable(checks):
        return WatchdogDecision(OK, f"status={checks.get('status')}", WatchdogState(0))

    if process_uptime_s is not None and process_uptime_s < config.bootstrap_grace_s:
        return WatchdogDecision(
            HOLD,
            f"bootstrap grace ({process_uptime_s:.0f}s < {config.bootstrap_grace_s:.0f}s)",
            WatchdogState(0),
        )

    strikes = state.consecutive_restartable + 1
    if strikes >= config.strikes_required:
        return WatchdogDecision(
            RESTART,
            f"critical restartable x{strikes}",
            WatchdogState(0),
        )
    return WatchdogDecision(
        HOLD,
        f"strike {strikes}/{config.strikes_required}",
        WatchdogState(strikes),
    )


def run_cycle(
    report: Mapping[str, object],
    *,
    uptime_s: float | None,
    state_path: Path,
    config: WatchdogConfig,
) -> WatchdogDecision:
    """Load persisted strike state, decide, persist the new state, and return
    the decision. One call == one watchdog invocation."""
    checks = report.get("checks") if isinstance(report, Mapping) else None
    if not isinstance(checks, Mapping):
        checks = {}
    state = load_state(state_path)
    decision = decide(checks, process_uptime_s=uptime_s, state=state, config=config)
    save_state(state_path, decision.state)
    return decision


def load_state(path: Path) -> WatchdogState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, Mapping):
            return WatchdogState(
                consecutive_restartable=int(data.get("consecutive_restartable", 0))
            )
    except (OSError, ValueError, TypeError):
        pass
    # Missing, unreadable, or non-dict (e.g. a stray ``[1,2,3]``) state file:
    # start clean rather than crash this robustness-critical component.
    return WatchdogState(0)


def save_state(path: Path, state: WatchdogState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"consecutive_restartable": state.consecutive_restartable}),
        encoding="utf-8",
    )


def parse_etime_seconds(etime: str) -> float | None:
    """Parse a ``ps -o etime=`` value ([[DD-]HH:]MM:SS) into seconds.

    Returns None for an empty or unparseable value, in which case the caller
    treats the daemon's uptime as unknown and skips the bootstrap grace.
    """
    s = (etime or "").strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        day_part, s = s.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        return None
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Watchdog decision entrypoint. Reads a ``diagnostics --json`` report on
    stdin, takes ``--uptime <etime>`` (the daemon process's ``ps`` etime), and
    prints the debounced action (``restart``/``hold``/``ok``) on stdout for the
    shell wrapper to act on. A report it cannot parse prints ``ok`` (fail-safe:
    never restart on a bad read)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env

    uptime_str = ""
    if "--uptime" in argv:
        idx = argv.index("--uptime")
        if idx + 1 < len(argv):
            uptime_str = argv[idx + 1]
    uptime_s = parse_etime_seconds(uptime_str)

    config = WatchdogConfig.from_env(env)
    state_path = Path(env.get("CLAW_WATCHDOG_STATE_PATH") or DEFAULT_STATE_PATH)

    try:
        report = json.load(stdin)
    except (ValueError, TypeError):
        # An unreadable report is a non-confirmatory reading: it must break the
        # consecutive-critical chain, not silently preserve a prior strike.
        save_state(state_path, WatchdogState(0))
        print(OK)
        return 0

    decision = run_cycle(
        report, uptime_s=uptime_s, state_path=state_path, config=config
    )
    sys.stderr.write(f"claw-watchdog: {decision.action} ({decision.reason})\n")
    print(decision.action)
    return 0


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
