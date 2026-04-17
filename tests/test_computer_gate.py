from __future__ import annotations

import unittest

from claw_v2.computer_gate import ActionGate, ActionVerdict, RiskLevel, verdict_for_risk


class ActionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ActionGate(sensitive_urls=["ads.google.com", "polymarket.com"])

    # --- Tier 1: Browser CDP ---

    def test_cdp_screenshot_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "screenshot"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_cdp_goto_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "goto", "url": "https://ads.google.com"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_cdp_click_on_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "click", "selector": "button"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_fill_on_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "fill", "selector": "input", "value": "100"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_submit_always_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "submit", "selector": "form"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_click_on_non_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "click", "selector": "button"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_wait_for_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "wait_for", "ms": 1000}, url="https://ads.google.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    # --- Tier 2: Computer Use ---

    def test_desktop_screenshot_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "screenshot"}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_mouse_move_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "mouse_move", "coordinate": [100, 200]}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_scroll_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "scroll", "coordinate": [500, 400], "scroll_direction": "down", "scroll_amount": 3}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_click_without_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_type_without_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "type", "text": "hello"}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_click_with_non_sensitive_url_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url="https://docs.google.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_click_with_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_key_navigation_is_safe(self) -> None:
        for key in ["Escape", "Tab", "Up", "Down", "Left", "Right"]:
            verdict = self.gate.classify_desktop_action({"action": "key", "text": key}, url=None)
            self.assertEqual(verdict, ActionVerdict.SAFE, f"key '{key}' should be safe")

    def test_desktop_key_destructive_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "key", "text": "super+Delete"}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    # --- URL matching ---

    def test_sensitive_url_match_is_substring(self) -> None:
        self.assertTrue(self.gate.is_sensitive_url("https://ads.google.com/campaigns?id=123"))
        self.assertTrue(self.gate.is_sensitive_url("https://polymarket.com/market/1"))

    def test_non_sensitive_url(self) -> None:
        self.assertFalse(self.gate.is_sensitive_url("https://google.com"))
        self.assertFalse(self.gate.is_sensitive_url("https://example.com"))

    def test_none_url_is_not_sensitive(self) -> None:
        self.assertFalse(self.gate.is_sensitive_url(None))


class RiskLevelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ActionGate(sensitive_urls=["ads.google.com", "polymarket.com"])

    # --- verdict_for_risk ---

    def test_low_risk_is_safe(self) -> None:
        self.assertEqual(verdict_for_risk(RiskLevel.LOW), ActionVerdict.SAFE)

    def test_medium_risk_is_safe(self) -> None:
        self.assertEqual(verdict_for_risk(RiskLevel.MEDIUM), ActionVerdict.SAFE)

    def test_high_risk_needs_approval(self) -> None:
        self.assertEqual(verdict_for_risk(RiskLevel.HIGH), ActionVerdict.NEEDS_APPROVAL)

    # --- CDP risk ---

    def test_cdp_screenshot_is_low(self) -> None:
        risk = self.gate.risk_cdp({"type": "screenshot"}, url="https://ads.google.com")
        self.assertEqual(risk, RiskLevel.LOW)

    def test_cdp_goto_is_low(self) -> None:
        risk = self.gate.risk_cdp({"type": "goto"}, url=None)
        self.assertEqual(risk, RiskLevel.LOW)

    def test_cdp_submit_is_high(self) -> None:
        risk = self.gate.risk_cdp({"type": "submit"}, url="https://example.com")
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_cdp_click_sensitive_is_high(self) -> None:
        risk = self.gate.risk_cdp({"type": "click"}, url="https://ads.google.com/x")
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_cdp_click_non_sensitive_is_medium(self) -> None:
        risk = self.gate.risk_cdp({"type": "click"}, url="https://example.com")
        self.assertEqual(risk, RiskLevel.MEDIUM)

    def test_cdp_fill_sensitive_is_high(self) -> None:
        risk = self.gate.risk_cdp({"type": "fill"}, url="https://polymarket.com/trade")
        self.assertEqual(risk, RiskLevel.HIGH)

    # --- Desktop risk ---

    def test_desktop_screenshot_is_low(self) -> None:
        risk = self.gate.risk_desktop({"action": "screenshot"}, url=None)
        self.assertEqual(risk, RiskLevel.LOW)

    def test_desktop_scroll_is_low(self) -> None:
        risk = self.gate.risk_desktop({"action": "scroll"}, url=None)
        self.assertEqual(risk, RiskLevel.LOW)

    def test_desktop_click_no_url_is_medium(self) -> None:
        risk = self.gate.risk_desktop({"action": "left_click"}, url=None)
        self.assertEqual(risk, RiskLevel.MEDIUM)

    def test_desktop_click_safe_url_is_low(self) -> None:
        risk = self.gate.risk_desktop({"action": "left_click"}, url="https://docs.google.com")
        self.assertEqual(risk, RiskLevel.LOW)

    def test_desktop_click_sensitive_url_is_high(self) -> None:
        risk = self.gate.risk_desktop({"action": "left_click"}, url="https://ads.google.com")
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_desktop_type_no_url_is_high(self) -> None:
        risk = self.gate.risk_desktop({"action": "type", "text": "hello"}, url=None)
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_desktop_type_safe_url_is_medium(self) -> None:
        risk = self.gate.risk_desktop({"action": "type", "text": "hello"}, url="https://docs.google.com")
        self.assertEqual(risk, RiskLevel.MEDIUM)

    def test_desktop_type_sensitive_url_is_high(self) -> None:
        risk = self.gate.risk_desktop({"action": "type", "text": "100"}, url="https://polymarket.com")
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_desktop_nav_key_is_low(self) -> None:
        risk = self.gate.risk_desktop({"action": "key", "text": "Escape"}, url=None)
        self.assertEqual(risk, RiskLevel.LOW)

    def test_desktop_hotkey_is_high(self) -> None:
        risk = self.gate.risk_desktop({"action": "key", "text": "super+Delete"}, url=None)
        self.assertEqual(risk, RiskLevel.HIGH)

    def test_desktop_hotkey_on_sensitive_is_high(self) -> None:
        risk = self.gate.risk_desktop({"action": "key", "text": "ctrl+a"}, url="https://ads.google.com")
        self.assertEqual(risk, RiskLevel.HIGH)


import tempfile
from pathlib import Path

from claw_v2.approval import ApprovalManager


class ApprovalRejectTests(unittest.TestCase):
    def test_reject_sets_status_to_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            self.assertEqual(manager.status(pending.approval_id), "rejected")

    def test_reject_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            payload = manager.read(pending.approval_id)
            self.assertEqual(payload["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
