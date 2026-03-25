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
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", account):
            raise ValueError(f"Invalid account name: {account}")
        path = self.accounts_root / account / "strategy.md"
        if not path.resolve().is_relative_to(self.accounts_root.resolve()):
            raise ValueError(f"Path traversal detected: {account}")
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
