"""Tests for telegram.py polling singleton lock (P0 hotfix E).

On 2026-05-24 the bot logged repeated ``telegram.error.Conflict:
Conflict: terminated by other getUpdates request`` — two processes were
polling the same token. The PID-file guard wasn't enough. A token-hash
flock keeps two daemons on different PIDs from racing.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.telegram import (
    PollingLockConflict,
    _polling_lock_path,
    _token_hash,
    acquire_polling_lock,
)


class TokenHashTests(unittest.TestCase):
    def test_same_token_produces_same_hash(self) -> None:
        self.assertEqual(_token_hash("123:abc"), _token_hash("123:abc"))

    def test_different_tokens_produce_different_hashes(self) -> None:
        self.assertNotEqual(_token_hash("123:abc"), _token_hash("999:zzz"))

    def test_hash_is_short_hex(self) -> None:
        hashed = _token_hash("abc")
        self.assertEqual(len(hashed), 16)
        int(hashed, 16)  # parses as hex


class PollingLockPathTests(unittest.TestCase):
    def test_lock_path_includes_token_hash(self) -> None:
        base = Path("/tmp/claw-test")
        path = _polling_lock_path("abc", base_dir=base)
        self.assertEqual(path.parent, base)
        self.assertIn(_token_hash("abc"), path.name)
        self.assertTrue(path.name.endswith(".lock"))


class TelegramPollingSingletonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())

    def test_telegram_polling_refuses_second_instance_same_token(self) -> None:
        # Pre-occupy the lock as if another live process held it.
        token = "987:realtoken"
        lock_path = _polling_lock_path(token, base_dir=self.base)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999")

        events: list[tuple[str, dict]] = []
        with patch("claw_v2.telegram._pid_alive", return_value=True):
            with self.assertRaises(PollingLockConflict) as ctx:
                acquire_polling_lock(
                    token,
                    base_dir=self.base,
                    observe=lambda et, payload: events.append((et, payload)),
                )

        self.assertEqual(ctx.exception.owner_pid, 99999)
        event_types = [name for name, _ in events]
        self.assertIn("telegram_polling_duplicate_instance", event_types)
        # Event payload must carry the token hash for diagnosis (never the raw token).
        payload = next(p for n, p in events if n == "telegram_polling_duplicate_instance")
        self.assertEqual(payload["token_hash"], _token_hash(token))
        self.assertNotIn(token, payload.values())

    def test_telegram_polling_allows_different_token(self) -> None:
        fh_a = acquire_polling_lock("token-A:x", base_dir=self.base)
        try:
            fh_b = acquire_polling_lock("token-B:y", base_dir=self.base)
            try:
                self.assertNotEqual(fh_a.name, fh_b.name)
                # Both processes own their own PID file recording our PID.
                self.assertEqual(Path(fh_a.name).read_text().strip(), str(os.getpid()))
                self.assertEqual(Path(fh_b.name).read_text().strip(), str(os.getpid()))
            finally:
                fh_b.close()
        finally:
            fh_a.close()

    def test_stale_pid_file_is_reclaimable(self) -> None:
        # If the previous owner died (PID gone), we must be allowed to claim.
        token = "abc:def"
        lock_path = _polling_lock_path(token, base_dir=self.base)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999")
        with patch("claw_v2.telegram._pid_alive", return_value=False):
            fh = acquire_polling_lock(token, base_dir=self.base)
        try:
            self.assertEqual(Path(fh.name).read_text().strip(), str(os.getpid()))
        finally:
            fh.close()


if __name__ == "__main__":
    unittest.main()
