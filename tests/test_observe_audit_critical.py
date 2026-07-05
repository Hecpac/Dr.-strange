from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.chat_api import LocalChatAPI
from claw_v2.observe import ObserveStream, is_audit_critical_event
from claw_v2.sqlite_runtime import RuntimeDb


WEB_CHAT_TEST_CREDENTIAL = "test-web-chat-credential"


@contextlib.contextmanager
def _try_acquire_result(acquired: bool):
    yield acquired


def _spill_records(observe: ObserveStream) -> list[dict]:
    spill_path = observe.db_path.with_suffix(".spill.jsonl")
    return [
        json.loads(line)
        for line in spill_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class ObserveAuditCriticalTests(unittest.TestCase):
    def test_audit_critical_event_classification_covers_security_categories(self) -> None:
        critical_events = {
            "approval_created",
            "approval_approved",
            "owner_delegation_approval_required",
            "telegram_imperative_pending_approval",
            "implicit_approval_requires_explicit_approval",
            "approval_detected",
            "computer_approval_pending",
            "computer_browser_use_approval_required",
            "sdk_post_tool_use",
            "sdk_post_tool_use_failure",
            "runtime_policy_tool_not_declared",
            "web_chat_auth_rejected",
            "runtime_db_degraded",
            "daemon_branch_integrity_violation",
            "scheduled_job_error",
        }

        self.assertEqual(
            {event for event in critical_events if not is_audit_critical_event(event)},
            set(),
        )
        self.assertFalse(is_audit_critical_event("daemon_background_runner_cycle"))

    def test_audit_critical_event_spills_with_marker_when_runtime_db_lock_contended(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)
                received: list[dict] = []
                observe.subscribe("approval_created", received.append)

                with (
                    patch.object(
                        runtime_db,
                        "try_acquire",
                        side_effect=lambda: _try_acquire_result(False),
                    ),
                    patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0),
                ):
                    observe.emit(
                        "approval_created",
                        payload={"approval_id": "ap-1", "status": "pending"},
                    )

                self.assertEqual(
                    received,
                    [{"approval_id": "ap-1", "status": "pending", "audit_critical": True}],
                )
                records = _spill_records(observe)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["event_type"], "approval_created")
                self.assertIs(records[0]["audit_critical"], True)
                payload = json.loads(records[0]["payload"])
                self.assertEqual(payload["approval_id"], "ap-1")
                self.assertIs(payload["audit_critical"], True)
            finally:
                runtime_db.close()

    def test_owner_delegation_approval_required_persists_with_audit_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)

                observe.emit(
                    "owner_delegation_approval_required",
                    payload={
                        "session_id": "tg-test",
                        "kind": "decide_for_me",
                        "resolution_source": "active_task",
                    },
                )

                events = observe.recent_events(
                    limit=1,
                    event_type="owner_delegation_approval_required",
                )
                self.assertEqual(len(events), 1)
                payload = events[0]["payload"]
                self.assertEqual(payload["session_id"], "tg-test")
                self.assertEqual(payload["kind"], "decide_for_me")
                self.assertIs(payload["audit_critical"], True)
                self.assertFalse(observe.db_path.with_suffix(".spill.jsonl").exists())
            finally:
                runtime_db.close()

    def test_owner_delegation_approval_required_spills_with_audit_marker_when_contended(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)
                received: list[dict] = []
                observe.subscribe("owner_delegation_approval_required", received.append)

                with (
                    patch.object(
                        runtime_db,
                        "try_acquire",
                        side_effect=lambda: _try_acquire_result(False),
                    ),
                    patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0),
                ):
                    observe.emit(
                        "owner_delegation_approval_required",
                        payload={
                            "session_id": "tg-test",
                            "kind": "decide_for_me",
                            "mode": "browse",
                        },
                    )

                self.assertEqual(
                    received,
                    [
                        {
                            "session_id": "tg-test",
                            "kind": "decide_for_me",
                            "mode": "browse",
                            "audit_critical": True,
                        }
                    ],
                )
                records = _spill_records(observe)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["event_type"], "owner_delegation_approval_required")
                self.assertIs(records[0]["audit_critical"], True)
                payload = json.loads(records[0]["payload"])
                self.assertEqual(payload["session_id"], "tg-test")
                self.assertEqual(payload["kind"], "decide_for_me")
                self.assertIs(payload["audit_critical"], True)
            finally:
                runtime_db.close()

    def test_audit_critical_spill_drains_back_to_observe_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)

                with (
                    patch.object(
                        runtime_db,
                        "try_acquire",
                        side_effect=lambda: _try_acquire_result(False),
                    ),
                    patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0),
                ):
                    observe.emit(
                        "runtime_policy_tool_not_declared",
                        payload={"tool_name": "UnregisteredTool", "reason": "not declared"},
                    )

                result = observe.drain_spill()

                self.assertEqual(result.inserted, 1)
                self.assertEqual(result.remaining_lines, 0)
                events = observe.recent_events(
                    limit=1,
                    event_type="runtime_policy_tool_not_declared",
                )
                self.assertEqual(len(events), 1)
                payload = events[0]["payload"]
                self.assertEqual(payload["tool_name"], "UnregisteredTool")
                self.assertIs(payload["audit_critical"], True)
            finally:
                runtime_db.close()

    def test_non_critical_event_contention_behavior_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)
                received: list[dict] = []
                observe.subscribe("daemon_background_runner_cycle", received.append)

                with (
                    patch.object(
                        runtime_db,
                        "try_acquire",
                        side_effect=lambda: _try_acquire_result(False),
                    ),
                    patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0),
                ):
                    observe.emit("daemon_background_runner_cycle", payload={"cycle": 1})

                self.assertEqual(received, [{"cycle": 1}])
                records = _spill_records(observe)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["event_type"], "daemon_background_runner_cycle")
                self.assertFalse(records[0].get("audit_critical", False))
                self.assertEqual(json.loads(records[0]["payload"]), {"cycle": 1})
            finally:
                runtime_db.close()

    def test_web_chat_auth_rejection_is_audit_critical_and_spills_under_contention(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_db = RuntimeDb(Path(tmpdir) / "claw.db")
            try:
                observe = ObserveStream(runtime_db.db_path, runtime_db=runtime_db)
                bot_service = MagicMock()
                bot_service.allowed_user_id = "123"
                api = LocalChatAPI(
                    bot_service=bot_service,
                    observe=observe,
                    auth_token=WEB_CHAT_TEST_CREDENTIAL,
                )

                with (
                    patch.object(
                        runtime_db,
                        "try_acquire",
                        side_effect=lambda: _try_acquire_result(False),
                    ),
                    patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0),
                ):
                    status_code, _, body = api.handle_http(
                        method="POST",
                        path="/api/chat",
                        body=b"{}",
                        headers={},
                    )

                self.assertEqual(status_code, 401)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "unauthorized"})
                bot_service.handle_text.assert_not_called()
                records = _spill_records(observe)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["event_type"], "web_chat_auth_rejected")
                self.assertIs(records[0]["audit_critical"], True)
                payload = json.loads(records[0]["payload"])
                self.assertEqual(payload["path"], "/api/chat")
                self.assertEqual(payload["method"], "POST")
                self.assertEqual(payload["reason"], "unauthorized")
                self.assertIs(payload["audit_critical"], True)
                self.assertNotIn(WEB_CHAT_TEST_CREDENTIAL, json.dumps(payload))
            finally:
                runtime_db.close()


if __name__ == "__main__":
    unittest.main()
