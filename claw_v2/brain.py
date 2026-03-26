from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.approval import ApprovalManager
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.types import CriticalActionExecution, CriticalActionVerification, LLMResponse


VERIFIER_PROMPT = """Review the proposed critical action using only the evidence pack.
Return JSON only with this exact shape:
{
  "recommendation": "approve" | "needs_approval" | "deny",
  "risk_level": "low" | "medium" | "high" | "critical",
  "summary": "short summary",
  "reasons": ["reason 1"],
  "blockers": ["blocker 1"],
  "missing_checks": ["missing check 1"],
  "confidence": 0.0
}

Rules:
- Use "approve" only if the action is ready to proceed now.
- Use "needs_approval" if human review is required before proceeding.
- Use "deny" if the action should not proceed in its current state.
- Keep arrays empty when there is nothing to report.
- The response must be valid JSON with no markdown fences."""


@dataclass(slots=True)
class BrainService:
    router: LLMRouter
    memory: MemoryStore
    system_prompt: str
    approvals: ApprovalManager | None = None
    observe: ObserveStream | None = None

    def handle_message(self, session_id: str, message: str) -> LLMResponse:
        context = self.memory.build_context(session_id, message)
        provider_session_id = self.memory.get_provider_session(session_id, "anthropic")
        response = self.router.ask(
            context,
            system_prompt=self.system_prompt,
            lane="brain",
            session_id=provider_session_id,
            evidence_pack={"app_session_id": session_id},
            max_budget=2.0,
            timeout=300.0,
        )
        provider_session_artifact = response.artifacts.get("session_id")
        if isinstance(provider_session_artifact, str) and provider_session_artifact:
            self.memory.link_provider_session(session_id, response.provider, provider_session_artifact)
        self.memory.store_message(session_id, "user", message)
        self.memory.store_message(session_id, "assistant", response.content)
        return response

    def verify_critical_action(
        self,
        *,
        plan: str,
        diff: str,
        test_output: str,
        action: str = "critical_action",
        create_approval: bool = True,
    ) -> CriticalActionVerification:
        evidence = {"plan": plan, "diff": diff, "test_output": test_output}
        response = self.router.ask(VERIFIER_PROMPT, lane="verifier", evidence_pack=evidence)
        parsed = _parse_verifier_payload(response.content)

        requires_human_approval = (
            parsed["recommendation"] != "approve"
            or parsed["risk_level"] in {"high", "critical"}
            or bool(parsed["blockers"])
            or bool(parsed["missing_checks"])
        )
        should_proceed = (
            parsed["recommendation"] == "approve"
            and parsed["risk_level"] in {"low", "medium"}
            and not parsed["blockers"]
            and not parsed["missing_checks"]
        )

        approval_id: str | None = None
        approval_token: str | None = None
        if create_approval and requires_human_approval and self.approvals is not None:
            pending = self.approvals.create(
                action=action,
                summary=f"{parsed['risk_level']}/{parsed['recommendation']}: {parsed['summary']}",
                metadata={
                    "recommendation": parsed["recommendation"],
                    "risk_level": parsed["risk_level"],
                    "reasons": parsed["reasons"],
                    "blockers": parsed["blockers"],
                    "missing_checks": parsed["missing_checks"],
                    "provider": response.provider,
                    "model": response.model,
                },
            )
            approval_id = pending.approval_id
            approval_token = pending.token

        if self.observe is not None:
            self.observe.emit(
                "critical_action_verification",
                lane=response.lane,
                provider=response.provider,
                model=response.model,
                payload={
                    "action": action,
                    "recommendation": parsed["recommendation"],
                    "risk_level": parsed["risk_level"],
                    "requires_human_approval": requires_human_approval,
                    "should_proceed": should_proceed,
                    "approval_id": approval_id,
                    "confidence": parsed["confidence"],
                    "blocker_count": len(parsed["blockers"]),
                    "missing_check_count": len(parsed["missing_checks"]),
                },
            )

        return CriticalActionVerification(
            recommendation=parsed["recommendation"],
            risk_level=parsed["risk_level"],
            summary=parsed["summary"],
            reasons=parsed["reasons"],
            blockers=parsed["blockers"],
            missing_checks=parsed["missing_checks"],
            confidence=parsed["confidence"],
            requires_human_approval=requires_human_approval,
            should_proceed=should_proceed,
            approval_id=approval_id,
            approval_token=approval_token,
            response=response,
        )

    def execute_critical_action(
        self,
        *,
        action: str,
        plan: str,
        diff: str,
        test_output: str,
        executor: Callable[[], Any],
        approval_id: str | None = None,
        pre_check: Callable[[CriticalActionVerification], bool] | None = None,
    ) -> CriticalActionExecution:
        approval_status: str | None = None
        approval_override = False
        if approval_id is not None and self.approvals is not None:
            try:
                approval_status = self.approvals.status(approval_id)
            except FileNotFoundError:
                approval_status = "missing"
            approval_override = approval_status == "approved"

        verification = self.verify_critical_action(
            plan=plan,
            diff=diff,
            test_output=test_output,
            action=action,
            create_approval=not approval_override,
        )

        # Pre-execution pause: caller can inspect verification and abort
        if pre_check is not None and not pre_check(verification):
            self._emit_execution_event(
                action=action,
                verification=verification,
                status="aborted_by_pre_check",
                approval_status=approval_status,
            )
            return CriticalActionExecution(
                action=action,
                status="aborted_by_pre_check",
                executed=False,
                verification=verification,
                reason="Pre-execution check rejected the action.",
                approval_status=approval_status,
            )

        if verification.should_proceed:
            result = executor()
            self._emit_execution_event(
                action=action,
                verification=verification,
                status="executed",
                approval_status=approval_status,
            )
            return CriticalActionExecution(
                action=action,
                status="executed",
                executed=True,
                verification=verification,
                result=result,
                approval_status=approval_status,
            )

        if approval_override:
            result = executor()
            self._emit_execution_event(
                action=action,
                verification=verification,
                status="executed_with_approval",
                approval_status=approval_status,
            )
            return CriticalActionExecution(
                action=action,
                status="executed_with_approval",
                executed=True,
                verification=verification,
                result=result,
                approval_status=approval_status,
                reason="human approval override",
            )

        if verification.requires_human_approval:
            status = "awaiting_approval" if self.approvals is not None else "blocked"
            reason = verification.summary
            self._emit_execution_event(
                action=action,
                verification=verification,
                status=status,
                approval_status=approval_status,
            )
            return CriticalActionExecution(
                action=action,
                status=status,
                executed=False,
                verification=verification,
                reason=reason,
                approval_status=approval_status,
            )

        self._emit_execution_event(
            action=action,
            verification=verification,
            status="blocked",
            approval_status=approval_status,
        )
        return CriticalActionExecution(
            action=action,
            status="blocked",
            executed=False,
            verification=verification,
            reason=verification.summary,
            approval_status=approval_status,
        )

    def _emit_execution_event(
        self,
        *,
        action: str,
        verification: CriticalActionVerification,
        status: str,
        approval_status: str | None,
    ) -> None:
        if self.observe is None or verification.response is None:
            return
        self.observe.emit(
            "critical_action_execution",
            lane=verification.response.lane,
            provider=verification.response.provider,
            model=verification.response.model,
            payload={
                "action": action,
                "status": status,
                "approval_status": approval_status,
                "recommendation": verification.recommendation,
                "risk_level": verification.risk_level,
                "requires_human_approval": verification.requires_human_approval,
                "should_proceed": verification.should_proceed,
                "approval_id": verification.approval_id,
            },
        )


def _parse_verifier_payload(content: str) -> dict:
    parsed = _try_parse_json_object(content)
    if parsed is None:
        lowered = content.lower()
        recommendation = "approve"
        if "deny" in lowered or "do not proceed" in lowered or "should not proceed" in lowered:
            recommendation = "deny"
        elif "approval" in lowered or "review" in lowered or "human" in lowered:
            recommendation = "needs_approval"
        risk_level = "medium"
        for candidate in ("critical", "high", "medium", "low"):
            if candidate in lowered:
                risk_level = candidate
                break
        summary = content.strip().splitlines()[0] if content.strip() else "Verifier returned no content."
        return {
            "recommendation": recommendation,
            "risk_level": risk_level,
            "summary": summary,
            "reasons": [summary],
            "blockers": [],
            "missing_checks": [],
            "confidence": 0.3,
        }

    recommendation = _normalize_recommendation(parsed.get("recommendation"))
    risk_level = _normalize_risk_level(parsed.get("risk_level"))
    reasons = _as_string_list(parsed.get("reasons"))
    blockers = _as_string_list(parsed.get("blockers"))
    missing_checks = _as_string_list(parsed.get("missing_checks"))
    summary = str(parsed.get("summary") or "").strip() or "Verifier returned no summary."
    confidence = _clamp_confidence(parsed.get("confidence"))

    if recommendation == "approve" and (blockers or missing_checks or risk_level in {"high", "critical"}):
        recommendation = "needs_approval"

    return {
        "recommendation": recommendation,
        "risk_level": risk_level,
        "summary": summary,
        "reasons": reasons,
        "blockers": blockers,
        "missing_checks": missing_checks,
        "confidence": confidence,
    }


def _try_parse_json_object(content: str) -> dict | None:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    for candidate in (stripped, _first_json_object(stripped)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _first_json_object(content: str) -> str | None:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return content[start : end + 1]


def _normalize_recommendation(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"approve", "approved", "allow", "proceed"}:
        return "approve"
    if text in {"needs_approval", "needs approval", "review", "manual_review", "manual review"}:
        return "needs_approval"
    if text in {"deny", "denied", "reject", "block"}:
        return "deny"
    return "needs_approval"


def _normalize_risk_level(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high", "critical"}:
        return text
    return "medium"


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _clamp_confidence(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return numeric
