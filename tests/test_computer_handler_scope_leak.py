from __future__ import annotations

import unittest
from types import SimpleNamespace

from claw_v2.computer_handler import ComputerHandler


class _FakeApprovals:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self, approval_id: str) -> dict:
        return self._payload


class _FakeObserve:
    def emit(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass


class ComputerHandlerScopeLeakTests(unittest.TestCase):
    def test_scope_mismatch_pops_orphan_session(self) -> None:
        # When the pending-approval scope no longer matches (e.g. the action
        # hash changed), resume returns a blocked message — but the session
        # must NOT be left lingering in awaiting_approval forever.
        approvals = _FakeApprovals(
            {
                "metadata": {
                    "kind": "computer_use",
                    "session_id": "s1",
                    "approval_scope": {"action_hash": "deadbeefdeadbeef"},
                }
            }
        )
        handler = ComputerHandler(approvals=approvals, observe=_FakeObserve())
        session = SimpleNamespace(
            pending_action={"approval_id": "a1", "action": "click", "x": 1, "y": 2},
            status="awaiting_approval",
            task="",
            current_url=None,
        )
        handler._sessions["s1"] = session

        result = handler._resume_approved_computer_action("a1")

        self.assertIn("contexto de computer cambió", result)
        self.assertNotIn("s1", handler._sessions)


if __name__ == "__main__":
    unittest.main()
