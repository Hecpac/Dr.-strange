from __future__ import annotations

from enum import Enum


class ActionVerdict(Enum):
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"


class RiskLevel(Enum):
    LOW = "low"          # Auto-approve, no log
    MEDIUM = "medium"    # Auto-approve, log for audit
    HIGH = "high"        # Requires explicit approval


CDP_READ_ACTIONS = frozenset({"screenshot", "goto", "wait_for"})
DESKTOP_READ_ACTIONS = frozenset({"screenshot", "mouse_move", "scroll", "zoom", "wait"})
DESKTOP_NAV_KEYS = frozenset({
    "Escape", "Tab", "Up", "Down", "Left", "Right",
    "Home", "End", "Page_Up", "Page_Down",
})
CDP_ALWAYS_APPROVE = frozenset({"submit"})
CDP_WRITE_ACTIONS = frozenset({"click", "fill", "select", "check", "uncheck"})


def verdict_for_risk(risk: RiskLevel) -> ActionVerdict:
    """Map a risk level to the legacy ActionVerdict."""
    if risk is RiskLevel.HIGH:
        return ActionVerdict.NEEDS_APPROVAL
    return ActionVerdict.SAFE


class ActionGate:
    def __init__(self, sensitive_urls: list[str] | None = None) -> None:
        self.sensitive_urls = list(sensitive_urls or [])

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
        # Preserve original behavior: all CDP writes → NEEDS_APPROVAL
        if risk in (RiskLevel.MEDIUM, RiskLevel.HIGH):
            return ActionVerdict.NEEDS_APPROVAL
        return ActionVerdict.SAFE

    def classify_desktop_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        risk = self.risk_desktop(action, url=url)
        # Desktop is conservative: only LOW is auto-approved.
        if risk is RiskLevel.LOW:
            return ActionVerdict.SAFE
        return ActionVerdict.NEEDS_APPROVAL

    def is_sensitive_url(self, url: str | None) -> bool:
        if url is None:
            return False
        for pattern in self.sensitive_urls:
            if pattern in url:
                return True
        return False
