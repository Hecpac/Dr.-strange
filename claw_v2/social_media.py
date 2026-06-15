"""Social media engagement skills for Dr. Strange.

Three skills the brain can invoke:
  - draft_caption_scaffold: deterministic platform constraints + hook patterns
    + skeleton template. The brain writes the actual copy.
  - research_competitor: scrape public IG profile (header + recent post captions)
    via Chrome CDP. Reusable for benchmarking.
  - suggest_reply_scaffold: tone + length + structure guidance for replying
    to a comment. The brain writes the actual reply.

Why scaffolds (not full LLM calls): the brain is already an LLM. Tools should
provide the deterministic intelligence (platform limits, hook patterns, scrape
data) the LLM cannot memorize accurately. Generated copy stays in the brain's
context window, no extra API cost, no token explosion in tests.

Auto-engagement (auto-follow, auto-DM, auto-comment) is intentionally NOT
implemented. Meta detects automation and shadowbans new accounts.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "instagram_feed": {
        "caption_max": 2200,
        "visible_before_more": 125,
        "hashtag_max_recommended": 5,
    },
    "instagram_reel": {
        "caption_max": 2200,
        "visible_before_more": 125,
        "hashtag_max_recommended": 3,
    },
    "instagram_story": {
        "caption_max": 100,
        "visible_before_more": 100,
        "hashtag_max_recommended": 0,
    },
    "linkedin": {"caption_max": 3000, "visible_before_more": 210, "hashtag_max_recommended": 3},
    "x": {"caption_max": 280, "visible_before_more": 280, "hashtag_max_recommended": 2},
    "threads": {"caption_max": 500, "visible_before_more": 500, "hashtag_max_recommended": 2},
}

HOOK_PATTERNS: dict[str, list[str]] = {
    "contrarian": [
        "Everyone says X. They are wrong because...",
        "The advice you keep hearing is killing your...",
        "I used to believe X. Then I learned...",
    ],
    "specific_number": [
        "I tested N tools. Only one survived.",
        "After N {time_unit}, the pattern is clear:",
        "$N spent. N hours saved. Here is what worked:",
    ],
    "concrete_story": [
        "Yesterday my {agent/team/client} told me...",
        "At 3am on a Tuesday, this happened:",
        "I asked my AI to X. It said no. Here is why that mattered:",
    ],
    "question_loop": [
        "Why does X happen? Three reasons:",
        "What if Y were true? Then Z follows.",
        "How would your business change if X stopped working?",
    ],
}

REPLY_TONE_PRESETS: dict[str, str] = {
    "warm": "Acknowledge what the commenter said specifically. Add one concrete detail or follow-up question. Keep <150 chars. No exclamation marks.",
    "expert": "Affirm the commenter's framing only if you genuinely agree. If you disagree, say so directly with one reason. Keep <200 chars. Cite a specific number, tool, or example.",
    "playful": "Mirror commenter's energy. Use one unexpected word or twist. Avoid stock emojis. Keep <120 chars.",
    "direct": "Answer the implied question in one sentence. Skip pleasantries. Keep <100 chars.",
}


@dataclass(slots=True)
class CaptionScaffold:
    platform: str
    topic: str
    char_limit: int
    visible_before_more: int
    hashtag_max: int
    hooks_to_consider: list[str] = field(default_factory=list)
    structure: list[str] = field(default_factory=list)
    voice_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "topic": self.topic,
            "char_limit": self.char_limit,
            "visible_before_more": self.visible_before_more,
            "hashtag_max": self.hashtag_max,
            "hooks_to_consider": self.hooks_to_consider,
            "structure": self.structure,
            "voice_notes": self.voice_notes,
        }


@dataclass(slots=True)
class ReplyScaffold:
    platform: str
    incoming_comment: str
    tone: str
    tone_guidance: str
    target_length_chars: int
    structure: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "incoming_comment": self.incoming_comment,
            "tone": self.tone,
            "tone_guidance": self.tone_guidance,
            "target_length_chars": self.target_length_chars,
            "structure": self.structure,
        }


@dataclass(slots=True)
class CompetitorResearch:
    handle: str
    url: str
    found: bool
    followers: int | None = None
    following: int | None = None
    posts: int | None = None
    display_name: str = ""
    bio_text: str = ""
    bio_lines: list[str] = field(default_factory=list)
    recent_post_captions: list[str] = field(default_factory=list)
    observed_hook_patterns: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "url": self.url,
            "found": self.found,
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
            "display_name": self.display_name,
            "bio_text": self.bio_text,
            "bio_lines": self.bio_lines,
            "recent_post_captions": self.recent_post_captions,
            "observed_hook_patterns": self.observed_hook_patterns,
            "error": self.error,
        }


def draft_caption_scaffold(
    topic: str,
    platform: str = "instagram_reel",
    voice: str = "punchy_contrarian",
    hook_style: str = "contrarian",
) -> CaptionScaffold:
    """Return platform-aware scaffold for caption drafting.

    The scaffold gives the brain hard constraints (char limits, hashtag count)
    plus hook patterns to consider. The brain writes the actual caption.
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(
            f"Unknown platform '{platform}'. Choose from: "
            + ", ".join(sorted(PLATFORM_LIMITS.keys()))
        )
    limits = PLATFORM_LIMITS[platform]
    hooks = HOOK_PATTERNS.get(hook_style, HOOK_PATTERNS["contrarian"])

    structure = [
        f"Hook (line 1, fits before 'more' at ~{limits['visible_before_more']} chars)",
        "Body (1-3 short paragraphs, concrete example or number)",
        "Punchline or quotable line",
        "CTA (one specific action: DM keyword, link click, or comment prompt)",
    ]
    if limits["hashtag_max_recommended"] > 0:
        structure.append(
            f"Up to {limits['hashtag_max_recommended']} relevant hashtags "
            f"(NOT 30 — Meta penalizes hashtag spam since 2024)"
        )

    voice_notes_map = {
        "punchy_contrarian": "Lead with a claim that most readers will quietly disagree with, then earn it. Spanish neutral LATAM (tú-form), no voseo, no Spain forms. No flag emojis.",
        "warm_authority": "Lead with empathy for the reader's specific pain. Show credential through one concrete result, not adjectives.",
        "story_driven": "Lead with a time/place/concrete moment. Reveal the lesson at the end. No abstract framings.",
    }

    return CaptionScaffold(
        platform=platform,
        topic=topic,
        char_limit=limits["caption_max"],
        visible_before_more=limits["visible_before_more"],
        hashtag_max=limits["hashtag_max_recommended"],
        hooks_to_consider=hooks,
        structure=structure,
        voice_notes=voice_notes_map.get(voice, voice_notes_map["punchy_contrarian"]),
    )


def suggest_reply_scaffold(
    incoming_comment: str,
    platform: str = "instagram_feed",
    tone: str = "warm",
) -> ReplyScaffold:
    """Return tone + length + structure guidance for replying to a comment.

    The brain writes the actual reply text. This function never publishes.
    """
    if tone not in REPLY_TONE_PRESETS:
        raise ValueError(
            f"Unknown tone '{tone}'. Choose from: " + ", ".join(sorted(REPLY_TONE_PRESETS.keys()))
        )
    if platform not in PLATFORM_LIMITS:
        raise ValueError(f"Unknown platform '{platform}'")

    target_len_map = {"warm": 150, "expert": 200, "playful": 120, "direct": 100}
    structure = [
        "1. Read the comment for the implied question or emotion",
        "2. Acknowledge specifically — quote a word or phrase from the comment",
        "3. Add value: one concrete detail, number, or follow-up question",
        "4. Stop. No padding, no fishing for more engagement.",
    ]

    return ReplyScaffold(
        platform=platform,
        incoming_comment=incoming_comment,
        tone=tone,
        tone_guidance=REPLY_TONE_PRESETS[tone],
        target_length_chars=target_len_map.get(tone, 150),
        structure=structure,
    )


def _detect_hook_patterns(captions: list[str]) -> list[str]:
    """Classify the opening hook of each caption into a known pattern bucket."""
    out: list[str] = []
    for cap in captions:
        first_line = (cap or "").strip().split("\n", 1)[0].strip()
        lower = first_line.lower()
        if not first_line:
            continue
        if any(w in lower for w in ("everyone", "people think", "common", "wrong", "myth", "lie")):
            out.append(f"contrarian: {first_line[:80]}")
        elif any(w in first_line for w in ("?")):
            out.append(f"question: {first_line[:80]}")
        elif any(c.isdigit() for c in first_line[:30]):
            out.append(f"specific_number: {first_line[:80]}")
        elif any(
            w in lower
            for w in (
                "ayer",
                "yesterday",
                "esta mañana",
                "this morning",
                "el otro día",
                "the other day",
            )
        ):
            out.append(f"concrete_story: {first_line[:80]}")
        else:
            out.append(f"other: {first_line[:80]}")
    return out


def research_competitor(
    handle: str,
    cdp_url: str = "http://localhost:9250",
    recent_post_count: int = 6,
) -> CompetitorResearch:
    """Scrape a public IG profile via Chrome CDP.

    Returns header stats + recent post captions + hook-pattern classification.
    Read-only. Does not log in, does not engage, does not publish.
    """
    handle = handle.strip().lstrip("@")
    url = f"https://www.instagram.com/{handle}/"
    research = CompetitorResearch(handle=handle, url=url, found=False)

    script_path = Path("/tmp/_dr_strange_ig_competitor.py")
    script_body = f"""
from playwright.sync_api import sync_playwright
import json, re, sys

HANDLE = {handle!r}
URL = {url!r}
CDP_URL = {cdp_url!r}
RECENT = {recent_post_count!r}

def parse_followers(s):
    if not s: return None
    m = re.search(r'([\\d,.]+)\\s*([KMB]|mil|mill)?\\s*(seguidores|followers)', s, re.IGNORECASE)
    if not m: return None
    n = float(m.group(1).replace(',', ''))
    suf = (m.group(2) or '').lower()
    if suf in ('k',): n *= 1_000
    elif suf in ('m','mill'): n *= 1_000_000
    elif suf == 'mil': n *= 1_000
    elif suf == 'b': n *= 1_000_000_000
    return int(n)

out = {{'handle': HANDLE, 'url': URL, 'found': False}}
try:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if 'instagram.com' in (p.url or ''):
                    page = p
                    break
            if page: break
        if not page:
            print(json.dumps({{**out, 'error': 'no_ig_tab'}})); sys.exit(0)
        page.bring_to_front()
        page.goto(URL, wait_until='domcontentloaded', timeout=18000)
        page.wait_for_timeout(2200)
        data = page.evaluate('''() => {{
            const meta = (document.querySelector('meta[property=\"og:description\"]')?.content) || '';
            const title = (document.querySelector('meta[property=\"og:title\"]')?.content) || '';
            const headerText = (document.querySelector('header')?.innerText || '').slice(0,1200);
            const notFound = (document.body.innerText||'').includes('no está disponible') || (document.body.innerText||'').includes(\"Sorry, this page isn't available\");
            const linkEls = Array.from(document.querySelectorAll('article a')).map(a => a.href).filter(h => h.indexOf('/p/') !== -1 || h.indexOf('/reel/') !== -1);
            return {{ meta, title, headerText, notFound, postLinks: Array.from(new Set(linkEls)).slice(0, RECENT) }};
        }}''')
        if data.get('notFound'):
            print(json.dumps({{**out, 'error': 'profile_not_found'}})); sys.exit(0)
        out['found'] = True
        out['display_name'] = (data.get('title') or '').split(' (@')[0]
        out['followers'] = parse_followers(data.get('meta'))
        m_follow = re.search(r'(\\d[\\d.,]*)\\s*(seguidos|following)', data.get('meta') or '', re.IGNORECASE)
        m_posts = re.search(r'(\\d[\\d.,]*)\\s*(publicaciones|posts)', data.get('meta') or '', re.IGNORECASE)
        if m_follow:
            out['following'] = int(float(m_follow.group(1).replace(',', '').replace('.', '')))
        if m_posts:
            out['posts'] = int(float(m_posts.group(1).replace(',', '').replace('.', '')))
        out['bio_text'] = (data.get('headerText') or '')[:600]
        # Extract just the bio chunk between display name and post count
        lines = (data.get('headerText') or '').split('\\n')
        out['bio_lines'] = [l for l in lines if l.strip() and 'publicaciones' not in l and 'seguidores' not in l and 'seguidos' not in l and l.strip() != HANDLE][:8]
        # Fetch recent post captions
        captions = []
        for link in (data.get('postLinks') or [])[:RECENT]:
            try:
                p2 = browser.contexts[0].new_page()
                p2.goto(link, wait_until='domcontentloaded', timeout=12000)
                p2.wait_for_timeout(1500)
                meta_desc = p2.evaluate('() => (document.querySelector(\"meta[property=og:description]\")?.content) || (document.querySelector(\"meta[name=description]\")?.content) || \"\"')
                if meta_desc:
                    captions.append(meta_desc[:400])
                p2.close()
            except Exception as e:
                pass
        out['recent_post_captions'] = captions
except Exception as e:
    out['error'] = str(e)[:200]
print(json.dumps(out, ensure_ascii=False))
"""
    script_path.write_text(script_body)
    try:
        result = subprocess.run(
            [".venv/bin/python", str(script_path)],
            cwd="/Users/hector/Projects/Dr.-strange",
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception as exc:
        research.error = f"cdp_subprocess_failed: {exc}"
        return research

    research.found = bool(payload.get("found"))
    research.followers = payload.get("followers")
    research.following = payload.get("following")
    research.posts = payload.get("posts")
    research.display_name = payload.get("display_name", "")
    research.bio_text = payload.get("bio_text", "")
    research.bio_lines = payload.get("bio_lines", [])
    research.recent_post_captions = payload.get("recent_post_captions", [])
    research.observed_hook_patterns = _detect_hook_patterns(research.recent_post_captions)
    research.error = payload.get("error")
    return research
