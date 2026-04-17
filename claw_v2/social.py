from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PublishResult:
    platform: str
    account: str
    post_id: str
    url: str
    published_at: str


class XAdapter:
    """X (Twitter) API v2 adapter.

    Uses OAuth 1.0a (user context) for posting and OAuth 2.0 Bearer for reading.
    """

    def __init__(
        self,
        bearer_token: str,
        handle: str,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bearer_token = bearer_token
        self.handle = handle
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self._client = client

    def _read_client(self) -> Any:
        """Client for read operations (Bearer token)."""
        if self._client is not None:
            return self._client
        import httpx
        return httpx.Client(timeout=30)

    def _write_session(self) -> Any:
        """Return a requests.Session with OAuth 1.0a for write operations."""
        if self._client is not None:
            return self._client
        if not all([self.api_key, self.api_secret, self.access_token, self.access_token_secret]):
            raise ValueError(
                "X posting requires OAuth 1.0a credentials: "
                "api_key, api_secret, access_token, access_token_secret"
            )
        import requests
        from requests_oauthlib import OAuth1

        auth = OAuth1(self.api_key, self.api_secret, self.access_token, self.access_token_secret)
        session = requests.Session()
        session.auth = auth
        return session

    def publish(self, text: str, *, media_path: Path | None = None) -> PublishResult:
        url = "https://api.x.com/2/tweets"
        session = self._write_session()
        response = session.post(url, json={"text": text})
        response.raise_for_status()
        data = response.json().get("data", {})
        return PublishResult(
            platform="x", account=self.handle, post_id=data.get("id", ""),
            url=f"https://x.com/i/status/{data.get('id', '')}", published_at=datetime.now(UTC).isoformat(),
        )

    def get_engagement(self, post_id: str) -> dict:
        client = self._read_client()
        response = client.get(
            f"https://api.x.com/2/tweets/{post_id}",
            params={"tweet.fields": "public_metrics"},
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        return response.json().get("data", {}).get("public_metrics", {})



class LinkedInAdapter:
    def __init__(self, access_token: str, org_id: str, client: Any | None = None) -> None:
        self.access_token = access_token
        self.org_id = org_id
        self.client = client or self._default_client()

    def publish(self, text: str, *, media_path: Path | None = None) -> PublishResult:
        payload = {
            "author": f"urn:li:organization:{self.org_id}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE",
            }},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        response = self.client.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json=payload,
            headers={"Authorization": f"Bearer {self.access_token}", "X-Restli-Protocol-Version": "2.0.0"},
        )
        post_id = response.headers.get("x-restli-id", "")
        return PublishResult(
            platform="linkedin", account=self.org_id, post_id=post_id,
            url=f"https://www.linkedin.com/feed/update/{post_id}", published_at=datetime.now(UTC).isoformat(),
        )

    def get_engagement(self, post_id: str) -> dict:
        response = self.client.get(
            f"https://api.linkedin.com/v2/socialActions/{post_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        return response.json()

    @staticmethod
    def _default_client() -> Any:
        import httpx
        return httpx.Client(timeout=30)


class InstagramAdapter:
    def __init__(self, access_token: str, ig_user_id: str, client: Any | None = None) -> None:
        self.access_token = access_token
        self.ig_user_id = ig_user_id
        self.client = client or self._default_client()

    def publish(self, text: str, *, media_path: Path | None = None) -> PublishResult:
        # Text-only posts not supported on Instagram — requires image
        # For now: create a container with caption only (will fail without image_url in production)
        response = self.client.post(
            f"https://graph.facebook.com/v19.0/{self.ig_user_id}/media",
            params={"caption": text, "access_token": self.access_token},
        )
        post_id = response.json().get("id", "")
        return PublishResult(
            platform="instagram", account=self.ig_user_id, post_id=post_id,
            url=f"https://www.instagram.com/p/{post_id}", published_at=datetime.now(UTC).isoformat(),
        )

    def get_engagement(self, post_id: str) -> dict:
        response = self.client.get(
            f"https://graph.facebook.com/v19.0/{post_id}",
            params={"fields": "like_count,comments_count", "access_token": self.access_token},
        )
        return response.json()

    @staticmethod
    def _default_client() -> Any:
        import httpx
        return httpx.Client(timeout=30)


class SocialPublisher:
    def __init__(self, adapters: dict[str, dict[str, Any]]) -> None:
        self.adapters = adapters

    def publish(self, draft: Any) -> PublishResult:
        adapter = self.adapters[draft.account][draft.platform]
        return adapter.publish(draft.text, media_path=None)

    def get_engagement(self, account: str, platform: str, post_id: str) -> dict:
        return self.adapters[account][platform].get_engagement(post_id)


def _load_keychain_credential(service_name: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service_name, "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def x_adapter_from_keychain(handle: str = "haborpachano") -> XAdapter:
    """Create an XAdapter with credentials loaded from macOS Keychain."""
    bearer = _load_keychain_credential("x-bearer-token") or ""
    return XAdapter(
        bearer_token=bearer,
        handle=handle,
        api_key=_load_keychain_credential("x-api-key"),
        api_secret=_load_keychain_credential("x-api-secret"),
        access_token=_load_keychain_credential("x-access-token"),
        access_token_secret=_load_keychain_credential("x-access-token-secret"),
    )
