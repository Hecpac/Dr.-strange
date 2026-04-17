from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from claw_v2.approval import ApprovalManager
from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlanProposal:
    agent_name: str
    experiment_number: int
    plan_summary: str
    risk_level: str
    estimated_files: list[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_id: str | None = None
    approval_token: str | None = None


class PlanGate:
    def __init__(
        self,
        router: LLMRouter,
        approvals: ApprovalManager | None = None,
        trust_threshold: int = 2,
    ) -> None:
        self.router = router
        self.approvals = approvals
        self.trust_threshold = trust_threshold

    def propose(
        self,
        agent_name: str,
        experiment_number: int,
        instruction: str,
        trust_level: int = 1,
    ) -> PlanProposal:
        prompt = (
            f"You are reviewing an experiment plan for agent '{agent_name}' (experiment #{experiment_number}).\n\n"
            f"Instruction:\n{instruction}\n\n"
            "Respond with JSON only:\n"
            '{"plan_summary": "...", "risk_level": "low|medium|high", "estimated_files": ["..."]}'
        )
        response = self.router.ask(
            prompt,
            lane="verifier",
            evidence_pack={"agent": agent_name, "experiment": experiment_number},
        )
        try:
            parsed = json.loads(response.content)
        except (json.JSONDecodeError, TypeError):
            parsed = {
                "plan_summary": response.content[:500] if response.content else "Could not parse plan",
                "risk_level": "medium",
                "estimated_files": [],
            }

        requires_approval = trust_level < self.trust_threshold
        proposal = PlanProposal(
            agent_name=agent_name,
            experiment_number=experiment_number,
            plan_summary=parsed.get("plan_summary", ""),
            risk_level=parsed.get("risk_level", "medium"),
            estimated_files=parsed.get("estimated_files", []),
            requires_approval=requires_approval,
        )

        if requires_approval and self.approvals is not None:
            pending = self.approvals.create(
                action=f"plan:{agent_name}:{experiment_number}",
                summary=f"Plan for {agent_name} experiment #{experiment_number}: {proposal.plan_summary[:200]}",
            )
            proposal.approval_id = pending.approval_id
            proposal.approval_token = pending.token
            logger.info("Plan requires approval: %s (id=%s)", agent_name, pending.approval_id)

        return proposal

    def is_cleared(self, proposal: PlanProposal) -> bool:
        if not proposal.requires_approval:
            return True
        if proposal.approval_id is None or self.approvals is None:
            return False
        try:
            status = self.approvals.status(proposal.approval_id)
            return status == "approved"
        except FileNotFoundError:
            return False
