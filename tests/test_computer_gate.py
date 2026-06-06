from __future__ import annotations

import unittest
from types import SimpleNamespace

from claw_v2.computer_gate import ActionGate, ActionVerdict, RiskLevel, verdict_for_risk
from claw_v2.computer_handler import ComputerHandler


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

    def test_sensitive_url_match_is_case_insensitive(self) -> None:
        gate = ActionGate(sensitive_urls=["robinhood.com"])
        self.assertTrue(gate.is_sensitive_url("https://ROBINHOOD.com/account"))
        self.assertTrue(gate.is_sensitive_url("https://Robinhood.COM"))

    # --- is_sensitive_text (free-text / browser_use task instructions) ---

    def test_is_sensitive_text_matches_brand(self) -> None:
        self.assertTrue(self.gate.is_sensitive_text("compra acciones en polymarket hoy"))
        self.assertTrue(self.gate.is_sensitive_text("abre POLYMARKET.com/market"))

    def test_is_sensitive_text_brand_is_whole_word(self) -> None:
        # brand is "ads.google", so a bare "google" must not false-positive
        self.assertFalse(self.gate.is_sensitive_text("busca recetas en google"))

    def test_is_sensitive_text_none_and_empty(self) -> None:
        self.assertFalse(self.gate.is_sensitive_text(None))
        self.assertFalse(self.gate.is_sensitive_text(""))

    def test_is_sensitive_text_no_substring_false_positive(self) -> None:
        gate = ActionGate(sensitive_urls=["stripe.com"])
        self.assertTrue(gate.is_sensitive_text("paga con stripe ahora"))
        self.assertFalse(gate.is_sensitive_text("un traje a pinstripe"))


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


class ActionGateAutoApproveTests(unittest.TestCase):
    """auto_approve=True auto-approves LOW + MEDIUM actions but still gates HIGH
    (sensitive URLs, destructive hotkeys, CDP submit). Risk classification is
    unchanged — only the verdict threshold moves."""

    def setUp(self) -> None:
        self.gate = ActionGate(sensitive_urls=["ads.google.com", "polymarket.com"], auto_approve=True)
        self.gated = ActionGate(sensitive_urls=["ads.google.com", "polymarket.com"])

    # auto-approved when ON, gated when OFF (the friction this removes)
    def test_cdp_click_non_sensitive_is_auto_approved(self) -> None:
        action, url = {"type": "click", "selector": "b"}, "https://example.com"
        self.assertEqual(self.gate.classify_cdp_action(action, url=url), ActionVerdict.SAFE)
        self.assertEqual(self.gated.classify_cdp_action(action, url=url), ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_type_non_sensitive_is_auto_approved(self) -> None:
        action, url = {"action": "type", "text": "hello"}, "https://docs.google.com"
        self.assertEqual(self.gate.classify_desktop_action(action, url=url), ActionVerdict.SAFE)
        self.assertEqual(self.gated.classify_desktop_action(action, url=url), ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_click_without_url_is_auto_approved(self) -> None:
        # clicking a native app (no URL) — MEDIUM — auto-approved under the flag
        self.assertEqual(
            self.gate.classify_desktop_action({"action": "left_click", "coordinate": [10, 10]}, url=None),
            ActionVerdict.SAFE,
        )

    # STILL gated even with auto_approve ON (the safety net we keep)
    def test_cdp_click_sensitive_still_needs_approval(self) -> None:
        self.assertEqual(
            self.gate.classify_cdp_action({"type": "click"}, url="https://ads.google.com/x"),
            ActionVerdict.NEEDS_APPROVAL,
        )

    def test_cdp_submit_still_needs_approval(self) -> None:
        self.assertEqual(
            self.gate.classify_cdp_action({"type": "submit"}, url="https://example.com"),
            ActionVerdict.NEEDS_APPROVAL,
        )

    def test_desktop_type_sensitive_still_needs_approval(self) -> None:
        self.assertEqual(
            self.gate.classify_desktop_action({"action": "type", "text": "100"}, url="https://polymarket.com/trade"),
            ActionVerdict.NEEDS_APPROVAL,
        )

    def test_desktop_destructive_hotkey_still_needs_approval(self) -> None:
        self.assertEqual(
            self.gate.classify_desktop_action({"action": "key", "text": "super+Delete"}, url=None),
            ActionVerdict.NEEDS_APPROVAL,
        )

    def test_off_by_default(self) -> None:
        default_gate = ActionGate(sensitive_urls=[])
        self.assertEqual(
            default_gate.classify_cdp_action({"type": "click"}, url="https://example.com"),
            ActionVerdict.NEEDS_APPROVAL,
        )


class _StubBrowserUse:
    def __init__(self) -> None:
        self.called = False
        self.instruction = ""

    async def run_task(self, instruction: str, **kwargs) -> str:
        self.called = True
        self.instruction = instruction
        return "browser task done"


class ComputerHandlerBrowserAutoApproveTests(unittest.TestCase):
    """browser_use_task (authenticated Chrome) auto-runs without 'te autorizo'
    when CLAW_COMPUTER_AUTO_APPROVE is on, EXCEPT when the task targets a
    sensitive domain — which still requires approval."""

    def _handler(self, *, auto_approve: bool, stub: _StubBrowserUse) -> ComputerHandler:
        config = SimpleNamespace(computer_auto_approve=auto_approve, sensitive_urls=["robinhood.com", "stripe.com"])
        return ComputerHandler(browser_use=stub, config=config)

    def _session(self, task: str, current_url: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            task=task,
            current_url=current_url,
            status="running",
            pending_action={"action": "browser_use_task", "backend": "browser_use", "task": task},
        )

    def test_non_sensitive_task_runs_without_approval_when_enabled(self) -> None:
        stub = _StubBrowserUse()
        handler = self._handler(auto_approve=True, stub=stub)
        session = self._session("ve a chatgpt.com y genera una imagen del grid")
        result = handler._run_browser_use_session(session)
        self.assertTrue(stub.called)
        self.assertEqual(session.status, "done")
        self.assertEqual(result, "browser task done")

    def test_sensitive_task_still_requires_approval_when_enabled(self) -> None:
        stub = _StubBrowserUse()
        handler = self._handler(auto_approve=True, stub=stub)
        session = self._session("entra a robinhood.com y vende mis acciones")
        handler._run_browser_use_session(session)
        self.assertFalse(stub.called)
        self.assertEqual(session.status, "awaiting_approval")

    def test_sensitive_by_current_url_still_requires_approval(self) -> None:
        stub = _StubBrowserUse()
        handler = self._handler(auto_approve=True, stub=stub)
        session = self._session("haz click en el botón", current_url="https://stripe.com/dashboard")
        handler._run_browser_use_session(session)
        self.assertFalse(stub.called)
        self.assertEqual(session.status, "awaiting_approval")

    def test_disabled_by_default_requires_approval(self) -> None:
        stub = _StubBrowserUse()
        handler = self._handler(auto_approve=False, stub=stub)
        session = self._session("ve a chatgpt.com y genera una imagen")
        handler._run_browser_use_session(session)
        self.assertFalse(stub.called)
        self.assertEqual(session.status, "awaiting_approval")

    def test_brand_name_without_tld_still_requires_approval(self) -> None:
        # "Robinhood" (capitalized, no .com) must still be gated
        stub = _StubBrowserUse()
        handler = self._handler(auto_approve=True, stub=stub)
        session = self._session("vende mis acciones en Robinhood ahora")
        handler._run_browser_use_session(session)
        self.assertFalse(stub.called)
        self.assertEqual(session.status, "awaiting_approval")

    def test_generic_substring_of_multilabel_brand_not_gated(self) -> None:
        # ads.google.com is sensitive, but a task mentioning only "google"
        # must NOT gate (brand match is "ads.google" as a whole word, not "google")
        config = SimpleNamespace(computer_auto_approve=True, sensitive_urls=["ads.google.com"])
        stub = _StubBrowserUse()
        handler = ComputerHandler(browser_use=stub, config=config)
        session = self._session("abre google y busca recetas de arepas")
        handler._run_browser_use_session(session)
        self.assertTrue(stub.called)
        self.assertEqual(session.status, "done")


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
