from __future__ import annotations

import ipaddress
import socket
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable
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
    def __init__(self, resolver: Callable[[str], Iterable[str]] | None = None) -> None:
        self._timestamps: dict[str, list[float]] = defaultdict(list)
        self._resolver = resolver or _resolve_host_ips

    def enforce_url(self, url: str, *, policy: NetworkPolicy, actor: str = "default") -> SandboxDecision:
        if len(url) > policy.max_url_length:
            return SandboxDecision(False, "URL too long")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return SandboxDecision(False, f"Blocked scheme: {parsed.scheme or 'empty'}")
        host = (parsed.hostname or "").lower()
        if not host:
            return SandboxDecision(False, "Missing domain")
        if any(self._matches(host, blocked) for blocked in policy.blocked_domains):
            return SandboxDecision(False, "Blocked domain")
        if not any(self._matches(host, allowed) for allowed in policy.allowed_domains):
            return SandboxDecision(False, "Domain not in allowlist")
        ip_decision = self._enforce_resolved_ips(host)
        if not ip_decision.allowed:
            return ip_decision
        now = time.monotonic()
        window = getattr(policy, "rate_window", 60.0)
        timestamps = self._timestamps[actor]
        # Prune old entries outside window
        self._timestamps[actor] = [t for t in timestamps if now - t < window]
        if len(self._timestamps[actor]) >= policy.rate_limit:
            return SandboxDecision(False, "Rate limit exceeded")
        self._timestamps[actor].append(now)
        return SandboxDecision(True, metadata=ip_decision.metadata)

    def enforce_redirect_chain(
        self,
        urls: Iterable[str],
        *,
        policy: NetworkPolicy,
        actor: str = "default",
    ) -> SandboxDecision:
        for url in urls:
            decision = self.enforce_url(url, policy=policy, actor=actor)
            if not decision.allowed:
                return SandboxDecision(False, f"Redirect target blocked: {decision.reason}", decision.metadata)
        return SandboxDecision(True)

    def _enforce_resolved_ips(self, host: str) -> SandboxDecision:
        try:
            ips = sorted(set(str(ipaddress.ip_address(ip)) for ip in self._resolver(host)))
        except (OSError, ValueError):
            return SandboxDecision(False, "Host DNS resolution failed")
        if not ips:
            return SandboxDecision(False, "Host DNS resolution returned no addresses")
        blocked = [ip for ip in ips if not ipaddress.ip_address(ip).is_global]
        if blocked:
            return SandboxDecision(False, "Host resolves to a non-public IP address", {"resolved_ips": ips})
        return SandboxDecision(True, metadata={"resolved_ips": ips})

    @staticmethod
    def _matches(host: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            return host == pattern[2:] or host.endswith(pattern[1:])
        return host == pattern


def _resolve_host_ips(host: str) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [str(literal)]
    results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [item[4][0] for item in results]
