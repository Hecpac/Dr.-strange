from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

CLAW_MAINTENANCE_MODE = "CLAW_MAINTENANCE_MODE"
CLAW_NO_JOB_CLAIM = "CLAW_NO_JOB_CLAIM"

MAINTENANCE_MODE_REASON = "maintenance_mode_active"
NO_JOB_CLAIM_REASON = "no_job_claim_active"
MAINTENANCE_ASSERTION_MESSAGE = "maintenance gates active: claim OFF, scheduler OFF, drain OFF"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class MaintenanceGateSnapshot:
    maintenance_mode: bool
    no_job_claim: bool

    @property
    def claim_block_reason(self) -> str | None:
        if self.maintenance_mode:
            return MAINTENANCE_MODE_REASON
        if self.no_job_claim:
            return NO_JOB_CLAIM_REASON
        return None

    @property
    def scheduler_block_reason(self) -> str | None:
        return MAINTENANCE_MODE_REASON if self.maintenance_mode else None

    @property
    def drain_apply_block_reason(self) -> str | None:
        return MAINTENANCE_MODE_REASON if self.maintenance_mode else None


def flag_enabled(name: str, env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    try:
        raw = source.get(name, "0")
    except Exception:
        return True
    return str(raw).strip().lower() in _TRUTHY_VALUES


def snapshot(env: Mapping[str, str] | None = None) -> MaintenanceGateSnapshot:
    return MaintenanceGateSnapshot(
        maintenance_mode=flag_enabled(CLAW_MAINTENANCE_MODE, env=env),
        no_job_claim=flag_enabled(CLAW_NO_JOB_CLAIM, env=env),
    )


def maintenance_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    return snapshot(env=env).maintenance_mode


def no_job_claim_enabled(env: Mapping[str, str] | None = None) -> bool:
    return snapshot(env=env).no_job_claim


def job_claim_block_reason(env: Mapping[str, str] | None = None) -> str | None:
    return snapshot(env=env).claim_block_reason


def scheduler_work_block_reason(env: Mapping[str, str] | None = None) -> str | None:
    return snapshot(env=env).scheduler_block_reason


def drain_apply_block_reason(env: Mapping[str, str] | None = None) -> str | None:
    return snapshot(env=env).drain_apply_block_reason


def maintenance_assertion_payload(*, f2_durability_enabled: bool) -> dict[str, object]:
    return {
        "message": MAINTENANCE_ASSERTION_MESSAGE,
        "claim": "off",
        "scheduler": "off",
        "drain": "off",
        "reason": MAINTENANCE_MODE_REASON,
        "f2_durability_enabled": bool(f2_durability_enabled),
    }
