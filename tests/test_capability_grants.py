"""Capability Grants — storage + API tests.

The store ships before any production caller wires it. These tests pin
the API (grant / revoke / is_granted / list_active / find_grants_for),
the scope-matching semantics (exact + glob), the expiry semantics, and
the observability emits so the next wave can plug into a stable
contract.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.capability_grants import (
    CapabilityGrant,
    CapabilityGrantStore,
    GrantScope,
    VALID_SCOPE_KINDS,
)


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_kw) -> None:
        self.events.append((event_type, dict(payload or {})))


class GrantScopeTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        scope = GrantScope(kind="tool", target="Bash")
        self.assertTrue(scope.matches(kind="tool", target="Bash"))
        self.assertFalse(scope.matches(kind="tool", target="Write"))
        self.assertFalse(scope.matches(kind="domain", target="Bash"))

    def test_glob_match(self) -> None:
        scope = GrantScope(kind="path", target="/Users/hector/Projects/*")
        self.assertTrue(scope.matches(kind="path", target="/Users/hector/Projects/Dr.-strange"))
        self.assertFalse(scope.matches(kind="path", target="/Users/somebody-else/files"))

    def test_wildcard_session(self) -> None:
        scope = GrantScope(kind="session", target="*")
        self.assertTrue(scope.matches(kind="session", target="tg-574707975"))
        self.assertTrue(scope.matches(kind="session", target="mac-main"))


class CapabilityGrantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.observe = _RecordingObserve()
        self.store = CapabilityGrantStore(Path(self._tmp.name) / "claw.db", observe=self.observe)

    def test_valid_scope_kinds_are_immutable_set(self) -> None:
        self.assertEqual(VALID_SCOPE_KINDS, frozenset({"tool", "domain", "path", "session"}))

    def test_grant_persists_and_emits_event(self) -> None:
        scope = GrantScope(kind="tool", target="Bash")
        record = self.store.grant(scope, grantor="user", ttl_seconds=3600)
        self.assertIsInstance(record, CapabilityGrant)
        self.assertEqual(record.scope.kind, "tool")
        self.assertEqual(record.scope.target, "Bash")
        self.assertEqual(record.grantor, "user")
        self.assertIsNotNone(record.expires_at)
        self.assertGreater(record.expires_at or 0, record.granted_at)
        self.assertIsNone(record.revoked_at)
        # Round-trip via get
        loaded = self.store.get(record.grant_id)
        assert loaded is not None
        self.assertEqual(loaded.grant_id, record.grant_id)
        self.assertEqual(loaded.scope.target, "Bash")
        # Emit
        events = [e[0] for e in self.observe.events]
        self.assertIn("capability_grant_issued", events)

    def test_grant_rejects_invalid_scope_kind(self) -> None:
        with self.assertRaises(ValueError):
            self.store.grant(GrantScope(kind="nuke", target="*"), grantor="user")

    def test_grant_rejects_empty_target_or_grantor(self) -> None:
        with self.assertRaises(ValueError):
            self.store.grant(GrantScope(kind="tool", target=""), grantor="user")
        with self.assertRaises(ValueError):
            self.store.grant(GrantScope(kind="tool", target="Bash"), grantor="")

    def test_grant_without_ttl_is_active_indefinitely(self) -> None:
        record = self.store.grant(GrantScope(kind="domain", target="x.com"), grantor="user")
        self.assertIsNone(record.expires_at)
        self.assertTrue(record.is_active())
        self.assertTrue(self.store.is_granted(kind="domain", target="x.com"))

    def test_expired_grant_is_not_active_and_not_listed(self) -> None:
        # TTL of 1ms — guaranteed expired by the time we check.
        record = self.store.grant(
            GrantScope(kind="tool", target="Edit"),
            grantor="user",
            ttl_seconds=0.001,
        )
        time.sleep(0.01)
        self.assertFalse(record.is_active())
        self.assertFalse(self.store.is_granted(kind="tool", target="Edit"))
        active = self.store.list_active()
        self.assertNotIn(record.grant_id, [g.grant_id for g in active])

    def test_revoke_marks_inactive_and_emits_event(self) -> None:
        record = self.store.grant(GrantScope(kind="tool", target="Write"), grantor="user")
        revoked = self.store.revoke(record.grant_id, reason="user asked stop")
        assert revoked is not None
        self.assertIsNotNone(revoked.revoked_at)
        self.assertEqual(revoked.revoke_reason, "user asked stop")
        self.assertFalse(self.store.is_granted(kind="tool", target="Write"))
        # Emit
        events = [e[0] for e in self.observe.events]
        self.assertIn("capability_grant_revoked", events)

    def test_revoke_already_revoked_returns_none(self) -> None:
        record = self.store.grant(GrantScope(kind="tool", target="Bash"), grantor="user")
        self.store.revoke(record.grant_id, reason="first")
        self.assertIsNone(self.store.revoke(record.grant_id, reason="again"))

    def test_revoke_unknown_grant_returns_none(self) -> None:
        self.assertIsNone(self.store.revoke("nonexistent", reason="x"))

    def test_is_granted_uses_glob_match(self) -> None:
        self.store.grant(
            GrantScope(kind="path", target="/tmp/safe/*"),
            grantor="user",
        )
        self.assertTrue(self.store.is_granted(kind="path", target="/tmp/safe/file.txt"))
        self.assertFalse(self.store.is_granted(kind="path", target="/etc/passwd"))

    def test_find_grants_for_returns_all_matches(self) -> None:
        g1 = self.store.grant(GrantScope(kind="tool", target="Bash"), grantor="user")
        g2 = self.store.grant(GrantScope(kind="tool", target="*"), grantor="kairos")
        self.store.grant(GrantScope(kind="tool", target="Write"), grantor="user")
        matches = self.store.find_grants_for(kind="tool", target="Bash")
        ids = sorted(g.grant_id for g in matches)
        self.assertEqual(sorted([g1.grant_id, g2.grant_id]), ids)

    def test_list_active_ignores_revoked_and_expired(self) -> None:
        active_grant = self.store.grant(GrantScope(kind="tool", target="Read"), grantor="user")
        revoked = self.store.grant(GrantScope(kind="tool", target="Write"), grantor="user")
        self.store.revoke(revoked.grant_id, reason="changed mind")
        expired = self.store.grant(
            GrantScope(kind="tool", target="Edit"),
            grantor="user",
            ttl_seconds=0.001,
        )
        time.sleep(0.01)
        active = self.store.list_active()
        ids = [g.grant_id for g in active]
        self.assertIn(active_grant.grant_id, ids)
        self.assertNotIn(revoked.grant_id, ids)
        self.assertNotIn(expired.grant_id, ids)

    def test_metadata_round_trip(self) -> None:
        record = self.store.grant(
            GrantScope(kind="session", target="tg-1"),
            grantor="user",
            metadata={"reason": "diagnóstico", "scope_tags": ["audit", "safe"]},
        )
        loaded = self.store.get(record.grant_id)
        assert loaded is not None
        self.assertEqual(loaded.metadata.get("reason"), "diagnóstico")
        self.assertEqual(loaded.metadata.get("scope_tags"), ["audit", "safe"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
