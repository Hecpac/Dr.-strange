from __future__ import annotations

import re
from enum import Enum

from claw_v2.automation_policy import (
    BROWSER_ACTION_DEFINITIONS,
    HIGH_RISK_BROWSER_ACTIONS as BROWSER_USE_HIGH_RISK_ACTIONS,
)


class ActionVerdict(Enum):
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"


class RiskLevel(Enum):
    LOW = "low"  # Auto-approve, no log
    MEDIUM = "medium"  # Auto-approve, log for audit
    HIGH = "high"  # Requires explicit approval


CDP_READ_ACTIONS = frozenset({"screenshot", "goto", "wait_for"})
DESKTOP_READ_ACTIONS = frozenset({"screenshot", "mouse_move", "scroll", "zoom", "wait"})
DESKTOP_NAV_KEYS = frozenset(
    {
        "Escape",
        "Tab",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "Page_Up",
        "Page_Down",
    }
)
CDP_ALWAYS_APPROVE = frozenset({"submit"})
CDP_WRITE_ACTIONS = frozenset({"click", "fill", "select", "check", "uncheck"})
BROWSER_USE_READ_ACTIONS = frozenset(
    name for name, definition in BROWSER_ACTION_DEFINITIONS.items() if definition.risk == "low"
)
BROWSER_USE_NAV_ACTIONS = frozenset({"navigate", "search", "go_back", "switch", "close"})
BROWSER_USE_WRITE_ACTIONS = frozenset({"click", "input", "send_keys", "select_dropdown"})


def verdict_for_risk(risk: RiskLevel) -> ActionVerdict:
    """Map a risk level to the legacy ActionVerdict."""
    if risk is RiskLevel.HIGH:
        return ActionVerdict.NEEDS_APPROVAL
    return ActionVerdict.SAFE


class ActionGate:
    def __init__(
        self, sensitive_urls: list[str] | None = None, *, auto_approve: bool = False
    ) -> None:
        self.sensitive_urls = list(sensitive_urls or [])
        # When True, LOW + MEDIUM actions auto-execute without approval; only
        # HIGH risk (sensitive URLs, destructive hotkeys, CDP submit) still
        # requires an explicit "te autorizo". Risk classification below is
        # unchanged — only the verdict threshold moves.
        self.auto_approve = bool(auto_approve)
        # Precompute once (sensitive_urls is fixed at construction): lowercased
        # hosts for URL substring matching, and a whole-word brand regex per
        # host (host minus the final TLD label) for free-text matching.
        self._sensitive_hosts_lower = [host.lower() for host in self.sensitive_urls]
        self._sensitive_brand_res = [
            re.compile(rf"\b{re.escape(brand)}\b")
            for brand in (host.rsplit(".", 1)[0] for host in self._sensitive_hosts_lower)
            if brand
        ]

    # -- Risk-level API (new, granular) ------------------------------------

    def risk_cdp(self, action: dict, *, url: str | None) -> RiskLevel:
        """Classify a CDP browser action into LOW / MEDIUM / HIGH risk."""
        action_type = action.get("type", "")
        if action_type in CDP_READ_ACTIONS:
            return RiskLevel.LOW
        if action_type in CDP_ALWAYS_APPROVE:
            return RiskLevel.HIGH
        # Write actions: risk depends on URL sensitivity
        if self.is_sensitive_url(url):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    def risk_desktop(self, action: dict, *, url: str | None) -> RiskLevel:
        """Classify a desktop Computer Use action into LOW / MEDIUM / HIGH risk."""
        action_type = action.get("action", "")
        if action_type in DESKTOP_READ_ACTIONS:
            return RiskLevel.LOW
        if action_type == "key":
            return self._risk_key(action, url=url)
        if action_type == "type":
            if self.is_sensitive_url(url):
                return RiskLevel.HIGH
            return RiskLevel.HIGH if url is None else RiskLevel.MEDIUM
        # Click-like actions
        if url is None:
            return RiskLevel.MEDIUM
        if self.is_sensitive_url(url):
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def risk_browser_use_action(
        self, action_name: str, params: dict | None = None, *, url: str | None
    ) -> RiskLevel:
        """Classify a browser-use action into LOW / MEDIUM / HIGH risk.

        browser_use exposes higher-level browser actions than the repo's CDP
        bridge. This policy keeps read/navigation actions low unless they target
        a sensitive domain, and gates writes/evaluation/uploads more tightly.
        """
        params = params or {}
        action_name = str(action_name or "")
        target_url = _string_value(params.get("url"))
        if target_url and self.is_sensitive_url(target_url):
            return RiskLevel.HIGH
        if action_name == "search" and self.is_sensitive_text(_string_value(params.get("query"))):
            return RiskLevel.HIGH
        if action_name in BROWSER_USE_READ_ACTIONS:
            return RiskLevel.LOW
        if action_name in BROWSER_USE_HIGH_RISK_ACTIONS:
            return RiskLevel.HIGH
        if action_name in BROWSER_USE_NAV_ACTIONS:
            return RiskLevel.HIGH if self.is_sensitive_url(url) else RiskLevel.LOW
        if action_name in BROWSER_USE_WRITE_ACTIONS:
            if self.is_sensitive_url(url):
                return RiskLevel.HIGH
            if action_name in {"input", "send_keys"} and url is None:
                return RiskLevel.HIGH
            return RiskLevel.MEDIUM
        if self.is_sensitive_url(url):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    def _risk_key(self, action: dict, *, url: str | None) -> RiskLevel:
        """Risk classification for keyboard actions."""
        key_text = action.get("text", "")
        base_key = key_text.split("+")[-1] if "+" in key_text else key_text
        # Hotkey combos are at least MEDIUM
        if "+" in key_text:
            if self.is_sensitive_url(url):
                return RiskLevel.HIGH
            return RiskLevel.HIGH  # Hotkeys can be destructive
        # Plain nav keys
        if base_key in DESKTOP_NAV_KEYS:
            return RiskLevel.LOW
        # Other single keys (Enter, etc.)
        if self.is_sensitive_url(url):
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    # -- Legacy verdict API (preserved for backward compatibility) ----------

    def classify_cdp_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        risk = self.risk_cdp(action, url=url)
        if self.auto_approve:
            # Only HIGH (sensitive URL / submit) still needs approval.
            return verdict_for_risk(risk)
        # Preserve original behavior: all CDP writes → NEEDS_APPROVAL
        if risk in (RiskLevel.MEDIUM, RiskLevel.HIGH):
            return ActionVerdict.NEEDS_APPROVAL
        return ActionVerdict.SAFE

    def classify_desktop_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        risk = self.risk_desktop(action, url=url)
        if self.auto_approve:
            # Only HIGH (sensitive URL / destructive hotkey / typing without a
            # known context) still needs approval.
            return verdict_for_risk(risk)
        # Desktop is conservative: only LOW is auto-approved.
        if risk is RiskLevel.LOW:
            return ActionVerdict.SAFE
        return ActionVerdict.NEEDS_APPROVAL

    def is_sensitive_url(self, url: str | None) -> bool:
        # Case-insensitive: a mixed-case host (https://ROBINHOOD.com) must not
        # bypass the gate.
        if url is None:
            return False
        url_lower = url.lower()
        return any(host in url_lower for host in self._sensitive_hosts_lower)

    def is_sensitive_text(self, text: str | None) -> bool:
        """True if free text names a sensitive domain by brand (host minus the
        final TLD label), matched as a whole word so "pinstripe" / "google" do
        not false-positive. Used to gate browser_use tasks given as instructions."""
        if not text:
            return False
        lowered = text.lower()
        return any(pattern.search(lowered) for pattern in self._sensitive_brand_res)


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
