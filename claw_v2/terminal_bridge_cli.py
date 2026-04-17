from __future__ import annotations

import argparse
import json
from pathlib import Path

from claw_v2.terminal_bridge import TerminalBridgeError, TerminalBridgeService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage persistent PTY bridge sessions for claude/codex.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    open_parser = subparsers.add_parser("open")
    open_parser.add_argument("tool", choices=["claude", "codex"])
    open_parser.add_argument("--cwd", default=None)
    open_parser.add_argument("--arg", dest="args", action="append", default=[])

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("session_id")
    send_parser.add_argument("text")
    send_parser.add_argument("--raw", action="store_true")

    read_parser = subparsers.add_parser("read")
    read_parser.add_argument("session_id")
    read_parser.add_argument("--offset", type=int, default=0)
    read_parser.add_argument("--limit", type=int, default=4000)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("session_id")

    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("session_id")
    close_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("list")

    args = parser.parse_args(argv)
    service = TerminalBridgeService()

    try:
        if args.command == "open":
            result = service.open(args.tool, cwd=args.cwd, args=list(args.args))
        elif args.command == "send":
            result = service.send(args.session_id, args.text, append_newline=not args.raw)
        elif args.command == "read":
            result = service.read(args.session_id, offset=args.offset, limit=args.limit)
        elif args.command == "status":
            result = service.status(args.session_id)
        elif args.command == "close":
            result = service.close(args.session_id, force=args.force)
        else:
            result = {"sessions": service.list_sessions()}
    except (TerminalBridgeError, FileNotFoundError) as exc:
        parser.exit(status=1, message=f"{exc}\n")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
