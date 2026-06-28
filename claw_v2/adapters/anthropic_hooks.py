"""SDK tool hooks and permission callback for the Claude SDK executor.

Split out of anthropic.py (D1, 2026-06-12). Pure move: each hook is now a
named factory with explicit dependencies (request, runtime_policy, observe,
mutation tracker) instead of a closure over the executor, but the hook bodies
must match the inlined closures they replace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from claw_v2.adapters.base import ADVISORY_LANES, LLMRequest
from claw_v2.approval_gate import ApprovalPending
from claw_v2.observe import ObserveStream
from claw_v2.redaction import redact_text
from claw_v2.runtime_policy import RuntimePolicyEngine
from claw_v2.tracing import trace_metadata

logger = logging.getLogger(__name__)

# Backstop for the delegation contract: high-confidence signals that a Bash
# command is about to drive Chrome/CDP, a browser, or desktop computer-use.
# Such work does not fit the brain turn's 300s wall and must be delegated.
# Worker lanes (delegated coordinator tasks) are NOT gated by this.
_INLINE_BROWSER_DRIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpeekaboo\b", re.IGNORECASE),
    re.compile(r"\bplaywright\b", re.IGNORECASE),
    re.compile(r"\bselenium\b", re.IGNORECASE),
    re.compile(r"\bchromedriver\b", re.IGNORECASE),
    re.compile(r"\bcliclick\b", re.IGNORECASE),
    re.compile(r"webSocketDebuggerUrl"),
    re.compile(r"/json/(?:list|version)\b"),
    re.compile(r":9(?:250|222)\b"),  # Chrome CDP debug ports used by the workspace
    re.compile(r"\bcomputer[-_]use\b", re.IGNORECASE),
)
# Absolute python script paths inside a command; their contents are folded into
# the scan so `python3 /path/_ig_publish.py` (CDP inside the script) is caught.
_SCRIPT_PATH_RE = re.compile(r"(/[^\s'\"]+\.py)\b")
_SCRIPT_SCAN_MAX_BYTES = 262_144

# Backstop for the durable-task contract (T12, 2026-06-12): when the durable
# channel failed (T10 lock storm) the brain improvised detached background
# processes — no ledger, no monitor, no completion notification — and the
# work died silently. T10 removed the motive; this removes the means.
# Worker lanes are NOT gated (the coordinator legitimately runs long processes
# under its own monitoring).
_DETACHED_PROCESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnohup\s"),
    re.compile(r"\bsetsid\s"),
    re.compile(r"\bdisown\b"),
)
# Background-based, not marker-based (review #100 round 2): in the brain lane
# ANY real backgrounding `&` is denied — a detached job has no ledger/monitor
# in the chat turn, so even `python long_job.py &` must be delegated. `&` is
# itself a shell separator, so it is matched whether it ends the string, is
# followed by `;`/newline, or is followed by another command (`& echo x`).
# The negative lookbehind/alternation exclude the logical-AND `&&`, the
# `&>`/`2>&1` redirections, and a `&` glued inside a token such as a URL query
# string (`?a=1&b=2`) — those are not background operators. A `&` inside a
# quoted string with spaces is a rare, tolerable false positive (the brain
# reformulates to foreground or delegates).
_BACKGROUND_TAIL_RE = re.compile(r"(?<![&>])&(?:\s*(?:[;\n]|$)|\s+\S)", re.MULTILINE)

# SDK tools that cannot mutate external state. Anything outside this set
# (Bash, Edit, Write, Task, MCP tools, ...) is treated as potentially mutating
# so a failed turn that already ran one is never replayed by fallback/retry.
_READ_ONLY_SDK_TOOLS = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "TodoWrite",
        "BashOutput",
        "NotebookRead",
        "ToolSearch",
    }
)


def _read_bounded_script(path: str) -> str:
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _SCRIPT_SCAN_MAX_BYTES:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _inline_browser_drive_reason(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """Return a deny reason if a Bash call would drive a browser/CDP/desktop.

    Scans the command and, when it runs a local ``.py`` script, that script's
    contents too. Conservative by design: only high-confidence markers, so a
    miss is preferred over blocking a benign local command.
    """
    if tool_name != "Bash":
        return None
    command = str((tool_input or {}).get("command") or "")
    if not command:
        return None
    haystacks = [command]
    for match in _SCRIPT_PATH_RE.finditer(command):
        content = _read_bounded_script(match.group(1))
        if content:
            haystacks.append(content)
    blob = "\n".join(haystacks)
    for pattern in _INLINE_BROWSER_DRIVE_PATTERNS:
        if pattern.search(blob):
            return "inline browser/CDP/computer-use drive in a chat turn"
    return None


def _detached_process_reason(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """Return a deny reason if a Bash call would launch a detached or
    backgrounded process from a chat turn.

    Background-based: in the brain lane any real `&` backgrounding is denied,
    not just long-running markers, because a detached job has no ledger in the
    chat turn. The regex excludes `&&`, `&>`/`2>&1` and URL query strings, so
    everyday foreground commands pass through.
    """
    if tool_name != "Bash":
        return None
    command = str((tool_input or {}).get("command") or "")
    if not command:
        return None
    for pattern in _DETACHED_PROCESS_PATTERNS:
        if pattern.search(command):
            return "detached background process launched from a chat turn"
    if _BACKGROUND_TAIL_RE.search(command):
        return "background process launched from a chat turn"
    return None


def _sdk_agent_dispatch_reason(tool_name: str) -> str | None:
    """Return a deny reason if the call invokes the SDK's ``Agent`` subagent
    dispatcher.

    The durable ``delegate_task`` contract (ledger, monitor, completion
    notification) is the sanctioned path; the bare SDK ``Agent`` tool bypasses
    it. Brain-lane only — worker lanes fall through to ``enforce()``, which
    denies via the empty ``allowed_contexts`` policy entry.
    """
    if tool_name == "Agent":
        return "sdk Agent subagent dispatcher is not the durable delegation path"
    return None


def _safe_runtime_policy_reason(reason: str) -> str:
    text = str(reason or "runtime policy blocked the command")
    text = re.sub(
        r"\bbinary\s+'([^']+)'\s+requires higher privilege level\s+\(not in the allowed whitelist\)",
        r"command '\1' is blocked by local execution policy",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\ballowed whitelist\b", "local execution policy", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwhitelist\b", "policy", text, flags=re.IGNORECASE)
    text = re.sub(r"\bruntime host\b", "local runtime", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBash tool\b|\btool Bash\b", "local tool", text, flags=re.IGNORECASE)
    return text


def make_pre_tool_use_hook(
    request: LLMRequest,
    *,
    runtime_policy: RuntimePolicyEngine,
    observe: ObserveStream | None,
) -> Callable[..., Any]:
    async def pre_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        if request.lane == "brain":
            drive_reason = _inline_browser_drive_reason(
                str(input_data.get("tool_name", "")),
                input_data.get("tool_input", {}),
            )
            if drive_reason:
                nudge = (
                    "Browser/CDP/computer-use cannot run inline in a chat turn (300s wall). "
                    "Delegate it with the delegate_task tool (mode=ops/publish/browse) — fold any "
                    "verification into the delegated objective — instead of running it here."
                )
                if observe is not None:
                    try:
                        observe.emit(
                            "brain_inline_browser_drive_blocked",
                            lane=request.lane,
                            provider="anthropic",
                            model=request.model,
                            **trace_metadata(request.evidence_pack),
                            payload={
                                "tool_name": str(input_data.get("tool_name", "")),
                                "reason": drive_reason,
                            },
                        )
                    except Exception:
                        logger.debug(
                            "brain_inline_browser_drive_blocked emit failed",
                            exc_info=True,
                        )
                return {
                    "systemMessage": f"Tool invocation blocked: {nudge}",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": nudge,
                    },
                }
            detach_reason = _detached_process_reason(
                str(input_data.get("tool_name", "")),
                input_data.get("tool_input", {}),
            )
            if detach_reason:
                nudge = (
                    "Detached/background processes cannot be launched from a chat turn: "
                    "they have no ledger, no monitor and no completion notification. "
                    "Delegate the work with the delegate_task tool so it runs durable "
                    "and reports back when it finishes."
                )
                if observe is not None:
                    try:
                        observe.emit(
                            "brain_detached_process_blocked",
                            lane=request.lane,
                            provider="anthropic",
                            model=request.model,
                            **trace_metadata(request.evidence_pack),
                            payload={
                                "tool_name": str(input_data.get("tool_name", "")),
                                "reason": detach_reason,
                            },
                        )
                    except Exception:
                        logger.debug(
                            "brain_detached_process_blocked emit failed",
                            exc_info=True,
                        )
                return {
                    "systemMessage": f"Tool invocation blocked: {nudge}",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": nudge,
                    },
                }
            sdk_agent_reason = _sdk_agent_dispatch_reason(
                str(input_data.get("tool_name", ""))
            )
            if sdk_agent_reason:
                nudge = (
                    "The Agent tool is the SDK's unmonitored subagent dispatcher and is "
                    "not allowed here: it has no task ledger, no monitor, and no "
                    "completion notification. Delegate the work with the delegate_task "
                    "tool so it runs durable and reports back when it finishes."
                )
                if observe is not None:
                    try:
                        observe.emit(
                            "brain_sdk_agent_dispatch_blocked",
                            lane=request.lane,
                            provider="anthropic",
                            model=request.model,
                            **trace_metadata(request.evidence_pack),
                            payload={
                                "tool_name": str(input_data.get("tool_name", "")),
                                "reason": sdk_agent_reason,
                            },
                        )
                    except Exception:
                        logger.debug(
                            "brain_sdk_agent_dispatch_blocked emit failed",
                            exc_info=True,
                        )
                return {
                    "systemMessage": f"Tool invocation blocked: {nudge}",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": nudge,
                    },
                }
        try:
            runtime_policy.enforce(
                input_data.get("tool_name", ""),
                input_data.get("tool_input", {}),
                context=request.lane,
            )
        except ApprovalPending as exc:
            reason = str(exc)
            return {
                "systemMessage": f"Tool invocation blocked: {reason}",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        except PermissionError as exc:
            reason = _safe_runtime_policy_reason(str(exc))
            # Observe the exact tool_name that hit "not declared" so the
            # post-deploy data/claw.db log resolves whether harness tools
            # (ToolSearch/Agent) really reach enforce() or are confabulated,
            # and proves mcp__claw__delegate_task never does.
            if observe is not None and "not declared" in str(exc):
                try:
                    observe.emit(
                        "runtime_policy_tool_not_declared",
                        lane=request.lane,
                        provider="anthropic",
                        model=request.model,
                        **trace_metadata(request.evidence_pack),
                        payload={
                            "tool_name": str(input_data.get("tool_name", "")),
                            "reason": reason,
                        },
                    )
                except Exception:
                    logger.debug(
                        "runtime_policy_tool_not_declared emit failed",
                        exc_info=True,
                    )
            return {
                "systemMessage": f"Tool invocation blocked: {reason}",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        return {"continue_": True}

    return pre_tool_use


def make_post_tool_use_hook(
    request: LLMRequest,
    *,
    observe: ObserveStream | None,
    track_mutation: Callable[[str], None],
) -> Callable[..., Any]:
    async def post_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        track_mutation(str(input_data.get("tool_name") or ""))
        if observe is not None:
            tool_name = str(input_data.get("tool_name") or "")
            payload = {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "session_id": input_data.get("session_id"),
                "tool_input": _tool_input_evidence(
                    tool_name,
                    input_data.get("tool_input"),
                ),
            }
            response_evidence = _tool_response_evidence(
                tool_name,
                input_data.get("tool_response"),
            )
            if response_evidence:
                payload["tool_response"] = response_evidence
            observe.emit(
                "sdk_post_tool_use",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
                **trace_metadata(request.evidence_pack),
                payload=payload,
            )
        return {}

    return post_tool_use


def make_post_tool_use_failure_hook(
    request: LLMRequest,
    *,
    observe: ObserveStream | None,
    track_mutation: Callable[[str], None],
) -> Callable[..., Any]:
    async def post_tool_use_failure(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        # A failed tool may still have produced partial side effects
        # (e.g. a Bash command that timed out after sending): count it.
        track_mutation(str(input_data.get("tool_name") or ""))
        if observe is not None:
            tool_name = str(input_data.get("tool_name") or "")
            tool_response = input_data.get("tool_response") or {}
            error_message = (
                input_data.get("error")
                or input_data.get("error_message")
                or input_data.get("stderr")
                or tool_response.get("error")
                or tool_response.get("error_message")
                or tool_response.get("stderr")
                or ""
            )
            observe.emit(
                "sdk_post_tool_use_failure",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
                **trace_metadata(request.evidence_pack),
                payload={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "session_id": input_data.get("session_id"),
                    "tool_input": _tool_input_evidence(
                        tool_name,
                        input_data.get("tool_input"),
                    ),
                    "error": str(error_message)[:1000],
                    "is_error": bool(tool_response.get("is_error"))
                    if isinstance(tool_response, dict)
                    else False,
                },
            )
        return {}

    return post_tool_use_failure


def make_stop_hook(
    request: LLMRequest,
    *,
    observe: ObserveStream | None,
) -> Callable[..., Any]:
    async def stop_hook(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        if observe is not None:
            observe.emit(
                "sdk_stop",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
                **trace_metadata(request.evidence_pack),
                payload={"session_id": input_data.get("session_id")},
            )
        return {}

    return stop_hook


def make_subagent_start_hook(
    request: LLMRequest,
    *,
    observe: ObserveStream | None,
) -> Callable[..., Any]:
    async def subagent_start(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        if observe is not None:
            observe.emit(
                "sdk_subagent_start",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
                **trace_metadata(request.evidence_pack),
                payload={
                    "agent_id": input_data.get("agent_id"),
                    "tool_use_id": tool_use_id,
                },
            )
        return {}

    return subagent_start


def make_subagent_stop_hook(
    request: LLMRequest,
    *,
    observe: ObserveStream | None,
) -> Callable[..., Any]:
    async def subagent_stop(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        if observe is not None:
            observe.emit(
                "sdk_subagent_stop",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
                **trace_metadata(request.evidence_pack),
                payload={
                    "agent_id": input_data.get("agent_id"),
                    "tool_use_id": tool_use_id,
                },
            )
        return {}

    return subagent_stop


def build_hooks(
    sdk: Any,
    request: LLMRequest,
    *,
    runtime_policy: RuntimePolicyEngine,
    observe: ObserveStream | None,
    mutation_tracker: list[str] | None = None,
) -> dict[str, list[Any]] | None:
    def _track_mutation(tool_name: str) -> None:
        if mutation_tracker is not None and tool_name and tool_name not in _READ_ONLY_SDK_TOOLS:
            mutation_tracker.append(tool_name)

    hooks: dict[str, list[Any]] = {}
    hooks["Stop"] = [sdk.HookMatcher(hooks=[make_stop_hook(request, observe=observe)])]
    hooks["SubagentStart"] = [
        sdk.HookMatcher(hooks=[make_subagent_start_hook(request, observe=observe)])
    ]
    hooks["SubagentStop"] = [
        sdk.HookMatcher(hooks=[make_subagent_stop_hook(request, observe=observe)])
    ]

    if request.lane not in ADVISORY_LANES:
        hooks["PreToolUse"] = [
            sdk.HookMatcher(
                hooks=[
                    make_pre_tool_use_hook(request, runtime_policy=runtime_policy, observe=observe)
                ]
            )
        ]
        hooks["PostToolUse"] = [
            sdk.HookMatcher(
                hooks=[
                    make_post_tool_use_hook(
                        request, observe=observe, track_mutation=_track_mutation
                    )
                ]
            )
        ]
        hooks["PostToolUseFailure"] = [
            sdk.HookMatcher(
                hooks=[
                    make_post_tool_use_failure_hook(
                        request, observe=observe, track_mutation=_track_mutation
                    )
                ]
            )
        ]

    if request.hooks:
        for event_name, matchers in request.hooks.items():
            hooks.setdefault(event_name, []).extend(matchers)
    return hooks


def build_can_use_tool(
    sdk_types: Any,
    request: LLMRequest,
    *,
    runtime_policy: RuntimePolicyEngine,
) -> Callable[..., Any]:
    allowed = set(request.allowed_tools or [])

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        if allowed and tool_name not in allowed:
            return sdk_types.PermissionResultDeny(
                message=f"Tool '{tool_name}' is not allowed in this execution.",
                interrupt=True,
            )

        try:
            runtime_policy.enforce(tool_name, input_data, context=request.lane)
        except ApprovalPending as exc:
            return sdk_types.PermissionResultDeny(message=str(exc), interrupt=True)
        except PermissionError as exc:
            return sdk_types.PermissionResultDeny(
                message=_safe_runtime_policy_reason(str(exc)),
                interrupt=True,
            )
        return sdk_types.PermissionResultAllow(updated_input=input_data)

    return can_use_tool


def _tool_input_evidence(tool_name: str, tool_input: Any) -> dict[str, str]:
    if not isinstance(tool_input, dict):
        return {}
    allowed_by_tool = {
        "Bash": ("command", "cmd"),
        "Edit": ("file_path",),
        "Write": ("file_path",),
        "Read": ("file_path",),
        "NotebookEdit": ("notebook_path", "file_path"),
        "Grep": ("pattern", "path"),
        "Glob": ("pattern",),
    }
    allowed = allowed_by_tool.get(tool_name, ())
    evidence: dict[str, str] = {}
    for key in allowed:
        value = tool_input.get(key)
        if value:
            evidence[key] = redact_text(str(value)[:1000], limit=0)
    return evidence


def _tool_response_evidence(tool_name: str, tool_response: Any) -> dict[str, Any]:
    if not isinstance(tool_response, dict):
        return {}
    evidence: dict[str, Any] = {}
    if "is_error" in tool_response:
        evidence["is_error"] = bool(tool_response.get("is_error"))
    for key in ("exit_code", "returncode", "return_code", "rc"):
        value = tool_response.get(key)
        if value is None:
            continue
        evidence["returncode"] = _safe_int(value)
        break
    if tool_name != "Bash":
        return evidence
    stdout = _coerce_tool_response_text(tool_response.get("stdout") or tool_response.get("output"))
    stderr = _coerce_tool_response_text(tool_response.get("stderr"))
    if stdout:
        evidence["stdout_chars"] = len(stdout)
        evidence["stdout_sha256"] = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        markers = _safe_json_markers_from_text(stdout)
        if markers:
            evidence["json_markers"] = markers[:5]
    if stderr:
        evidence["stderr_chars"] = len(stderr)
        evidence["stderr_sha256"] = hashlib.sha256(stderr.encode("utf-8")).hexdigest()
    return evidence


def _safe_int(value: Any) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)[:40]


def _coerce_tool_response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return ""


def _safe_json_markers_from_text(text: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    safe_keys = {
        "ok",
        "message_id",
        "bytes",
        "returncode",
        "rc",
        "status",
        "width",
        "height",
        "duration",
    }
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        marker = {key: payload[key] for key in safe_keys if key in payload}
        if marker:
            markers.append(marker)
    return markers
