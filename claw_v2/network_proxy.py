from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from claw_v2.types import SandboxDecision


@dataclass(slots=True)
class NetworkPolicy:
    allowed_domains: list[str]
    blocked_domains: list[str] = field(default_factory=list)
    max_url_length: int = 2048
    rate_limit: int = 30
    rate_window: float = 60.0


class DomainAllowlistEnforcer:
    def __init__(self) -> None:
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    def enforce_url(self, url: str, *, policy: NetworkPolicy, actor: str = "default") -> SandboxDecision:
        if len(url) > policy.max_url_length:
            return SandboxDecision(False, "URL too long")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return SandboxDecision(False, f"Blocked scheme: {parsed.scheme or 'empty'}")
        host = parsed.netloc.lower()
        if not host:
            return SandboxDecision(False, "Missing domain")
        if any(self._matches(host, blocked) for blocked in policy.blocked_domains):
            return SandboxDecision(False, "Blocked domain")
        if not any(self._matches(host, allowed) for allowed in policy.allowed_domains):
            return SandboxDecision(False, "Domain not in allowlist")
        now = time.monotonic()
        window = getattr(policy, "rate_window", 60.0)
        timestamps = self._timestamps[actor]
        # Prune old entries outside window
        self._timestamps[actor] = [t for t in timestamps if now - t < window]
        if len(self._timestamps[actor]) >= policy.rate_limit:
            return SandboxDecision(False, "Rate limit exceeded")
        self._timestamps[actor].append(now)
        return SandboxDecision(True)

    @staticmethod
    def _matches(host: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            return host == pattern[2:] or host.endswith(pattern[1:])
        return host == pattern
