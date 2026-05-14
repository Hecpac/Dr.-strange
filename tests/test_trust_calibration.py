"""Wave 3.2: TrustCalibrator tier adjustments based on track record."""
from __future__ import annotations

import unittest

from claw_v2.trust_calibration import TrustCalibrator


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))


class _FakeEvalResult:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.failures: list[str] = []


class _FakeEvaluator:
    def __init__(self, passed: bool = True) -> None:
        self._passed = passed
        self.calls: list[dict] = []

    def run_self_improvement_gate(self, *, plan: str, diff: str, test_output: str) -> _FakeEvalResult:
        self.calls.append({"plan": plan, "diff": diff, "test_output": test_output})
        return _FakeEvalResult(self._passed)


class TrustCalibratorTests(unittest.TestCase):
    def _record_n(self, calibrator: TrustCalibrator, agent: str, kind: str, *, n: int, success: bool) -> None:
        for _ in range(n):
            calibrator.record_outcome(agent, kind, success=success)

    def test_no_change_when_sample_count_below_min(self) -> None:
        calibrator = TrustCalibrator(min_samples=20)
        self._record_n(calibrator, "rook", "build", n=5, success=True)
        self.assertEqual(calibrator.suggest_tier("rook", "build", current_tier=2), 2)

    def test_bump_when_success_rate_above_threshold_with_enough_samples(self) -> None:
        calibrator = TrustCalibrator(min_samples=20, bump_threshold=0.95)
        self._record_n(calibrator, "rook", "build", n=20, success=True)
        self.assertAlmostEqual(calibrator.success_rate("rook", "build"), 1.0)
        self.assertEqual(calibrator.suggest_tier("rook", "build", current_tier=2), 3)

    def test_downgrade_when_success_rate_below_threshold(self) -> None:
        calibrator = TrustCalibrator(min_samples=20, downgrade_threshold=0.7)
        # 10 success / 20 = 0.5 → downgrade
        self._record_n(calibrator, "alma", "deploy", n=10, success=True)
        self._record_n(calibrator, "alma", "deploy", n=10, success=False)
        self.assertEqual(calibrator.suggest_tier("alma", "deploy", current_tier=2), 1)

    def test_no_change_in_dead_zone_between_thresholds(self) -> None:
        # 16 success / 20 = 0.8 → between bump (0.95) and downgrade (0.7)
        calibrator = TrustCalibrator(min_samples=20)
        self._record_n(calibrator, "hex", "test", n=16, success=True)
        self._record_n(calibrator, "hex", "test", n=4, success=False)
        self.assertEqual(calibrator.suggest_tier("hex", "test", current_tier=2), 2)

    def test_clamps_to_ceiling_tier_3(self) -> None:
        calibrator = TrustCalibrator(min_samples=20, ceiling_tier=3)
        self._record_n(calibrator, "rook", "build", n=20, success=True)
        # already at ceiling — must stay
        self.assertEqual(calibrator.suggest_tier("rook", "build", current_tier=3), 3)

    def test_clamps_to_floor_tier_1(self) -> None:
        calibrator = TrustCalibrator(min_samples=20, floor_tier=1)
        self._record_n(calibrator, "rook", "build", n=20, success=False)
        # already at floor — must stay
        self.assertEqual(calibrator.suggest_tier("rook", "build", current_tier=1), 1)

    def test_calibrate_emits_event_when_tier_changes(self) -> None:
        observe = _RecordingObserve()
        calibrator = TrustCalibrator(min_samples=20, observe=observe)
        self._record_n(calibrator, "rook", "build", n=20, success=True)
        result = calibrator.calibrate("rook", "build", current_tier=2)
        self.assertTrue(result["changed"])
        self.assertEqual(result["from_tier"], 2)
        self.assertEqual(result["to_tier"], 3)
        events = [name for name, _ in observe.events]
        self.assertIn("trust_calibrated", events)

    def test_calibrate_no_event_when_no_change(self) -> None:
        observe = _RecordingObserve()
        calibrator = TrustCalibrator(min_samples=20, observe=observe)
        self._record_n(calibrator, "rook", "build", n=10, success=True)  # under min_samples
        result = calibrator.calibrate("rook", "build", current_tier=2)
        self.assertFalse(result["changed"])
        self.assertEqual([n for n, _ in observe.events if n == "trust_calibrated"], [])

    def test_calibrate_blocked_by_gate_when_evaluator_denies(self) -> None:
        observe = _RecordingObserve()
        calibrator = TrustCalibrator(min_samples=20, observe=observe)
        evaluator = _FakeEvaluator(passed=False)
        self._record_n(calibrator, "rook", "build", n=20, success=True)
        result = calibrator.calibrate("rook", "build", current_tier=2, evaluator=evaluator)
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "gate_denied")
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual([n for n, _ in observe.events if n == "trust_calibrated"], [])

    def test_calibrate_proceeds_when_gate_passes(self) -> None:
        observe = _RecordingObserve()
        calibrator = TrustCalibrator(min_samples=20, observe=observe)
        evaluator = _FakeEvaluator(passed=True)
        self._record_n(calibrator, "rook", "build", n=20, success=True)
        result = calibrator.calibrate("rook", "build", current_tier=2, evaluator=evaluator)
        self.assertTrue(result["changed"])
        self.assertEqual(result["to_tier"], 3)

    def test_window_rolls_off_old_samples_above_window_size(self) -> None:
        calibrator = TrustCalibrator(min_samples=10, window_size=10)
        # 10 fails first, then 10 successes — only the last 10 should count
        self._record_n(calibrator, "rook", "build", n=10, success=False)
        self._record_n(calibrator, "rook", "build", n=10, success=True)
        self.assertEqual(calibrator.sample_count("rook", "build"), 10)
        self.assertAlmostEqual(calibrator.success_rate("rook", "build"), 1.0)


if __name__ == "__main__":
    unittest.main()
