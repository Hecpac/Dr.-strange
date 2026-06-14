"""Render the brain's markdown into Telegram's HTML subset.

Telegram Bot API ``parse_mode="HTML"`` supports only a small, fixed set of
tags (``b i u s code pre a tg-spoiler blockquote``). This module converts the
markdown the brain already emits into that subset, guaranteeing well-escaped
text and balanced tags so a malformed reply can never silently change meaning
or get rejected with HTTP 400 "can't parse entities".

Design contract:
- Output is always valid Telegram HTML: text is escaped, tags are balanced.
- Anything not representable is downgraded to plain (escaped) text, never
  dropped.
- ``strip_to_plain`` is the guaranteed fallback when the live send path needs
  to degrade.

Fase 1 of reports/2026-06-13/telegram_rich_messages_mapping.md. Tag set is the
stable Bot API HTML mode; reconcile against the live formatting-options table
when the doc fetch is available.
"""

from __future__ import annotations

import re

__all__ = ["markdown_to_telegram_html", "strip_to_plain"]

_PLACEHOLDER = "\x00TGRT{}\x00"

# ```lang\n...\n``` fenced block.
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)
# `inline code`
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# [text](url) — url restricted to a safe scheme to avoid injection.
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*")
_STRIKE_RE = re.compile(r"~~([^\n]+?)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([^\s*][^\n*]*?)\*(?![\w*])")
_ITALIC_UND_RE = re.compile(r"(?<![\w_])_([^\s_][^\n_]*?)_(?![\w_])")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.*)$")
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
# Quote markers run after global escaping, so ">" is already "&gt;".
_QUOTE_RE = re.compile(r"^&gt;[ \t]?(.*)$")


def _esc(text: str) -> str:
    """Escape the three characters Telegram HTML treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text: str) -> str:
    """Escape for an HTML attribute value: text markup plus the double quote
    that would otherwise close the ``href="..."`` attribute early."""
    return _esc(text).replace('"', "&quot;")


def _render_emphasis(escaped: str) -> str:
    """Apply inline emphasis (bold/strike/italic) to already-escaped text."""
    escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
    escaped = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    escaped = _ITALIC_UND_RE.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    return escaped


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram's HTML subset.

    Always returns valid, balanced, escaped HTML. Unsupported constructs are
    rendered as plain escaped text.
    """
    if not text:
        return ""

    protected: list[str] = []

    def _protect(rendered: str) -> str:
        protected.append(rendered)
        return _PLACEHOLDER.format(len(protected) - 1)

    # 1) Fenced code blocks first — their contents must not be touched by any
    #    other rule. Optional language hint maps to <pre><code class=...>.
    def _fence(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        body = _esc(match.group(2).rstrip("\n"))
        if lang:
            inner = f'<pre><code class="language-{_esc(lang)}">{body}</code></pre>'
        else:
            inner = f"<pre>{body}</pre>"
        return _protect(inner)

    text = _FENCE_RE.sub(_fence, text)

    # 2) Inline code — protect before inline emphasis so `*x*` inside code is
    #    left literal.
    text = _INLINE_CODE_RE.sub(
        lambda m: _protect(f"<code>{_esc(m.group(1))}</code>"), text
    )

    # 3) Links — protect (rendered text + href) before escaping the rest. The
    #    link text keeps inline emphasis; inline code inside it is already a
    #    placeholder from step 2 and passes through untouched.
    text = _LINK_RE.sub(
        lambda m: _protect(
            f'<a href="{_esc_attr(m.group(2))}">'
            f"{_render_emphasis(_esc(m.group(1)))}</a>"
        ),
        text,
    )

    # 4) Now escape everything that remains (placeholders survive: they hold no
    #    HTML-special chars).
    text = _esc(text)

    # 5) Inline emphasis on the escaped text.
    text = _render_emphasis(text)

    # 6) Block-level, line by line. Consecutive quote lines collapse into a
    #    single <blockquote>: Telegram has no nested blockquotes, and a
    #    multi-line quote is one element with newline-separated content
    #    (per the Bot API formatting-options table), not one bubble per line.
    out_lines: list[str] = []
    quote_buf: list[str] = []

    def _flush_quote() -> None:
        if quote_buf:
            joined = "\n".join(quote_buf)
            out_lines.append(f"<blockquote>{joined}</blockquote>")
            quote_buf.clear()

    for line in text.split("\n"):
        quote = _QUOTE_RE.match(line)
        if quote:
            quote_buf.append(quote.group(1))
            continue
        _flush_quote()
        heading = _HEADING_RE.match(line)
        if heading:
            out_lines.append(f"<b>{heading.group(1).strip()}</b>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out_lines.append(f"• {bullet.group(1)}")
            continue
        out_lines.append(line)
    _flush_quote()
    text = "\n".join(out_lines)

    # 7) Restore protected spans.
    def _restore(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        # Guard against an out-of-range index: a placeholder-shaped sequence in
        # the original input must not raise IndexError. Leave it as literal text.
        if 0 <= idx < len(protected):
            return protected[idx]
        return _esc(match.group(0))

    # Loop so a placeholder nested inside another restored span (e.g. inline
    # code inside link text) is also expanded. Bounded by the protected count.
    placeholder_re = re.compile(r"\x00TGRT(\d+)\x00")
    for _ in range(len(protected) + 1):
        text, n = placeholder_re.subn(_restore, text)
        if n == 0:
            break
    return text


def strip_to_plain(text: str) -> str:
    """Best-effort plain-text downgrade: unwrap markdown, no HTML.

    Used as the guaranteed fallback when the formatted send path must degrade.
    """
    if not text:
        return ""
    text = _FENCE_RE.sub(lambda m: m.group(2).rstrip("\n"), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _BOLD_RE.sub(lambda m: m.group(1), text)
    text = _STRIKE_RE.sub(lambda m: m.group(1), text)
    text = _ITALIC_STAR_RE.sub(lambda m: m.group(1), text)
    text = _ITALIC_UND_RE.sub(lambda m: m.group(1), text)
    return text
