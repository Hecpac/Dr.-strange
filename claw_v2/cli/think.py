"""Read-only observability CLI over Claw's audit DB.

Subcommands:
  tail      Recent events, optional --type / --session filter.
  trace     Events for a given trace_id.
  spending  Cost rollup today (lane, provider, model, agent).
  circuit   Observation window state (frozen, thresholds, rolling cost).
  replay    Reconstruct an agent session's reasoning chain.
  failures  Aggregate failure events by tool + error (default:
            sdk_post_tool_use_failure) with per-turn concentration.

Bot does not have to be running. Reads the same SQLite the live bot writes to.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

from claw_v2.observation_window import ObservationWindowState
from claw_v2.observe import ObserveStream


_DEFAULT_DB_CANDIDATES: tuple[Path, ...] = (
    Path("data/claw.db"),
    Path.home() / ".claw" / "claw.db",
)


def _resolve_db_path(arg: str | None) -> Path:
    if arg:
        path = Path(arg).expanduser()
        if not path.exists():
            raise SystemExit(f"--db path does not exist: {path}")
        return path
    for candidate in _DEFAULT_DB_CANDIDATES:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    raise SystemExit("No claw.db found (tried data/claw.db, ~/.claw/claw.db). Pass --db <path>.")


def _short_ts(ts: object) -> str:
    text = str(ts or "")
    for sep in ("T", " "):
        if sep in text:
            try:
                return text.split(sep, 1)[1][:8]
            except Exception:
                continue
    return text[:19]


def _short_session(payload: dict) -> str:
    sid = str(payload.get("session_id") or "")
    return sid[-8:] if sid else "-"


def _format_event(event: dict) -> str:
    payload = event.get("payload") or {}
    event_type = str(event.get("event_type") or "?")
    summary = _payload_summary(event_type, payload, event)
    return f"{_short_ts(event.get('timestamp')):>8s}  {event_type:32s}  {_short_session(payload):>8s}  {summary}"


def _payload_summary(event_type: str, payload: dict, event: dict) -> str:
    if event_type == "dispatch_decision":
        text = (payload.get("text_preview") or "").replace("\n", " ")[:40]
        tried = payload.get("tried_handlers")
        if isinstance(tried, list):
            # F0.3c consolidated shape: one event per turn carrying every
            # handler considered. Show the selected route, how many handlers
            # were tried, and the distinct fall-through reasons so audits see
            # *why* a turn ended up at the brain.
            selected_route = payload.get("selected_route") or payload.get("route") or "?"
            selected_handler = payload.get("selected_handler")
            selected_part = f" selected={selected_handler}" if selected_handler else ""
            fall_reasons: list[str] = []
            for entry in tried:
                if not isinstance(entry, dict) or entry.get("captured"):
                    continue
                reason = entry.get("reason")
                if reason and reason not in fall_reasons:
                    fall_reasons.append(str(reason))
            reasons_part = f" reasons={','.join(fall_reasons[:4])}" if fall_reasons else ""
            return (
                f"selected={selected_route}{selected_part} tried={len(tried)}"
                f"{reasons_part} text={text!r}"
            )
        handler = payload.get("handler") or "?"
        route = payload.get("route") or "?"
        reason = payload.get("reason") or ""
        matched = payload.get("matched_pattern")
        matched_part = f" matched={matched}" if matched else ""
        return f"handler={handler} route={route} reason={reason}{matched_part} text={text!r}"
    if event_type in {"llm_response", "llm_call", "llm_audit"}:
        cost = payload.get("cost_estimate")
        tokens = payload.get("total_tokens") or payload.get("tokens")
        lane = event.get("lane") or payload.get("lane") or "?"
        provider = event.get("provider") or "?"
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "?"
        return f"lane={lane} provider={provider} tokens={tokens} cost={cost_str}"
    if event_type == "kairos_decide_failed":
        kind = payload.get("error_kind") or "?"
        err = (payload.get("error") or "").replace("\n", " ")[:60]
        return f"kind={kind} error={err!r}"
    if event_type == "circuit_breaker_tripped":
        return (
            f"breaker={payload.get('breaker')} value={payload.get('value')}"
            f" threshold={payload.get('threshold')} actor={payload.get('actor')}"
        )
    if event_type.startswith("observation_window_"):
        return " ".join(f"{k}={v}" for k, v in payload.items() if k != "session_id")[:120]
    if event_type.startswith("autonomous_task_") or event_type == "task_blocked_with_evidence":
        bits = [f"task={payload.get('task_id', '?')}"]
        if payload.get("status"):
            bits.append(f"status={payload['status']}")
        if payload.get("error"):
            bits.append(f"error={str(payload['error'])[:50]!r}")
        if payload.get("blockers"):
            blockers = payload["blockers"]
            first = blockers[0] if isinstance(blockers, list) and blockers else blockers
            bits.append(f"blocker={first}")
        return " ".join(bits)
    return _generic_summary(payload)


def _generic_summary(payload: dict) -> str:
    parts: list[str] = []
    for key, value in payload.items():
        if key in {"session_id", "trace_id", "span_id", "parent_span_id"}:
            continue
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={str(value)[:40]}")
            if sum(len(part) for part in parts) > 100:
                break
    return " ".join(parts)[:140]


def _filter_events(
    events: Iterable[dict], event_type: str | None, session: str | None
) -> Iterable[dict]:
    for event in events:
        if event_type and event.get("event_type") != event_type:
            continue
        if session:
            sid = (event.get("payload") or {}).get("session_id")
            if sid != session:
                continue
        yield event


def cmd_tail(args: argparse.Namespace, observe: ObserveStream) -> int:
    raw_limit = args.limit * 5 if (args.type or args.session) else args.limit
    events = observe.recent_events(limit=raw_limit)
    filtered = list(_filter_events(events, args.type, args.session))[: args.limit]
    if args.asc:
        filtered.reverse()
    for event in filtered:
        print(_format_event(event))
    return 0


def cmd_trace(args: argparse.Namespace, observe: ObserveStream) -> int:
    events = observe.trace_events(args.trace_id, limit=args.limit)
    for event in events:
        print(_format_event(event))
    return 0


def cmd_spending(args: argparse.Namespace, observe: ObserveStream) -> int:
    today = observe.spending_today()
    by_agent = observe.cost_per_agent_today()
    total = today.get("total") or 0.0
    print(f"Total cost today: ${total:.4f}")
    print(f"By lane: {json.dumps(today.get('by_lane', {}), sort_keys=True)}")
    print(f"By provider: {json.dumps(today.get('by_provider', {}), sort_keys=True)}")
    print(f"By model: {json.dumps(today.get('by_model', {}), sort_keys=True)}")
    if by_agent:
        print()
        print("Cost per agent today:")
        for agent, cost in sorted(by_agent.items(), key=lambda kv: -kv[1]):
            print(f"  {agent:20s}  ${cost:.4f}")
    return 0


def cmd_circuit(args: argparse.Namespace, observe: ObserveStream) -> int:
    db_path = _resolve_db_path(args.db)
    state_path = db_path.parent / "observation_window.json"
    window = ObservationWindowState(observe=observe, state_path=state_path)
    print(json.dumps(window.status_payload(), indent=2, sort_keys=True))
    return 0


def cmd_replay(args: argparse.Namespace, observe: ObserveStream) -> int:
    events = observe.recent_events(limit=args.scan)
    chain = [
        event
        for event in events
        if (event.get("payload") or {}).get("session_id") == args.session_id
    ]
    chain.reverse()
    print(f"# Session {args.session_id}  ({len(chain)} events shown)")
    for event in chain[: args.limit]:
        print(_format_event(event))
    return 0


def cmd_failures(args: argparse.Namespace, observe: ObserveStream) -> int:
    with observe._lock:
        totals = observe._conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(DISTINCT json_extract(payload, '$.turn_id')),
                   MIN(timestamp),
                   MAX(timestamp)
            FROM observe_stream
            WHERE event_type = ?
            """,
            (args.type,),
        ).fetchone()
        rows = observe._conn.execute(
            """
            SELECT COALESCE(json_extract(payload, '$.tool_name'), '?') AS tool,
                   substr(COALESCE(json_extract(payload, '$.error'), '(sin error)'), 1, ?) AS error,
                   COUNT(*) AS n
            FROM observe_stream
            WHERE event_type = ?
            GROUP BY tool, error
            ORDER BY n DESC
            LIMIT ?
            """,
            (args.error_chars, args.type, args.limit),
        ).fetchall()
        daily = observe._conn.execute(
            """
            SELECT date(timestamp) AS d, COUNT(*) AS n
            FROM observe_stream
            WHERE event_type = ?
            GROUP BY d
            ORDER BY d DESC
            LIMIT ?
            """,
            (args.type, args.days),
        ).fetchall()
    total, turns, first_ts, last_ts = totals
    if not total:
        print(f"Sin eventos {args.type!r} en esta DB.")
        return 0
    turns = turns or 0
    print(f"Evento: {args.type}")
    print(f"Total: {total}  |  turnos distintos: {turns}  |  rango: {first_ts} .. {last_ts}")
    if turns:
        ratio = total / turns
        hint = "cascadas (cwd roto / retry loops)" if ratio > 3 else "fallos dispersos"
        print(f"Concentración: {ratio:.1f} fallos/turno → {hint}")
    print()
    print(f"{'n':>5s}  {'tool':12s}  error")
    for tool, error, n in rows:
        print(f"{n:>5d}  {str(tool):12s}  {str(error).replace(chr(10), ' ')}")
    print()
    print("Por día (recientes):")
    for day, n in daily:
        print(f"  {day}  {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claw-think", description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("--db", help="Override path to claw.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tail = sub.add_parser("tail", help="Recent events (optionally filtered)")
    p_tail.add_argument("--limit", type=int, default=20)
    p_tail.add_argument("--type", help="Filter by event_type")
    p_tail.add_argument("--session", help="Filter by session_id (in payload)")
    p_tail.add_argument("--asc", action="store_true", help="Show oldest first")

    p_trace = sub.add_parser("trace", help="Events for a trace_id")
    p_trace.add_argument("trace_id")
    p_trace.add_argument("--limit", type=int, default=None)

    sub.add_parser("spending", help="Cost rollup today")
    sub.add_parser("circuit", help="Observation window state")

    p_replay = sub.add_parser("replay", help="Reconstruct a session's reasoning chain")
    p_replay.add_argument("session_id")
    p_replay.add_argument("--limit", type=int, default=200)
    p_replay.add_argument(
        "--scan", type=int, default=2000, help="Recent events to scan for the session"
    )

    p_failures = sub.add_parser("failures", help="Aggregate failure events by tool + error")
    p_failures.add_argument(
        "--type", default="sdk_post_tool_use_failure", help="event_type to aggregate"
    )
    p_failures.add_argument(
        "--limit", type=int, default=25, help="Top (tool, error) groups to show"
    )
    p_failures.add_argument("--days", type=int, default=14, help="Daily distribution window")
    p_failures.add_argument("--error-chars", type=int, default=90, help="Error message truncation")

    args = parser.parse_args(argv)
    db_path = _resolve_db_path(args.db)
    observe = ObserveStream(db_path)
    handlers = {
        "tail": cmd_tail,
        "trace": cmd_trace,
        "spending": cmd_spending,
        "circuit": cmd_circuit,
        "replay": cmd_replay,
        "failures": cmd_failures,
    }
    return handlers[args.cmd](args, observe)


if __name__ == "__main__":
    sys.exit(main())
