"""CLI: publish Instagram media over the CDP Chrome session.

Usage:
    python -m claw_v2.cli.instagram_publish <media_path> --caption "..." \\
        [--media-type reel|photo] \\
        [--account pachanodesign]

The caption may also be read from a file with --caption-file. Verification is
done in-flow via Instagram's share-confirmation modal; photo posts also verify
the profile changed. Exit code 0 only when the media is verified as shared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from claw_v2.instagram_publish import InstagramPublishService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media_path", help="Absolute path to the media to publish")
    parser.add_argument(
        "--media-type",
        choices=("reel", "photo"),
        default="reel",
        help="Publish flow to use. Defaults to reel for backward compatibility.",
    )
    parser.add_argument("--caption", default="", help="Reel caption text")
    parser.add_argument("--caption-file", default=None, help="Read caption from a file")
    parser.add_argument(
        "--account",
        default=None,
        help="Expected logged-in account handle (guard against wrong account)",
    )
    args = parser.parse_args(argv)

    caption = args.caption
    if args.caption_file:
        caption = Path(args.caption_file).read_text(encoding="utf-8")

    svc = InstagramPublishService()
    if args.media_type == "photo":
        result = svc.publish_photo(
            photo_path=args.media_path,
            caption=caption,
            expected_account=args.account,
        )
    else:
        result = svc.publish_reel(
            video_path=args.media_path,
            caption=caption,
            expected_account=args.account,
        )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
