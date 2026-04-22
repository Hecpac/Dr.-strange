from __future__ import annotations

import argparse
from pathlib import Path

from claw_v2.process_manager import (
    DockerProcessManager,
    LaunchdProcessManager,
    ProcessSpec,
    SystemdProcessManager,
    detect_process_manager,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Claw process wrapper.")
    parser.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status", "definition", "plan"])
    parser.add_argument("--plan-action", choices=["install", "uninstall", "start", "stop", "restart", "status"], default="status")
    parser.add_argument("--backend", choices=["auto", "launchd", "systemd", "docker"], default="auto")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--service-name", default="claw.service")
    parser.add_argument("--label", default="com.pachano.claw")
    parser.add_argument("--docker-image", default="claw-core:latest")
    parser.add_argument("--container-name", default="claw-core")
    parser.add_argument("--system", action="store_true", help="Use system-level systemd instead of --user.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = ProcessSpec(
        repo_root=args.repo_root,
        docker_image=args.docker_image,
        container_name=args.container_name,
    )
    manager = _manager(args.backend, spec, label=args.label, service_name=args.service_name, system=bool(args.system))
    if args.action == "definition":
        print(manager.render_definition(), end="")
        return 0
    if args.action == "plan":
        print(" ".join(manager.plan(args.plan_action)))
        return 0
    result = manager.run(args.action)  # type: ignore[arg-type]
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    return result.returncode


def _manager(backend: str, spec: ProcessSpec, *, label: str, service_name: str, system: bool):
    if backend == "launchd":
        return LaunchdProcessManager(spec=spec, label=label)
    if backend == "systemd":
        return SystemdProcessManager(spec=spec, service_name=service_name, user=not system)
    if backend == "docker":
        return DockerProcessManager(spec=spec)
    return detect_process_manager(spec=spec)


if __name__ == "__main__":
    raise SystemExit(main())
