"""Tests for the 2026-06-10 audit, group 2 (stability).

1. Checkpoint restore removes stale -wal/-shm sidecars so SQLite cannot
   replay frames from the old database over the restored snapshot.
2. Approval records are written atomically (tmp + rename) and one corrupt
   record no longer breaks the whole pending inbox.
3. Per-request timeouts are actually enforced by the Anthropic and OpenAI
   adapters instead of only being validated.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
from claw_v2.adapters.base import AdapterError, LLMRequest
from claw_v2.adapters.openai import OpenAIAdapter
from claw_v2.approval import ApprovalManager
from claw_v2.checkpoint import CheckpointService, apply_pending_restore_if_any
from claw_v2.memory import MemoryStore

from tests.helpers import make_config


class CheckpointRestoreWalSafetyTests(unittest.TestCase):
    def test_restore_removes_stale_wal_sidecars_before_copy(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "claw.db"

        store = MemoryStore(db_path)
        store.store_fact("k", "before", source="test")
        service = CheckpointService(memory=store, snapshots_dir=tmp / "snapshots")
        ckpt_id = service.create(trigger_reason="seed")
        store.store_fact("k", "after", source="test")
        service.schedule_restore(ckpt_id)
        store._conn.close()

        # Simulate a crash that left sidecars from the pre-restore database:
        # without the fix these would be recovered over the snapshot.
        stale = b"stale-wal-content" * 4
        Path(f"{db_path}-wal").write_bytes(stale)
        Path(f"{db_path}-shm").write_bytes(stale)

        applied = apply_pending_restore_if_any(db_path)
        self.assertEqual(applied, ckpt_id)

        store2 = MemoryStore(db_path)
        values = [f["value"] for f in store2.search_facts("k")]
        self.assertIn("before", values)
        self.assertNotIn("after", values)
        wal_after = Path(f"{db_path}-wal")
        if wal_after.exists():
            self.assertNotEqual(wal_after.read_bytes(), stale)


class ApprovalAtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.manager = ApprovalManager(self.root, "secret")

    def test_corrupt_record_does_not_break_pending_inbox(self) -> None:
        pending = self.manager.create("deploy", "ship it")
        (self.root / "deadbeefdeadbeef.json").write_text("{truncated", encoding="utf-8")
        # PR #83 review (gemini): valid JSON that is not an object must be
        # skipped too, not raise AttributeError mid-listing.
        (self.root / "cafecafecafecafe.json").write_text("null", encoding="utf-8")
        (self.root / "beefbeefbeefbeef.json").write_text("[]", encoding="utf-8")

        listed = self.manager.list_pending()

        self.assertEqual([p["approval_id"] for p in listed], [pending.approval_id])
        # Resolving the healthy record still works.
        self.assertTrue(self.manager.approve(pending.approval_id, pending.token))

    def test_create_and_update_leave_no_tmp_files_and_keep_mode_0600(self) -> None:
        pending = self.manager.create("deploy", "ship it")
        self.manager.approve(pending.approval_id, pending.token)

        leftovers = [p for p in self.root.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])
        record = self.root / f"{pending.approval_id}.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "approved")
        self.assertNotIn("_result", payload)

    def test_locked_update_single_use_survives_atomic_replace(self) -> None:
        pending = self.manager.create("deploy", "ship it")
        self.assertTrue(self.manager.approve(pending.approval_id, pending.token))
        # Replay must still be rejected after the record was atomically replaced.
        self.assertFalse(self.manager.approve(pending.approval_id, pending.token))
        self.assertEqual(self.manager.status(pending.approval_id), "approved")


def _advisory_request(*, timeout: float, provider: str, model: str) -> LLMRequest:
    return LLMRequest(
        prompt="judge this",
        system_prompt=None,
        lane="judge",
        provider=provider,
        model=model,
        effort=None,
        session_id=None,
        max_budget=0.1,
        evidence_pack={"data": "x"},
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=timeout,
    )


class AnthropicTimeoutEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_sdk_turn_raises_adapter_error_with_timeout_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)

            class HangingClient:
                def __init__(self, options=None) -> None:
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def query(self, prompt, session_id: str = "default") -> None:
                    return None

                async def receive_response(self):
                    await asyncio.sleep(30)
                    yield  # pragma: no cover

            fake_sdk = SimpleNamespace(
                ClaudeSDKClient=HangingClient,
                ClaudeAgentOptions=lambda **kwargs: SimpleNamespace(kwargs=kwargs),
                HookMatcher=lambda **kwargs: SimpleNamespace(**kwargs),
                AssistantMessage=type("AssistantMessage", (), {}),
                ResultMessage=type("ResultMessage", (), {}),
            )

            request = _advisory_request(
                timeout=0.05, provider="anthropic", model="claude-sonnet-4-6"
            )
            with patch("claw_v2.adapters.anthropic._load_sdk", return_value=fake_sdk):
                with self.assertRaises(AdapterError) as ctx:
                    await executor._run(request)

            self.assertEqual(ctx.exception.metadata.get("reason"), "timeout")
            self.assertIn("timed out", str(ctx.exception))


class OpenAITimeoutEnforcementTests(unittest.TestCase):
    def test_request_timeout_is_passed_to_client(self) -> None:
        recorder: dict[str, object] = {}

        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(
                    output=[],
                    output_text="ok",
                    usage={"input_tokens": 1, "output_tokens": 1},
                    id="resp-1",
                )

        class FakeClient:
            def __init__(self) -> None:
                self.responses = FakeResponses()

            def with_options(self, *, timeout):
                recorder["timeout"] = timeout
                return self

        fake_sdk = SimpleNamespace(OpenAI=lambda **kwargs: FakeClient())
        adapter = OpenAIAdapter(api_key="sk-test")
        request = _advisory_request(timeout=42.0, provider="openai", model="gpt-5.4-mini")

        with patch.object(OpenAIAdapter, "_load_sdk", staticmethod(lambda: fake_sdk)):
            response = adapter.complete(request)

        self.assertEqual(recorder["timeout"], 42.0)
        self.assertEqual(response.content, "ok")


if __name__ == "__main__":
    unittest.main()
