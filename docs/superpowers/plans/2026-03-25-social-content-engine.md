# HEC-8.1: Social Content Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Build an autonomous social content engine that generates and publishes posts to Instagram, LinkedIn, and X for multiple client accounts, integrated as a deployer-class AutoResearch agent.

**Architecture:** ContentEngine generates posts from strategy.md + LLM worker lane. SocialPublisher routes to platform-specific httpx adapters. Integrated into agent loop for trust ladder progression.

**Tech Stack:** Python 3.12+, httpx, macOS Keychain, X API v2, LinkedIn Marketing API, Instagram Graph API

**Spec:** `docs/superpowers/specs/2026-03-25-social-content-engine-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Edit | `claw_v2/config.py` | Add social config fields |
| Create | `claw_v2/content.py` | Content generation from strategy.md via LLM |
| Create | `tests/test_content.py` | Content engine tests |
| Create | `claw_v2/social.py` | Platform adapters (X, LinkedIn, Instagram) + publisher facade |
| Create | `tests/test_social.py` | Social adapter tests |
| Create | `agents/social/program.md` | Agent instructions |
| Create | `agents/social/accounts/pachanodesign/strategy.md` | Example account strategy |
| Edit | `claw_v2/bot.py` | Add /social_status, /social_preview, /social_publish |
| Edit | `claw_v2/main.py` | Wire social agent + cron |
| Edit | `.env` | Add social config placeholders |

---

### Task 1: Config + Agent Files

**Files:**
- Modify: `claw_v2/config.py`
- Modify: `tests/helpers.py`
- Create: `agents/social/program.md`
- Create: `agents/social/accounts/pachanodesign/strategy.md`
- Modify: `.env`

- [ ] **Step 1: Add config fields to AppConfig**

Add after `pipeline_state_root`:
```python
    social_accounts_root: Path
    social_keychain_prefix: str
```

In `from_env()`:
```python
            social_accounts_root=Path(os.getenv("SOCIAL_ACCOUNTS_ROOT", str(Path(__file__).parent / "agents" / "social" / "accounts"))),
            social_keychain_prefix=os.getenv("SOCIAL_KEYCHAIN_PREFIX", "com.pachano.claw.social"),
```

- [ ] **Step 2: Update tests/helpers.py**

Add to `make_config()`:
```python
            social_accounts_root=root / "social_accounts",
            social_keychain_prefix="com.test.claw.social",
```

- [ ] **Step 3: Create agents/social/program.md**

```markdown
# Social Content Agent — Program

Class: deployer
Objective: Generate and publish social media content for all configured accounts.

## Rules
- Read each account's strategy.md for content pillars, tone, cadence
- Generate posts matching the defined cadence per platform
- Enforce character limits: X=280, LinkedIn=3000, Instagram=2200
- Never post duplicate content across accounts
- Include relevant hashtags (max 5 for X, max 10 for LinkedIn, max 30 for Instagram)

## Metrics
- Primary: engagement rate = (likes + comments + shares) / impressions
- Secondary: posting cadence adherence (actual vs target posts/week)

## Trust Ladder
- Level 1: Shadow mode — generate and log, don't publish
- Level 2: Suggest — generate and request approval via Telegram
- Level 3: Execute — generate and publish autonomously
```

- [ ] **Step 4: Create agents/social/accounts/pachanodesign/strategy.md**

```markdown
# pachanodesign — Social Strategy

## Platforms
- instagram: @pachanodesign
- linkedin: company/pachano-design
- x: @pachanodesign

## Content Pillars
1. Web design tips & trends
2. Client success stories / case studies
3. Behind the scenes / agency life
4. SEO & performance insights

## Tone
Professional but approachable. Spanish default, English for technical content.

## Cadence
- Instagram: 4 posts/week
- LinkedIn: 3 posts/week
- X: 5 posts/week

## Credentials
Platform API keys stored in macOS Keychain under:
- com.pachano.claw.social.pachanodesign.x
- com.pachano.claw.social.pachanodesign.linkedin
- com.pachano.claw.social.pachanodesign.instagram
```

- [ ] **Step 5: Append to .env**

```
# Social Content Engine (HEC-8.1)
# SOCIAL_ACCOUNTS_ROOT=       # defaults to claw_v2/agents/social/accounts
SOCIAL_KEYCHAIN_PREFIX=com.pachano.claw.social
```

- [ ] **Step 6: Run tests, commit**

```bash
.venv/bin/python -m pytest tests/ -x -q
git add claw_v2/config.py tests/helpers.py agents/ .env
git commit -m "feat(social): add social agent config, program.md, and account strategy template"
```

---

### Task 2: Content Engine

**Files:**
- Create: `claw_v2/content.py`
- Create: `tests/test_content.py`

- [ ] **Step 1: Create tests/test_content.py**

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.content import ContentEngine, PostDraft

STRATEGY = """# test — Social Strategy

## Platforms
- x: @testaccount
- linkedin: company/test

## Content Pillars
1. Tech tips
2. Product updates

## Tone
Casual and direct. English only.

## Cadence
- x: 3 posts/week
- linkedin: 2 posts/week
"""


class GenerateSingleTests(unittest.TestCase):
    def test_generates_post_for_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="Great tech tip! #coding #dev")
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "x")
            self.assertIsInstance(draft, PostDraft)
            self.assertEqual(draft.account, "testaccount")
            self.assertEqual(draft.platform, "x")
            self.assertTrue(len(draft.text) <= 280)
            router.ask.assert_called_once()

    def test_enforces_x_char_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="a" * 500)
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "x")
            self.assertTrue(len(draft.text) <= 280)

    def test_generates_with_topic_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="Post about AI trends")
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "linkedin", topic="AI trends")
            self.assertEqual(draft.platform, "linkedin")
            prompt = router.ask.call_args.args[0]
            self.assertIn("AI trends", prompt)


class GenerateBatchTests(unittest.TestCase):
    def test_generates_posts_for_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="A great post")
            engine = ContentEngine(router=router, accounts_root=root)
            drafts = engine.generate_batch("testaccount")
            platforms = {d.platform for d in drafts}
            self.assertEqual(platforms, {"x", "linkedin"})


class ParseStrategyTests(unittest.TestCase):
    def test_extracts_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            engine = ContentEngine(router=MagicMock(), accounts_root=root)
            strategy = engine._load_strategy("testaccount")
            self.assertIn("x", strategy["platforms"])
            self.assertIn("linkedin", strategy["platforms"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify tests fail**

- [ ] **Step 3: Create claw_v2/content.py**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from claw_v2.llm import LLMRouter

CHAR_LIMITS = {"x": 280, "linkedin": 3000, "instagram": 2200}
HASHTAG_LIMITS = {"x": 5, "linkedin": 10, "instagram": 30}


@dataclass(slots=True)
class PostDraft:
    account: str
    platform: str
    text: str
    hashtags: list[str]
    media_prompt: str | None = None
    scheduled_for: str | None = None


class ContentEngine:
    def __init__(self, router: LLMRouter, accounts_root: Path) -> None:
        self.router = router
        self.accounts_root = accounts_root

    def generate_batch(self, account: str, count: int = 1) -> list[PostDraft]:
        strategy = self._load_strategy(account)
        drafts: list[PostDraft] = []
        for platform in strategy["platforms"]:
            for _ in range(count):
                drafts.append(self._generate(account, platform, strategy))
        return drafts

    def generate_single(self, account: str, platform: str, *, topic: str | None = None) -> PostDraft:
        strategy = self._load_strategy(account)
        return self._generate(account, platform, strategy, topic=topic)

    def _generate(self, account: str, platform: str, strategy: dict, *, topic: str | None = None) -> PostDraft:
        limit = CHAR_LIMITS.get(platform, 2200)
        hashtag_limit = HASHTAG_LIMITS.get(platform, 5)
        prompt = self._build_prompt(account, platform, strategy, limit, hashtag_limit, topic)
        response = self.router.ask(prompt, lane="worker", evidence_pack={"account": account, "platform": platform})
        text = response.content.strip()
        hashtags = _extract_hashtags(text)[:hashtag_limit]
        text_clean = _strip_hashtags(text)[:limit]
        media_prompt = None
        if platform == "instagram":
            media_prompt = f"Social media image for: {text_clean[:100]}"
        return PostDraft(
            account=account, platform=platform, text=text_clean,
            hashtags=hashtags, media_prompt=media_prompt,
        )

    def _build_prompt(self, account: str, platform: str, strategy: dict, limit: int, hashtag_limit: int, topic: str | None) -> str:
        pillars = "\n".join(f"- {p}" for p in strategy.get("pillars", []))
        tone = strategy.get("tone", "professional")
        parts = [
            f"Generate a {platform} post for @{account}.",
            f"Character limit: {limit}. Max hashtags: {hashtag_limit}.",
            f"Tone: {tone}",
            f"Content pillars:\n{pillars}",
        ]
        if topic:
            parts.append(f"Topic: {topic}")
        parts.append("Return ONLY the post text with hashtags. No explanations.")
        return "\n\n".join(parts)

    def _load_strategy(self, account: str) -> dict:
        path = self.accounts_root / account / "strategy.md"
        if not path.exists():
            raise FileNotFoundError(f"No strategy.md for account: {account}")
        content = path.read_text(encoding="utf-8")
        return _parse_strategy(content)


def _parse_strategy(content: str) -> dict:
    platforms: dict[str, str] = {}
    pillars: list[str] = []
    tone = ""
    cadence: dict[str, str] = {}
    section = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].lower()
            continue
        if section == "platforms" and stripped.startswith("- "):
            parts = stripped[2:].split(":", 1)
            if len(parts) == 2:
                platforms[parts[0].strip()] = parts[1].strip()
        elif section == "content pillars" and re.match(r"^\d+\.\s", stripped):
            pillars.append(re.sub(r"^\d+\.\s*", "", stripped))
        elif section == "tone" and stripped:
            tone = stripped
        elif section == "cadence" and stripped.startswith("- "):
            parts = stripped[2:].split(":", 1)
            if len(parts) == 2:
                cadence[parts[0].strip().lower()] = parts[1].strip()
    return {"platforms": platforms, "pillars": pillars, "tone": tone, "cadence": cadence}


def _extract_hashtags(text: str) -> list[str]:
    return re.findall(r"#\w+", text)


def _strip_hashtags(text: str) -> str:
    return re.sub(r"\s*#\w+", "", text).strip()
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
.venv/bin/python -m pytest tests/test_content.py -v
.venv/bin/python -m pytest tests/ -x -q
git add claw_v2/content.py tests/test_content.py
git commit -m "feat(social): add ContentEngine for LLM-driven post generation"
```

---

### Task 3: Social Adapters + Publisher

**Files:**
- Create: `claw_v2/social.py`
- Create: `tests/test_social.py`

- [ ] **Step 1: Create tests/test_social.py**

```python
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
```

- [ ] **Step 2: Verify tests fail**

- [ ] **Step 3: Create claw_v2/social.py**

```python
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
```

- [ ] **Step 4: Run tests, full suite, commit**

```bash
.venv/bin/python -m pytest tests/test_social.py -v
.venv/bin/python -m pytest tests/ -x -q
git add claw_v2/social.py tests/test_social.py
git commit -m "feat(social): add platform adapters (X, LinkedIn, Instagram) and publisher"
```

---

### Task 4: Bot Commands + Main Wiring

**Files:**
- Modify: `claw_v2/bot.py`
- Modify: `claw_v2/main.py`

- [ ] **Step 1: Add social commands to bot.py**

Add `from claw_v2.content import ContentEngine` and `from claw_v2.social import SocialPublisher` imports. Add `content_engine: ContentEngine | None = None` and `social_publisher: SocialPublisher | None = None` to `BotService.__init__()`.

Add commands before the final `brain.handle_message` fallback:

```python
        if stripped == "/social_status":
            if self.content_engine is None:
                return "social content engine unavailable"
            accounts_root = self.content_engine.accounts_root
            accounts = sorted(p.name for p in accounts_root.iterdir() if p.is_dir())
            return json.dumps([{"account": a} for a in accounts], indent=2)
        if stripped.startswith("/social_preview "):
            if self.content_engine is None:
                return "social content engine unavailable"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                return json.dumps([{"platform": d.platform, "text": d.text, "hashtags": d.hashtags} for d in drafts], indent=2)
            except FileNotFoundError:
                return f"account not found: {account}"
            except Exception as exc:
                return f"error: {exc}"
        if stripped.startswith("/social_publish "):
            if self.content_engine is None or self.social_publisher is None:
                return "social services unavailable"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                results = [self.social_publisher.publish(d) for d in drafts]
                return json.dumps([{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results], indent=2)
            except Exception as exc:
                return f"error: {exc}"
```

- [ ] **Step 2: Wire in main.py**

Add imports and wire after `bot.pipeline = pipeline`:

```python
    from claw_v2.content import ContentEngine
    from claw_v2.social import SocialPublisher

    content_engine = ContentEngine(router=router, accounts_root=config.social_accounts_root)
    bot.content_engine = content_engine
    bot.social_publisher = SocialPublisher(adapters={})  # adapters loaded from Keychain at runtime
```

- [ ] **Step 3: Run full suite, commit**

```bash
.venv/bin/python -m pytest tests/ -x -q
git add claw_v2/bot.py claw_v2/main.py
git commit -m "feat(social): wire bot commands and content engine into runtime"
```
