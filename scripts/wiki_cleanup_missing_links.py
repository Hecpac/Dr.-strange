#!/usr/bin/env python3
"""Cleanup broken [[wikilinks]] in the Claw wiki.

For each wiki page, every [[slug|display]] or [[slug]] reference whose target
does not exist becomes plain text (the display alias if present, else the
original inner text). Valid links are preserved untouched.

Run with --dry-run to preview, --apply to mutate. Always backs up to
~/.claw/wiki/wiki.backup-<timestamp> on --apply.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

WIKI_DIR = Path.home() / ".claw" / "wiki" / "wiki"
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")[:80]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write changes")
    args = parser.parse_args()

    if not WIKI_DIR.exists():
        print(f"wiki dir not found: {WIKI_DIR}", file=sys.stderr)
        return 1

    pages = sorted(WIKI_DIR.glob("*.md"))
    all_slugs = {p.stem for p in pages}

    pages_modified = 0
    links_cleaned = 0
    sample: list[tuple[str, str]] = []

    def _replace(match: re.Match) -> str:
        nonlocal links_cleaned
        inner = match.group(1)
        # Wiki convention: [[Display Text|slug]] — display first, slug second.
        if "|" in inner:
            display, slug_part = inner.split("|", 1)
        else:
            display = slug_part = inner
        # Try the explicit slug, then the slugified display, then the slugified whole inner.
        for candidate in (slug_part.strip(), slugify(slug_part), slugify(display), slugify(inner)):
            if candidate in all_slugs:
                return match.group(0)
        links_cleaned += 1
        if len(sample) < 10:
            sample.append((match.group(0), display.strip()))
        return display.strip()

    if args.apply:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = WIKI_DIR.parent / f"wiki.backup-{ts}"
        print(f"Backing up to {backup_dir}")
        shutil.copytree(WIKI_DIR, backup_dir)

    for page in pages:
        text = page.read_text(encoding="utf-8")
        new_text = WIKILINK_RE.sub(_replace, text)
        if new_text != text:
            pages_modified += 1
            if args.apply:
                page.write_text(new_text, encoding="utf-8")

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"\n=== {mode} ===")
    print(f"pages scanned:  {len(pages)}")
    print(f"pages modified: {pages_modified}")
    print(f"links cleaned:  {links_cleaned}")
    if sample:
        print("\nfirst 10 broken links removed:")
        for original, replacement in sample:
            print(f"  {original!r} -> {replacement!r}")
    if not args.apply:
        print("\n(no files written; rerun with --apply to commit changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
