from __future__ import annotations

import json
import sys
import unittest

from claw_v2.action_events import ProposedAction
from claw_v2.critic_protocol import CRITIC_SCHEMA_VERSION
from claw_v2.evidence_ledger import Claim, EvidenceRef
from claw_v2.external_critic import (
    EXTERNAL_CRITIC_REQUEST_SCHEMA_VERSION,
    ExternalCriticConfig,
    build_external_critic_payload,
    run_external_critic,
)
from claw_v2.goal_contract import GoalContract


class ExternalCriticTests(unittest.TestCase):
    def test_payload_is_limited_and_redacted(self) -> None:
        payload = build_external_critic_payload(
            goal_contract=GoalContract(goal_id="g_1", objective="Ship telemetry"),
            proposed_next_action=ProposedAction(
                tool="write_file",
                args_redacted={"telegram_bot_token": "secret-token-123456"},
                tier="tier_2",
            ),
            evidence_ledger_subset=[
                Claim(
                    claim_id="c_1",
                    goal_id="g_1",
                    claim_text="Tests passed",
                    claim_type="fact",
                    evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest -q")],
                    verification_status="verified",
                )
            ],
            risk_level="medium",
            recall_results=[{"request_id": "r_1", "chain_of_thought": "private", "hits": []}],
        )

        raw = json.dumps(payload)
        self.assertEqual(payload["schema_version"], EXTERNAL_CRITIC_REQUEST_SCHEMA_VERSION)
        self.assertNotIn("secret-token-123456", raw)
        self.assertNotIn("chain_of_thought", raw)

    def test_rejects_unallowed_spawner(self) -> None:
        config = ExternalCriticConfig(command=("critic",), allowed_spawners=frozenset({"coordinator"}))
        with self.assertRaises(PermissionError):
            config.validate_spawner("worker")

    def test_runs_external_critic_command(self) -> None:
        script = (
            "import json,sys;"
            "json.load(sys.stdin);"
            "print(json.dumps({"
            "'schema_version':'critic_decision.v1',"
            "'decision_id':'d_1',"
            "'goal_id':'g_1',"
            "'decision':'approve',"
            "'reason_summary':'ok',"
            "'goal_alignment':1.0,"
            "'required_fix':[],"
            "'risk_assessment':{'level':'low','factors':[]},"
            "'evidence_gaps':[],"
            "'decided_at':'2026-04-30T00:00:00Z'"
            "}))"
        )
        config = ExternalCriticConfig(
            command=(sys.executable, "-c", script),
            allowed_spawners=frozenset({"coordinator"}),
        )

        decision = run_external_critic(config, requester="coordinator", payload={"goal_id": "g_1"})

        self.assertEqual(decision.schema_version, CRITIC_SCHEMA_VERSION)
        self.assertEqual(decision.decision, "approve")


if __name__ == "__main__":
    unittest.main()

