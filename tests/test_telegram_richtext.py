"""Tests for claw_v2.telegram_richtext (Fase 1 of the Telegram rich-message work)."""

from __future__ import annotations

import re

from claw_v2.telegram_richtext import markdown_to_telegram_html, strip_to_plain

_TAG_RE = re.compile(r"</?([a-z-]+)(?:\s[^>]*)?>")


def _balanced(html: str) -> bool:
    """All generated tags open and close in LIFO order."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        tag = match.group(1)
        if match.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


class TestEscaping:
    def test_angle_brackets_and_amp_escaped(self) -> None:
        out = markdown_to_telegram_html("a < b & c > d")
        assert out == "a &lt; b &amp; c &gt; d"

    def test_no_raw_markup_leaks(self) -> None:
        out = markdown_to_telegram_html("if x<0 && y>0: pass")
        assert "<0" not in out
        assert "&&" in out.replace("&amp;&amp;", "&&")

    def test_plain_text_unchanged(self) -> None:
        assert markdown_to_telegram_html("hola mundo") == "hola mundo"

    def test_empty(self) -> None:
        assert markdown_to_telegram_html("") == ""


class TestInline:
    def test_bold(self) -> None:
        assert markdown_to_telegram_html("**fuerte**") == "<b>fuerte</b>"

    def test_italic_star(self) -> None:
        assert markdown_to_telegram_html("un *enfasis* aqui") == "un <i>enfasis</i> aqui"

    def test_italic_underscore(self) -> None:
        assert markdown_to_telegram_html("un _enfasis_ aqui") == "un <i>enfasis</i> aqui"

    def test_strike(self) -> None:
        assert markdown_to_telegram_html("~~no~~") == "<s>no</s>"

    def test_inline_code_escapes_inside(self) -> None:
        out = markdown_to_telegram_html("usa `a < b`")
        assert out == "usa <code>a &lt; b</code>"

    def test_markdown_inside_code_is_literal(self) -> None:
        out = markdown_to_telegram_html("`**no negrita**`")
        assert out == "<code>**no negrita**</code>"

    def test_link(self) -> None:
        out = markdown_to_telegram_html("[Telegram](https://core.telegram.org/bots)")
        assert out == '<a href="https://core.telegram.org/bots">Telegram</a>'

    def test_link_with_amp_in_url(self) -> None:
        out = markdown_to_telegram_html("[x](https://e.com/a?b=1&c=2)")
        assert out == '<a href="https://e.com/a?b=1&amp;c=2">x</a>'

    def test_link_with_quote_in_url_does_not_break_attr(self) -> None:
        out = markdown_to_telegram_html('[x](https://e.com/a?q="z")')
        assert out == '<a href="https://e.com/a?q=&quot;z&quot;">x</a>'

    def test_placeholder_shaped_input_does_not_crash(self) -> None:
        # A literal placeholder sequence must not raise IndexError; it has no
        # protected span, so it survives as escaped text.
        out = markdown_to_telegram_html("\x00TGRT9\x00")
        assert out == "\x00TGRT9\x00"

    def test_link_text_keeps_bold(self) -> None:
        out = markdown_to_telegram_html("[**bold** label](https://e.com)")
        assert out == '<a href="https://e.com"><b>bold</b> label</a>'

    def test_link_text_keeps_inline_code(self) -> None:
        out = markdown_to_telegram_html("[`run`](https://e.com)")
        assert out == '<a href="https://e.com"><code>run</code></a>'

    def test_underscore_in_identifier_not_italic(self) -> None:
        # snake_case must survive intact.
        assert markdown_to_telegram_html("send_text y parse_mode") == (
            "send_text y parse_mode"
        )


class TestBlocks:
    def test_heading_to_bold(self) -> None:
        assert markdown_to_telegram_html("## Titulo") == "<b>Titulo</b>"

    def test_bullet(self) -> None:
        assert markdown_to_telegram_html("- item uno") == "• item uno"

    def test_quote(self) -> None:
        assert markdown_to_telegram_html("> citado") == "<blockquote>citado</blockquote>"

    def test_multiline_quote_merges_into_one_blockquote(self) -> None:
        # Telegram has no nested blockquotes; consecutive quote lines are one
        # element with newline-separated content, not a bubble per line.
        out = markdown_to_telegram_html("> a\n> b\n> c")
        assert out == "<blockquote>a\nb\nc</blockquote>"
        assert _balanced(out)

    def test_quotes_split_by_normal_line_stay_separate(self) -> None:
        out = markdown_to_telegram_html("> a\ntexto\n> b")
        assert out == "<blockquote>a</blockquote>\ntexto\n<blockquote>b</blockquote>"
        assert _balanced(out)

    def test_expandable_quote(self) -> None:
        # ">>>" maps to Telegram's collapsible blockquote.
        out = markdown_to_telegram_html(">>> oculto")
        assert out == "<blockquote expandable>oculto</blockquote>"
        assert _balanced(out)

    def test_multiline_expandable_quote_merges(self) -> None:
        out = markdown_to_telegram_html(">>> a\n>>> b\n>>> c")
        assert out == "<blockquote expandable>a\nb\nc</blockquote>"
        assert _balanced(out)

    def test_expandable_and_normal_quote_do_not_merge(self) -> None:
        # Different quote kinds are distinct blocks even when adjacent.
        out = markdown_to_telegram_html(">>> a\n> b")
        assert out == "<blockquote expandable>a</blockquote>\n<blockquote>b</blockquote>"
        assert _balanced(out)

    def test_expandable_quote_keeps_emphasis(self) -> None:
        out = markdown_to_telegram_html(">>> **bold** y `code`")
        assert out == "<blockquote expandable><b>bold</b> y <code>code</code></blockquote>"
        assert _balanced(out)

    def test_fenced_code_with_lang(self) -> None:
        out = markdown_to_telegram_html("```python\nx = 1 < 2\n```")
        assert out == '<pre><code class="language-python">x = 1 &lt; 2</code></pre>'

    def test_fenced_code_no_lang(self) -> None:
        out = markdown_to_telegram_html("```\nplain\n```")
        assert out == "<pre>plain</pre>"

    def test_fence_protects_markdown(self) -> None:
        out = markdown_to_telegram_html("```\n**no** _italic_\n```")
        assert out == "<pre>**no** _italic_</pre>"


class TestInvariants:
    def test_output_always_balanced(self) -> None:
        samples = [
            "**fuerte** y *suave* y `code`",
            "## H\n- a\n- b\n> q",
            "```py\nif a<b and c>d: pass\n```",
            "[l](https://x.com) **b** _i_ ~~s~~",
            "raw < > & no markdown",
            "incompleto **sin cierre",
        ]
        for s in samples:
            assert _balanced(markdown_to_telegram_html(s)), s

    def test_unbalanced_markdown_does_not_crash(self) -> None:
        # A stray ** must not produce an unbalanced <b>.
        out = markdown_to_telegram_html("texto con ** suelto")
        assert _balanced(out)


class TestStripToPlain:
    def test_strips_formatting(self) -> None:
        out = strip_to_plain("**b** _i_ `c` ~~s~~")
        assert out == "b i c s"

    def test_link_becomes_text_and_url(self) -> None:
        assert strip_to_plain("[t](https://x.com)") == "t (https://x.com)"

    def test_fence_becomes_body(self) -> None:
        assert strip_to_plain("```py\ncode\n```") == "code"

    def test_no_html_tags(self) -> None:
        assert "<" not in strip_to_plain("**b** y `c`")
