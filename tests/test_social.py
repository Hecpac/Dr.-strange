from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.content import PostDraft
from claw_v2.social import (
    PublishResult,
    SocialPublisher,
    XAdapter,
    LinkedInAdapter,
    InstagramAdapter,
    _load_keychain_credential,
)


class XAdapterTests(unittest.TestCase):
    def test_publish_returns_result(self) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            status_code=201,
            json=MagicMock(return_value={"data": {"id": "123", "text": "hello"}}),
        )
        adapter = XAdapter(bearer_token="test-token", handle="@test", client=mock_client)
        result = adapter.publish("Hello world")
        self.assertIsInstance(result, PublishResult)
        self.assertEqual(result.platform, "x")
        self.assertEqual(result.post_id, "123")

    def test_get_engagement(self) -> None:
        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"data": {"public_metrics": {"like_count": 10, "reply_count": 2}}}),
        )
        adapter = XAdapter(bearer_token="test-token", handle="@test", client=mock_client)
        metrics = adapter.get_engagement("123")
        self.assertEqual(metrics["like_count"], 10)


class LinkedInAdapterTests(unittest.TestCase):
    def test_publish_returns_result(self) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            status_code=201,
            headers={"x-restli-id": "urn:li:share:456"},
            json=MagicMock(return_value={}),
        )
        adapter = LinkedInAdapter(access_token="test-token", org_id="org123", client=mock_client)
        result = adapter.publish("LinkedIn post")
        self.assertEqual(result.platform, "linkedin")
        self.assertEqual(result.post_id, "urn:li:share:456")


class InstagramAdapterTests(unittest.TestCase):
    def test_publish_returns_result(self) -> None:
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"id": "789"}),
        )
        adapter = InstagramAdapter(access_token="test-token", ig_user_id="ig123", client=mock_client)
        result = adapter.publish("Instagram caption")
        self.assertEqual(result.platform, "instagram")
        self.assertEqual(result.post_id, "789")


class SocialPublisherTests(unittest.TestCase):
    def test_routes_to_correct_adapter(self) -> None:
        x_adapter = MagicMock()
        x_adapter.publish.return_value = PublishResult(
            platform="x", account="test", post_id="1", url="https://x.com/1", published_at="2026-01-01",
        )
        publisher = SocialPublisher(adapters={"test": {"x": x_adapter}})
        draft = PostDraft(account="test", platform="x", text="hello", hashtags=[])
        result = publisher.publish(draft)
        self.assertEqual(result.platform, "x")
        x_adapter.publish.assert_called_once_with("hello", media_path=None)

    def test_raises_for_unknown_account(self) -> None:
        publisher = SocialPublisher(adapters={})
        draft = PostDraft(account="unknown", platform="x", text="hello", hashtags=[])
        with self.assertRaises(KeyError):
            publisher.publish(draft)


class KeychainTests(unittest.TestCase):
    @patch("claw_v2.social.subprocess")
    def test_load_credential_calls_security(self, mock_subprocess) -> None:
        mock_subprocess.run.return_value = MagicMock(
            returncode=0, stdout="secret-token\n",
        )
        result = _load_keychain_credential("com.test.claw.social.acct.x")
        self.assertEqual(result, "secret-token")
        mock_subprocess.run.assert_called_once()

    @patch("claw_v2.social.subprocess")
    def test_returns_none_when_not_found(self, mock_subprocess) -> None:
        mock_subprocess.run.return_value = MagicMock(returncode=44, stdout="")
        result = _load_keychain_credential("com.test.missing")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
