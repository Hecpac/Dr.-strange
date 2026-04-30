from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from claw_v2.config import AppConfig
from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl


class GenerateIdTests(unittest.TestCase):
    def test_prefix_is_included(self) -> None:
        self.assertTrue(generate_id("g").startswith("g_"))

    def test_ids_are_unique(self) -> None:
        self.assertEqual(len({generate_id("e") for _ in range(100)}), 100)

    def test_id_contains_only_safe_chars(self) -> None:
        self.assertRegex(generate_id("claim"), r"^claim_[0-9a-f]+$")


class NowIsoTests(unittest.TestCase):
    def test_returns_timezone_aware_iso_string(self) -> None:
        parsed = datetime.fromisoformat(now_iso())
        self.assertIsNotNone(parsed.tzinfo)

    def test_omits_microseconds(self) -> None:
        self.assertNotIn(".", now_iso().split("T", maxsplit=1)[1])


class JsonlTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_creates_parent_and_writes_json_line(self) -> None:
        path = self.root / "nested" / "events.jsonl"
        append_jsonl(path, {"key": "value"})

        rows = read_jsonl(path)
        self.assertEqual(rows, [{"key": "value"}])

    def test_redacts_sensitive_fields_before_write(self) -> None:
        path = self.root / "events.jsonl"
        append_jsonl(path, {"telegram_bot_token": "secret-token-123456"})

        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("secret-token-123456", raw)
        self.assertIn("[REDACTED]", raw)

    def test_thread_safe_concurrent_writes(self) -> None:
        path = self.root / "concurrent.jsonl"
        errors: list[Exception] = []

        def write(n: int) -> None:
            try:
                append_jsonl(path, {"n": n})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 20)

    def test_read_jsonl_skips_corrupt_lines(self) -> None:
        path = self.root / "events.jsonl"
        path.write_text('{"ok":true}\nnot-json\n{"ok":false}\n', encoding="utf-8")

        self.assertEqual(read_jsonl(path), [{"ok": True}, {"ok": False}])

    def test_lines_are_valid_json(self) -> None:
        path = self.root / "events.jsonl"
        append_jsonl(path, {"n": 1})

        json.loads(path.read_text(encoding="utf-8").strip())


class TelemetryConfigTests(unittest.TestCase):
    def test_config_uses_telemetry_root_env_and_creates_directory(self) -> None:
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, {"TELEMETRY_ROOT": str(root / "telemetry")}, clear=True):
                config = AppConfig.from_env()
            config.ensure_directories()

            self.assertEqual(config.telemetry_root, root / "telemetry")
            self.assertTrue(config.telemetry_root.exists())


if __name__ == "__main__":
    unittest.main()

