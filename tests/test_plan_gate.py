from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.approval import ApprovalManager
from claw_v2.plan_gate import PlanGate, PlanProposal


class ProposeTests(unittest.TestCase):
    def test_high_trust_auto_approves(self) -> None:
        router = MagicMock()
        router.ask.return_value = MagicMock(content='{"plan_summary": "Fix bug", "risk_level": "low", "estimated_files": ["a.py"]}')
        gate = PlanGate(router=router, trust_threshold=2)
        proposal = gate.propose("seo", 1, "Fix the SEO agent", trust_level=3)
        self.assertFalse(proposal.requires_approval)
        self.assertIsNone(proposal.approval_id)

    def test_low_trust_requires_approval(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            approvals = ApprovalManager(Path(tmpdir), "secret")
            router = MagicMock()
            router.ask.return_value = MagicMock(content='{"plan_summary": "Refactor", "risk_level": "high", "estimated_files": []}')
            gate = PlanGate(router=router, approvals=approvals, trust_threshold=2)
            proposal = gate.propose("new_agent", 1, "Refactor everything", trust_level=1)
            self.assertTrue(proposal.requires_approval)
            self.assertIsNotNone(proposal.approval_id)


class IsClearedTests(unittest.TestCase):
    def test_auto_approved_is_cleared(self) -> None:
        gate = PlanGate(router=MagicMock())
        proposal = PlanProposal(agent_name="x", experiment_number=1, plan_summary="", risk_level="low", requires_approval=False)
        self.assertTrue(gate.is_cleared(proposal))

    def test_pending_is_not_cleared(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            approvals = ApprovalManager(Path(tmpdir), "secret")
            gate = PlanGate(router=MagicMock(), approvals=approvals)
            pending = approvals.create(action="test", summary="test")
            proposal = PlanProposal(agent_name="x", experiment_number=1, plan_summary="", risk_level="low", requires_approval=True, approval_id=pending.approval_id)
            self.assertFalse(gate.is_cleared(proposal))

    def test_approved_is_cleared(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            approvals = ApprovalManager(Path(tmpdir), "secret")
            gate = PlanGate(router=MagicMock(), approvals=approvals)
            pending = approvals.create(action="test", summary="test")
            approvals.approve(pending.approval_id, pending.token)
            proposal = PlanProposal(agent_name="x", experiment_number=1, plan_summary="", risk_level="low", requires_approval=True, approval_id=pending.approval_id)
            self.assertTrue(gate.is_cleared(proposal))


if __name__ == "__main__":
    unittest.main()
