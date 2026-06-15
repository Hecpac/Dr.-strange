"""Approval gate factories bridging ToolRegistry dispatcher and ApprovalManager.

Paso 4 del SOP Tier Enforcement (HEC-14). The dispatcher (tools.py) only knows
how to call an `approval_gate(definition, args)` callable and expects it to
raise on denial. These factories adapt that contract to the persistent
ApprovalManager flow, so a Tier 3 tool call generates a pending-approval record
instead of an opaque PermissionError.

Two flavors:

- `build_telegram_approval_gate`: interactive — creates a pending record the
  user approves from Telegram via `/approve <id> <token>`. Raises
  `ApprovalPending` (distinct from PermissionError) so the bot can surface the
  approval prompt instead of treating it as a hard failure.

- `build_system_auto_approve_gate`: non-interactive — for the daemon/scheduler
  where no human is present at dispatch time. Creates a record and approves it
  internally via a pre-shared system token, logging to observe.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator

from claw_v2.approval import ApprovalManager, PendingApproval

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # avoid circular at runtime
    from claw_v2.tools import ApprovalGate, ToolDefinition


# Context variable controlling which gate the shared tool executor picks.
# Default is the interactive Telegram mode; daemon/Kairos code paths flip
# this to "system" via `system_approval_mode(reason=...)` context manager.
_DAEMON_REASON: ContextVar[str | None] = ContextVar("claw_daemon_reason", default=None)
_APPROVED_TOOL_CONTEXT: ContextVar[dict | None] = ContextVar(
    "claw_approved_tool_context", default=None
)


def approval_args_hash(args: dict[str, Any] | None) -> str:
    """Stable hash for a tool invocation's arguments.

    The hash is stored with approvals so an approved retry can authorize the
    same concrete invocation, not just any later call to the same Tier 3 tool.
    """
    try:
        canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=repr)
    except TypeError:
        canonical = repr(args or {})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def system_approval_mode(reason: str) -> Iterator[None]:
    """Within this block, the shared tool executor should use the system
    auto-approve gate instead of the interactive Telegram gate.

    Used by the daemon/Kairos/heartbeat for scheduled Tier 3 tool calls so
    they leave an audit trail in ApprovalManager (status=approved) without
    blocking on human input.
    """
    token = _DAEMON_REASON.set(reason)
    try:
        yield
    finally:
        _DAEMON_REASON.reset(token)


def current_daemon_reason() -> str | None:
    """Return the active daemon reason, or None when running in Telegram mode."""
    return _DAEMON_REASON.get()


@contextlib.contextmanager
def approved_tool_invocation(
    *,
    tool: str,
    approval_id: str,
    reason: str,
    args_hash: str | None = None,
) -> Iterator[None]:
    """Allow one already-approved interactive Tier 3 tool invocation.

    This is used when Hector approves a pending tool request from the same
    Telegram session and the bot retries the original instruction. The approval
    remains one-shot: a second Tier 3 call in the same retry must request a new
    approval.
    """
    token = _APPROVED_TOOL_CONTEXT.set(
        {
            "tool": tool,
            "approval_id": approval_id,
            "reason": reason,
            "args_hash": args_hash,
            "used": False,
        }
    )
    try:
        yield
    finally:
        _APPROVED_TOOL_CONTEXT.reset(token)


@dataclass(slots=True)
class ApprovalPending(Exception):
    """Raised by an interactive approval_gate to signal Tier 3 is pending.

    Not a PermissionError: callers (bot) treat this as "ask Hector" rather than
    "blocked". Carries the approval_id + token so the user can issue
    `/approve <id> <token>` from Telegram.
    """

    approval_id: str
    token: str
    tool: str
    summary: str
    risk_code: str | None = None
    required_confirmation: str | None = None
    diff_summary: str | None = None
    sensitive_paths: tuple[str, ...] = ()
    args_hash: str | None = None

    def __str__(self) -> str:
        return f"Tier 3 tool '{self.tool}' pending approval (id={self.approval_id})"


def build_telegram_approval_gate(
    approvals: ApprovalManager,
    *,
    notifier: Callable[[PendingApproval], None] | None = None,
) -> "ApprovalGate":
    """Return an approval_gate that registers a pending approval and raises.

    `notifier`, if provided, is invoked synchronously with the PendingApproval
    so the bot can push a Telegram alert to Hector. Notifier failures are
    logged at ERROR level but do not block — the approval record itself is
    the source of truth.
    """

    def gate(definition: "ToolDefinition", args: dict) -> None:
        args_hash = approval_args_hash(args)
        approved_context = _APPROVED_TOOL_CONTEXT.get()
        if (
            isinstance(approved_context, dict)
            and not approved_context.get("used")
            and approved_context.get("tool") == definition.name
            and (
                not approved_context.get("args_hash")
                or approved_context.get("args_hash") == args_hash
            )
        ):
            approved_context["used"] = True
            return
        summary = f"{definition.name}({', '.join(sorted(args.keys()))})"
        pending = approvals.create(
            action=f"tool:{definition.name}",
            summary=summary,
            metadata={
                "tool": definition.name,
                "tier": definition.tier,
                "args_keys": sorted(args.keys()),
                "args_hash": args_hash,
            },
            risk_basis=f"tier3_tool:{definition.name}",
        )
        payload = approvals.read(pending.approval_id)
        pending_metadata = payload.get("metadata") or {}
        if notifier is not None:
            try:
                notifier(pending)
            except Exception:
                logger.exception("approval notifier failed for %s", pending.approval_id)
        raise ApprovalPending(
            approval_id=pending.approval_id,
            token=pending.token,
            tool=definition.name,
            summary=summary,
            risk_code=pending_metadata.get("risk_code"),
            required_confirmation=pending_metadata.get("required_confirmation"),
            diff_summary=pending_metadata.get("diff_summary"),
            sensitive_paths=tuple(pending_metadata.get("sensitive_paths") or ()),
            args_hash=str(pending_metadata.get("args_hash") or args_hash),
        )

    return gate


def build_system_auto_approve_gate(
    approvals: ApprovalManager,
    *,
    reason: str = "system-scheduled",
) -> "ApprovalGate":
    """Return an approval_gate that auto-approves on behalf of the daemon.

    Writes a pending record, immediately stamps it approved via
    `approve_internal`, and returns. The record acts as an audit trail of
    autonomous Tier 3 executions the daemon performed without a human in the
    loop (e.g. scheduled heartbeats, Kairos cron).
    """

    def gate(definition: "ToolDefinition", args: dict) -> None:
        args_hash = approval_args_hash(args)
        pending = approvals.create(
            action=f"tool:{definition.name}",
            summary=f"[auto] {definition.name} ({reason})",
            metadata={
                "tool": definition.name,
                "tier": definition.tier,
                "args_keys": sorted(args.keys()),
                "args_hash": args_hash,
                "auto_approved_reason": reason,
            },
            risk_basis=f"system_auto_tier3_tool:{definition.name}:{reason}",
        )
        approvals.approve_internal(pending.approval_id)

    return gate
