# HEC-8 Sub-project 1: Social Content Engine — Design Spec

**Date:** 2026-03-25
**Status:** Approved
**Scope:** Autonomous social media content generation and publishing for Instagram, LinkedIn, and X across 4-10 client accounts

---

## 1. Context

Claw v2.1 has a functioning agent runtime with AutoResearch experiment loops, trust ladder (shadow → suggest → execute), LLM routing, and cron scheduling. HEC-8 adds marketing/content agents. This sub-project builds the foundation: a content engine that generates and publishes social media posts.

## 2. Approach

**Social Content Agent as AutoResearch agent** (deployer class). Two new modules:
- `content.py` (~150 lines) — content generation using LLM worker lane, constrained by per-account strategy.md
- `social.py` (~200 lines) — platform adapters for X, LinkedIn, Instagram via httpx REST APIs

Reuses: `AutoResearchAgentService` for experiment loop, `LLMRouter` (worker lane) for generation, `CronScheduler` for daily posting, trust ladder for autonomy progression.

## 3. Design

### 3.1 Account Configuration

Each account gets a directory under `agents/social/accounts/`:

```
agents/social/
├── program.md              — Agent-level instructions (HUMAN-ONLY)
├── state.json              — Agent state (trust level, metrics)
├── results.tsv             — Experiment log
└── accounts/
    ├── pachanodesign/
    │   └── strategy.md
    ├── tcinsurancetx/
    │   └── strategy.md
    └── ...
```

`strategy.md` format:
```markdown
# {account} — Social Strategy

## Platforms
- instagram: @handle
- linkedin: company/slug
- x: @handle

## Content Pillars
1. Pillar one
2. Pillar two
...

## Tone
Description of voice and language preferences.

## Cadence
- Instagram: N posts/week
- LinkedIn: N posts/week
- X: N posts/week

## Credentials
Platform API keys stored in macOS Keychain under:
- com.pachano.claw.social.{account}.x
- com.pachano.claw.social.{account}.linkedin
- com.pachano.claw.social.{account}.instagram
```

No API keys in the file — Keychain reference only. Social adapter reads them at runtime via `security find-generic-password`.

### 3.2 content.py — Content Generation (~150 lines)

```python
@dataclass(slots=True)
class PostDraft:
    account: str           # "pachanodesign"
    platform: str          # "instagram" | "linkedin" | "x"
    text: str
    hashtags: list[str]
    media_prompt: str | None  # Image description (Instagram), not executed this iteration
    scheduled_for: str | None # ISO datetime

class ContentEngine:
    def __init__(self, router: LLMRouter, accounts_root: Path): ...

    def generate_batch(self, account: str, count: int = 1) -> list[PostDraft]:
        """Read strategy.md, generate `count` posts per platform."""

    def generate_single(self, account: str, platform: str, *, topic: str | None = None) -> PostDraft:
        """Generate one post for a specific platform."""
```

Key decisions:
- Reads strategy.md as LLM context (content pillars, tone, cadence)
- Uses `worker` lane (execution, not orchestration)
- Character limits enforced per platform: X=280, LinkedIn=3000, Instagram=2200
- `media_prompt` saved but image generation deferred (future: DALL-E/Figma MCP)
- No platform-specific formatting in engine — adapter handles that

### 3.3 social.py — Platform Adapters (~200 lines)

```python
@dataclass(slots=True)
class PublishResult:
    platform: str
    account: str
    post_id: str
    url: str
    published_at: str

class SocialAdapter:
    """Base interface — one per platform."""
    def publish(self, text: str, *, media_path: Path | None = None) -> PublishResult
    def get_engagement(self, post_id: str) -> dict

class XAdapter(SocialAdapter): ...       # X API v2 via httpx
class LinkedInAdapter(SocialAdapter): ... # LinkedIn Marketing API via httpx
class InstagramAdapter(SocialAdapter): ... # Instagram Graph API via httpx

class SocialPublisher:
    def __init__(self, adapters: dict[str, dict[str, SocialAdapter]]): ...
    # adapters["pachanodesign"]["x"] = XAdapter(...)

    def publish(self, draft: PostDraft) -> PublishResult
    def get_engagement(self, account: str, platform: str, post_id: str) -> dict
```

Key decisions:
- httpx for all platforms — no heavy SDK dependencies
- Adapters are per-account-per-platform (each has own credentials)
- Credentials from macOS Keychain via `security find-generic-password` subprocess
- `SocialPublisher` is the facade for content engine and agent loop
- `get_engagement` returns raw metrics dict — agent loop uses this as experiment metric
- Media upload for Instagram: local file path, adapter handles upload

### 3.4 Agent Integration

**Agent class:** `deployer` (publishes externally = Tier 3)

**Experiment loop metric:** engagement rate = (likes + comments + shares) / impressions, averaged across batch

**Trust ladder:**
- Level 1 (Shadow): generates posts, logs what it would publish, reports to Telegram
- Level 2 (Suggest): generates posts, sends to Telegram for approval before publishing
- Level 3 (Execute): generates and publishes autonomously

**Cron:** Daily at 8am — generate and publish today's posts for all accounts

### 3.5 Bot Commands

- `/social_status` — all accounts, last post, next scheduled, trust level
- `/social_preview {account}` — generate preview batch (don't publish)
- `/social_publish {account}` — manually trigger publish

### 3.6 Config Additions

```python
# AppConfig:
social_accounts_root: Path      # SOCIAL_ACCOUNTS_ROOT, default agents/social/accounts
social_keychain_prefix: str     # SOCIAL_KEYCHAIN_PREFIX, default "com.pachano.claw.social"
```

## 4. Non-Goals (this iteration)

- Image generation for Instagram (media_prompt saved but not executed)
- Engagement analytics dashboard
- A/B testing of post variations
- Automatic strategy adjustment based on metrics
- Webhook-based posting (cron only)

## 5. Test Plan

| Test file | Coverage |
|-----------|----------|
| `test_content.py` (~100 lines) | Mock LLMRouter; generate_batch reads strategy.md, enforces char limits, returns PostDraft per platform; generate_single with topic override |
| `test_social.py` (~120 lines) | Mock httpx; XAdapter publish/engagement, LinkedInAdapter publish/engagement, InstagramAdapter publish/engagement; SocialPublisher routing; Keychain credential loading |

## 6. Files Summary

| Action | File | Lines (est.) |
|--------|------|-------------|
| Create | `claw_v2/content.py` | ~150 |
| Create | `claw_v2/social.py` | ~200 |
| Create | `agents/social/program.md` | ~30 |
| Create | `agents/social/accounts/pachanodesign/strategy.md` | ~25 |
| Edit | `claw_v2/config.py` | +3 |
| Edit | `claw_v2/bot.py` | +30 |
| Edit | `claw_v2/main.py` | +15 |
| Create | `tests/test_content.py` | ~100 |
| Create | `tests/test_social.py` | ~120 |

## 7. Constraints

- All new Python files under 250 lines
- bot.py tech debt continues (~483 lines after this) — refactor deferred
- Existing 91 tests must continue passing
- No API keys in repo — Keychain only
