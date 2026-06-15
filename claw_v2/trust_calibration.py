"""Wave 3.2: dynamic autoexec_max_tier per (agent, task_kind).

Today autoexec_max_tier is a static ceiling on Tier 1/2/3 tools. This
module tracks per-(agent, task_kind) success rate over a rolling window
and suggests tier adjustments — bump when a pair earns it, downgrade when
it stops earning it. Calibration is loud (emits ``trust_calibrated``)
and gated through Evaluator.run_self_improvement_gate so a single bad
streak doesn't immediately revoke autonomy.

Persistence is intentionally omitted at this layer: the runtime can
hydrate the calibrator from the audit stream if it wants cross-restart
state. For now the contract is "calibrate within a process".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque


@dataclass(slots=True)
class TrustWindow:
    outcomes: Deque[int] = field(default_factory=deque)
    current_tier: int = 2  # default = TIER_LOCAL_MUTATION

    def record(self, success: bool) -> None:
        self.outcomes.append(1 if success else 0)

    def success_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(self.outcomes) / len(self.outcomes)

    def sample_count(self) -> int:
        return len(self.outcomes)


class TrustCalibrator:
    """Per-(agent, task_kind) success-rate tracker with tier suggestions.

    Defaults align with the Wave 3.2 plan:
    - bump: success_rate >= 0.95 AND sample_count >= 20
    - downgrade: success_rate < 0.7 AND sample_count >= 20
    - tier clamped to [1, 3] (Tier 1 = read-only, Tier 3 = approval-required)

    Calibration is gated through ``evaluator.run_self_improvement_gate``
    when an evaluator is provided; otherwise the suggestion applies
    directly. Event ``trust_calibrated`` is emitted on apply.
    """

    def __init__(
        self,
        *,
        observe: Any | None = None,
        bump_threshold: float = 0.95,
        downgrade_threshold: float = 0.7,
        min_samples: int = 20,
        window_size: int = 20,
        floor_tier: int = 1,
        ceiling_tier: int = 3,
    ) -> None:
        self.observe = observe
        self.bump_threshold = bump_threshold
        self.downgrade_threshold = downgrade_threshold
        self.min_samples = min_samples
        self.window_size = window_size
        self.floor_tier = floor_tier
        self.ceiling_tier = ceiling_tier
        self._windows: dict[tuple[str, str], TrustWindow] = {}

    def _window(self, agent: str, task_kind: str) -> TrustWindow:
        key = (agent, task_kind)
        if key not in self._windows:
            self._windows[key] = TrustWindow(outcomes=deque(maxlen=self.window_size))
        return self._windows[key]

    def record_outcome(self, agent: str, task_kind: str, *, success: bool) -> None:
        self._window(agent, task_kind).record(success)

    def success_rate(self, agent: str, task_kind: str) -> float:
        return self._window(agent, task_kind).success_rate()

    def sample_count(self, agent: str, task_kind: str) -> int:
        return self._window(agent, task_kind).sample_count()

    def suggest_tier(self, agent: str, task_kind: str, current_tier: int) -> int:
        window = self._window(agent, task_kind)
        if window.sample_count() < self.min_samples:
            return current_tier
        rate = window.success_rate()
        if rate >= self.bump_threshold and current_tier < self.ceiling_tier:
            return current_tier + 1
        if rate < self.downgrade_threshold and current_tier > self.floor_tier:
            return current_tier - 1
        return current_tier

    def calibrate(
        self,
        agent: str,
        task_kind: str,
        current_tier: int,
        *,
        evaluator: Any | None = None,
    ) -> dict[str, Any]:
        """Apply the suggested tier (gated by evaluator if provided).

        Returns a dict with ``changed`` (bool), ``from_tier``, ``to_tier``,
        ``reason``, ``rate``, ``samples``. Emits ``trust_calibrated`` only
        when changed AND gate (if any) passed.
        """
        suggested = self.suggest_tier(agent, task_kind, current_tier)
        rate = self.success_rate(agent, task_kind)
        samples = self.sample_count(agent, task_kind)
        base = {
            "from_tier": current_tier,
            "rate": round(rate, 4),
            "samples": samples,
        }
        if suggested == current_tier:
            return {
                **base,
                "changed": False,
                "to_tier": current_tier,
                "reason": "no_change_suggested",
            }
        direction = "bump" if suggested > current_tier else "downgrade"
        if evaluator is not None:
            plan = (
                f"Trust calibration: {direction} {agent}/{task_kind} "
                f"from tier {current_tier} to {suggested} "
                f"(rate={rate:.3f}, n={samples})"
            )
            gate = evaluator.run_self_improvement_gate(
                plan=plan,
                diff=f"- tier {current_tier}\n+ tier {suggested}",
                test_output="",
            )
            if not getattr(gate, "passed", False):
                return {**base, "changed": False, "to_tier": current_tier, "reason": "gate_denied"}
        self._window(agent, task_kind).current_tier = suggested
        if self.observe is not None:
            self.observe.emit(
                "trust_calibrated",
                payload={
                    "agent": agent,
                    "task_kind": task_kind,
                    "from_tier": current_tier,
                    "to_tier": suggested,
                    "direction": direction,
                    "rate": round(rate, 4),
                    "samples": samples,
                },
            )
        return {**base, "changed": True, "to_tier": suggested, "reason": direction}
