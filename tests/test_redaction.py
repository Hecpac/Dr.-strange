from __future__ import annotations

import unittest

from claw_v2.redaction import redact_sensitive


class RedactSensitiveTests(unittest.TestCase):
    def test_strips_openai_api_key(self) -> None:
        text = "Authorization: sk-abc123def456ghi789jkl012mno345"
        self.assertNotIn("sk-abc", redact_sensitive(text))
        self.assertIn("[REDACTED]", redact_sensitive(text))

    def test_strips_github_token(self) -> None:
        text = "token=ghp_abcdef1234567890abcdef12 done"
        self.assertNotIn("ghp_abcdef", redact_sensitive(text))

    def test_strips_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", redact_sensitive(text))

    def test_strips_query_string_token(self) -> None:
        text = "https://example.com/form?token=secret-token-123456789&mode=cyber"
        result = redact_sensitive(text)
        self.assertNotIn("secret-token-123456789", result)
        self.assertIn("[REDACTED]", result)

    def test_strips_google_api_key(self) -> None:
        text = "GOOGLE_API_KEY=AIzaSyDapFhUqfj8Inzc8GeEfzLRqsg3Hlrq2Bs"
        result = redact_sensitive(text)
        self.assertNotIn("AIzaSy", result)
        self.assertIn("[REDACTED]", result)

    def test_strips_approve_command(self) -> None:
        text = "User typed /approve abc-123 secret-token-xyz to confirm"
        result = redact_sensitive(text)
        self.assertNotIn("secret-token-xyz", result)
        self.assertIn("[REDACTED]", result)

    def test_strips_approval_token_field(self) -> None:
        text = '{"approval_token": "abc123def456"}'
        self.assertNotIn("abc123def456", redact_sensitive(text))

    def test_truncates_long_text(self) -> None:
        text = "a" * 5000
        result = redact_sensitive(text, limit=100)
        self.assertTrue(result.endswith("…[truncated]"))
        self.assertLess(len(result), 200)

    def test_handles_none(self) -> None:
        self.assertEqual(redact_sensitive(None), "")

    def test_passes_through_safe_text(self) -> None:
        text = "Hello, this is a normal log line with nothing sensitive."
        self.assertEqual(redact_sensitive(text), text)


class RecursiveRedactionTests(unittest.TestCase):
    def test_recursive_dict(self) -> None:
        import json
        payload = {
            "cmd": "/approve abc secret-token-xyz",
            "nested": {"key": "Bearer eyJhbGciOi.payload.sig123"},
        }
        redacted = redact_sensitive(payload)
        as_text = json.dumps(redacted)
        self.assertNotIn("secret-token-xyz", as_text)
        self.assertNotIn("eyJhbGciOi", as_text)
        self.assertIn("[REDACTED]", as_text)

    def test_field_name_redaction(self) -> None:
        payload = {"approval_token": "abc123def456ghi", "task_id": "task-1"}
        redacted = redact_sensitive(payload)
        self.assertEqual(redacted["approval_token"], "[REDACTED]")
        self.assertEqual(redacted["task_id"], "task-1")

    def test_field_name_fragment_redaction(self) -> None:
        payload = {"telegram_bot_token": "abc123def456ghi", "safe": "hello"}
        redacted = redact_sensitive(payload)
        self.assertEqual(redacted["telegram_bot_token"], "[REDACTED]")
        self.assertEqual(redacted["safe"], "hello")

    def test_list_of_dicts(self) -> None:
        import json
        payload = [{"token": "abc-1234567890"}, {"safe": "hello"}]
        redacted = redact_sensitive(payload)
        as_text = json.dumps(redacted)
        self.assertNotIn("abc-1234567890", as_text)
        self.assertIn("hello", as_text)

    def test_extended_api_key_patterns(self) -> None:
        for line in (
            "OPENAI_API_KEY=sk-test-very-long-key-12345",
            "ANTHROPIC_API_KEY=ant-key-very-long-67890",
            "LINEAR_API_KEY=lin_xxxxxxxxxxxx",
            "/social_approve abc123 token-xyz-456",
        ):
            with self.subTest(line=line):
                self.assertIn("[REDACTED]", redact_sensitive(line))

    def test_pipeline_merge_confirm_redacted(self) -> None:
        text = "user typed /pipeline_merge_confirm abc-123 token-456"
        self.assertNotIn("token-456", redact_sensitive(text))


if __name__ == "__main__":
    unittest.main()
