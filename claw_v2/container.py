from __future__ import annotations

import logging
import resource
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ContainerPolicy:
    cpu_seconds: int = 120
    memory_mb: int = 512
    max_processes: int = 64
    timeout_seconds: int = 300
    network_enabled: bool = True
    docker_image: str | None = None


def sandboxed_run(
    command: str | list[str],
    *,
    cwd: str | Path,
    policy: ContainerPolicy | None = None,
    shell: bool = True,
) -> subprocess.CompletedProcess:
    if policy is None:
        policy = ContainerPolicy()
    if policy.docker_image:
        return _docker_run(command, cwd=cwd, policy=policy, shell=shell)
    return _limited_run(command, cwd=cwd, policy=policy, shell=shell)


def _set_limits(policy: ContainerPolicy):
    def _apply():
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds))
        except (ValueError, OSError):
            pass
        try:
            mem_bytes = policy.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_processes, policy.max_processes))
        except (ValueError, OSError):
            pass
    return _apply


def _limited_run(
    command: str | list[str],
    *,
    cwd: str | Path,
    policy: ContainerPolicy,
    shell: bool,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=shell,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=policy.timeout_seconds,
        preexec_fn=_set_limits(policy),
    )


def _docker_run(
    command: str | list[str],
    *,
    cwd: str | Path,
    policy: ContainerPolicy,
    shell: bool = True,
) -> subprocess.CompletedProcess:
    cwd_str = str(Path(cwd).resolve())
    cmd_str = command if isinstance(command, str) else " ".join(command)
    docker_args = [
        "docker", "run", "--rm",
        f"--cpus={policy.cpu_seconds}",
        f"--memory={policy.memory_mb}m",
        f"--pids-limit={policy.max_processes}",
        "--read-only",
        "-v", f"{cwd_str}:{cwd_str}",
        "-w", cwd_str,
    ]
    if not policy.network_enabled:
        docker_args.append("--network=none")
    docker_args.extend([policy.docker_image, "/bin/sh", "-c", cmd_str])
    return subprocess.run(
        docker_args,
        capture_output=True,
        text=True,
        check=False,
        timeout=policy.timeout_seconds,
    )
