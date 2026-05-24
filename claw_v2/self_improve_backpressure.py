"""P0-G: backpressure for self-improve promote approvals.

Behavioral audit (2026-05-23) found 17 pending `promote_perf-optimizer`
approval JSON records — every self-improve tick proposed the same
promotion while Hector did not respond, so the pending queue grew
indefinitely. This module exposes two helpers the daemon calls before
running `auto_research.run_loop(agent_name)` so the same handler does
not spam the queue when its previous proposals are still unresolved.
"""

from __future__ import annotations

from typing import Any

SELF_IMPROVE_MAX_PENDING_PER_ACTION = 3
"""Default cap on pending promote_<agent> approvals before the loop pauses.

Three pendings is enough surface area for Hector to see the proposal
recur, but small enough that the queue does not pile up across days.
"""


def count_pending_promote_approvals(approvals: Any) -> dict[str, int]:
    """Return ``{action: count}`` for pending self-improve promote approvals.

    ``approvals`` must expose ``list_pending()`` (the production
    ``ApprovalManager`` already does). Actions whose ``action`` field does
    NOT start with ``promote_`` are ignored so the backpressure only
    targets the self-improve loop, never unrelated approvals
    (browser_use_task, tool:GPTImage, etc.).
    """
    counts: dict[str, int] = {}
    try:
        pendings = approvals.list_pending()
    except Exception:
        return counts
    for payload in pendings or ():
        action = str((payload or {}).get("action", "") or "")
        if not action.startswith("promote_"):
            continue
        counts[action] = counts.get(action, 0) + 1
    return counts


def should_pause_self_improve(
    approvals: Any,
    agent_name: str,
    *,
    threshold: int = SELF_IMPROVE_MAX_PENDING_PER_ACTION,
) -> tuple[bool, int]:
    """Return ``(paused, pending_count)`` for ``promote_<agent_name>``.

    ``paused`` is True when the pending count is at or above the
    threshold — the caller should skip this agent's experiment loop and
    emit ``self_improve_paused_backlog_too_high``.
    """
    counts = count_pending_promote_approvals(approvals)
    action = f"promote_{agent_name}"
    pending_count = int(counts.get(action, 0))
    return pending_count >= int(threshold), pending_count
