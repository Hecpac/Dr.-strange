from __future__ import annotations

from enum import Enum


class ActionVerdict(Enum):
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"


CDP_READ_ACTIONS = frozenset({"screenshot", "goto", "wait_for"})
DESKTOP_READ_ACTIONS = frozenset({"screenshot", "mouse_move", "scroll", "zoom", "wait"})
DESKTOP_NAV_KEYS = frozenset({
    "Escape", "Tab", "Up", "Down", "Left", "Right",
    "Home", "End", "Page_Up", "Page_Down",
})
CDP_ALWAYS_APPROVE = frozenset({"submit"})


class ActionGate:
    def __init__(self, sensitive_urls: list[str] | None = None) -> None:
        self.sensitive_urls = list(sensitive_urls or [])

    def classify_cdp_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        action_type = action.get("type", "")
        if action_type in CDP_READ_ACTIONS:
            return ActionVerdict.SAFE
        if action_type in CDP_ALWAYS_APPROVE:
            return ActionVerdict.NEEDS_APPROVAL
        # All other CDP write actions need approval (per spec: all writes in CDP require approval)
        return ActionVerdict.NEEDS_APPROVAL

    def classify_desktop_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        action_type = action.get("action", "")
        if action_type in DESKTOP_READ_ACTIONS:
            return ActionVerdict.SAFE
        if action_type == "key":
            key_text = action.get("text", "")
            base_key = key_text.split("+")[-1] if "+" in key_text else key_text
            if base_key in DESKTOP_NAV_KEYS and not self.is_sensitive_url(url):
                return ActionVerdict.SAFE
            if "+" in key_text:
                return ActionVerdict.NEEDS_APPROVAL
            if base_key in DESKTOP_NAV_KEYS:
                return ActionVerdict.SAFE
            return ActionVerdict.NEEDS_APPROVAL
        # State-changing actions without trustworthy URL → needs approval
        if url is None:
            return ActionVerdict.NEEDS_APPROVAL
        if self.is_sensitive_url(url):
            return ActionVerdict.NEEDS_APPROVAL
        return ActionVerdict.SAFE

    def is_sensitive_url(self, url: str | None) -> bool:
        if url is None:
            return False
        for pattern in self.sensitive_urls:
            if pattern in url:
                return True
        return False
