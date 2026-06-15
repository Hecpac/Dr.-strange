"""CLI: poll a HeyGen render, download, compress, and deliver to Telegram.

Usage:
    python -m claw_v2.cli.heygen_deliver <video_id> [--caption "..."] [--slug "name"]
    python -m claw_v2.cli.heygen_deliver --latest [--caption "..."]

The --latest flag picks the most recent video from /v1/video.list.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from claw_v2.heygen_delivery import HeygenDeliveryService, _heygen_api_key


def _latest_video_id() -> str:
    api_key = _heygen_api_key()
    url = "https://api.heygen.com/v1/video.list?limit=1"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    videos = (payload.get("data") or {}).get("videos") or []
    if not videos:
        raise SystemExit("no_videos_in_account")
    return videos[0]["video_id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", nargs="?", help="HeyGen video_id to deliver")
    parser.add_argument(
        "--latest", action="store_true", help="Pick the most recent video from the account"
    )
    parser.add_argument("--caption", default=None, help="Telegram caption")
    parser.add_argument("--slug", default=None, help="Filename slug for the saved mp4")
    parser.add_argument("--chat-id", default=None, help="Override target chat_id")
    args = parser.parse_args(argv)

    if args.latest:
        video_id = _latest_video_id()
    elif args.video_id:
        video_id = args.video_id
    else:
        parser.error("provide a video_id or pass --latest")
        return 2

    svc = HeygenDeliveryService()
    result = svc.auto_deliver(
        video_id=video_id,
        caption=args.caption,
        chat_id=args.chat_id,
        slug=args.slug,
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
