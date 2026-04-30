from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from claw_v2.action_events import ProposedAction
from claw_v2.critic_protocol import CriticDecision
from claw_v2.evidence_ledger import Claim
from claw_v2.gdi import GDISnapshot
from claw_v2.goal_contract import GoalContract
from claw_v2.redaction import redact_sensitive

EXTERNAL_CRITIC_REQUEST_SCHEMA_VERSION = "external_critic_request.v1"


class ExternalCriticError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalCriticConfig:
    command: tuple[str, ...]
    allowed_spawners: frozenset[str]
    timeout_seconds: float = 5.0

    def validate_spawner(self, requester: str) -> None:
        if requester not in self.allowed_spawners:
            raise PermissionError(f"requester '{requester}' is not allowed to spawn external critic")


def build_external_critic_payload(
    *,
    goal_contract: GoalContract,
    proposed_next_action: ProposedAction | dict[str, Any],
    evidence_ledger_subset: list[Claim],
    risk_level: str,
    gdi_snapshot: GDISnapshot | None = None,
    recall_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = proposed_next_action if isinstance(proposed_next_action, ProposedAction) else ProposedAction.from_dict(proposed_next_action)
    payload = {
        "schema_version": EXTERNAL_CRITIC_REQUEST_SCHEMA_VERSION,
        "goal_contract": goal_contract.to_dict(),
        "evidence_ledger_subset": [claim.to_dict() for claim in evidence_ledger_subset],
        "proposed_next_action": action.to_dict(),
        "risk_level": risk_level,
        "gdi_snapshot": gdi_snapshot.to_dict() if gdi_snapshot is not None else None,
        "recall_results": [_safe_recall_result(item) for item in (recall_results or [])],
    }
    return redact_sensitive(payload, limit=4000)


def run_external_critic(
    config: ExternalCriticConfig,
    *,
    requester: str,
    payload: dict[str, Any],
) -> CriticDecision:
    config.validate_spawner(requester)
    if not config.command:
        raise ExternalCriticError("external critic command is required")
    result = subprocess.run(
        list(config.command),
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCriticError(f"external critic failed: {result.stderr.strip()[:500]}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCriticError("external critic returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ExternalCriticError("external critic returned non-object JSON")
    return CriticDecision.from_dict(parsed)


def _safe_recall_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": result.get("request_id"),
        "goal_id": result.get("goal_id"),
        "hits": result.get("hits", []),
        "quality_gate": result.get("quality_gate", {}),
        "recorded_at": result.get("recorded_at"),
    }

