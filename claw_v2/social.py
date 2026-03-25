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
    def __init__(self, bearer_token: str, handle: str, client: Any | None = None) -> None:
        self.bearer_token = bearer_token
        self.handle = handle
        self.client = client or self._default_client()

    def publish(self, text: str, *, media_path: Path | None = None) -> PublishResult:
        response = self.client.post(
            "https://api.x.com/2/tweets",
            json={"text": text},
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        data = response.json().get("data", {})
        return PublishResult(
            platform="x", account=self.handle, post_id=data.get("id", ""),
            url=f"https://x.com/i/status/{data.get('id', '')}", published_at=datetime.now(UTC).isoformat(),
        )

    def get_engagement(self, post_id: str) -> dict:
        response = self.client.get(
            f"https://api.x.com/2/tweets/{post_id}",
            params={"tweet.fields": "public_metrics"},
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        return response.json().get("data", {}).get("public_metrics", {})

    @staticmethod
    def _default_client() -> Any:
        import httpx
        return httpx.Client(timeout=30)


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
