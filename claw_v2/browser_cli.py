from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from claw_v2.browser import BrowserError, DevBrowserService


def _load_payload(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("browser payload must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run structured browser interactions via dev-browser.")
    parser.add_argument("payload", nargs="?", help="JSON payload; when omitted, read from stdin")
    parser.add_argument("--browser", dest="browser_name", default="default")
    parser.add_argument("--headed", action="store_true", help="Disable headless mode")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)

    raw_payload = args.payload if args.payload is not None else sys.stdin.read()
    if not raw_payload.strip():
        print("browser payload is required", file=sys.stderr)
        return 2

    try:
        payload = _load_payload(raw_payload)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid browser payload: {exc}", file=sys.stderr)
        return 2

    service = DevBrowserService(timeout=args.timeout, headless=not args.headed)
    try:
        result = service.interact(
            payload.get("url"),
            actions=list(payload.get("actions") or []),
            page_name=str(payload.get("page_name") or "main"),
            browser_name=args.browser_name,
        )
    except BrowserError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({
        "url": result.url,
        "title": result.title,
        "content": result.content,
        "screenshot_path": result.screenshot_path,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
