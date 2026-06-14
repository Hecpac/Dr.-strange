from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class RawElement:
    """One interactive element as the backend sees it (pre-ref)."""
    selector: str
    role: str | None
    label: str
    text: str | None
    href: str | None
    input_type: str | None


@dataclass(slots=True)
class RawPage:
    """Backend's raw view of a page; the service turns this into refs + snapshot."""
    url: str
    title: str
    text: str
    elements: list[RawElement]
    login_or_challenge: bool = False


@dataclass(slots=True)
class BrowserElementRef:
    ref: str
    label: str
    role: str | None
    selector: str | None
    text: str | None
    href: str | None
    input_type: str | None


@dataclass(slots=True)
class BrowserToolResult:
    success: bool
    url: str | None = None
    title: str | None = None
    snapshot: str | None = None
    element_count: int = 0
    screenshot_path: str | None = None
    error: str | None = None
    backend: str = "chrome_cdp"
    metadata: dict[str, object] = field(default_factory=dict)


class BrowserToolBackend(Protocol):
    """Selector-level CDP operations. Refs are owned by the service, not here."""
    name: str

    def navigate(self, url: str) -> RawPage: ...
    def snapshot(self, full: bool = False) -> RawPage: ...
    def act(self, selector: str, action: str, text: str | None = None) -> RawPage: ...
    def screenshot(self, path: str) -> bool: ...
    def console(self, clear: bool = False) -> list[str]: ...
