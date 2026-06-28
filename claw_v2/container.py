from __future__ import annotations

import logging
import re
import resource
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claw_v2.runtime_policy import SanitizedChildEnv, sanitize_child_env

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ContainerPolicy:
    cpu_seconds: int = 120
    cpu_count: float = 2.0
    memory_mb: int = 512
    max_processes: int = 64
    timeout_seconds: int = 300
    network_enabled: bool = False
    docker_image: str | None = None
    isolation_mode: str = "host_sanitized"


_CDP_COMMAND_RE = re.compile(
    r"(:9250\b|:9222\b|webSocketDebuggerUrl|/json/(?:list|version)\b)",
    re.IGNORECASE,
)


def _looks_like_cdp_command(command: str | list[str]) -> bool:
    """True when a command drives Chrome DevTools Protocol (CDP) on the host.

    Browser/CDP work must run in the SDK worker lane, not ``sandboxed_run``:
    a ``docker_ephemeral`` container runs ``--network=none`` by default and
    its ``localhost`` is not the host's loopback, so a CDP connection from
    inside the container cannot reach the host's Chrome on :9250/:9222.
    """
    text = command if isinstance(command, str) else " ".join(str(c) for c in command)
    return bool(_CDP_COMMAND_RE.search(text))


def sandboxed_run(
    command: str | list[str],
    *,
    cwd: str | Path,
    policy: ContainerPolicy | None = None,
    shell: bool = True,
    observe: object | None = None,
) -> subprocess.CompletedProcess:
    if policy is None:
        policy = ContainerPolicy()
    mode = policy.isolation_mode.strip().lower()
    if mode not in {"host_sanitized", "docker_ephemeral"}:
        raise ValueError("isolation_mode must be one of: host_sanitized, docker_ephemeral")
    env_result = sanitize_child_env()
    use_docker = bool(policy.docker_image) or mode == "docker_ephemeral"
    if use_docker and not docker_available():
        # no_silent_degrade: docker isolation was requested but docker is not
        # available. Degrade to host_sanitized rather than crashing the caller,
        # and make the downgrade observable.
        if observe is not None:
            try:
                observe.emit(
                    "runtime_isolation_degraded",
                    payload={
                        "requested_mode": mode,
                        "effective_mode": "host_sanitized",
                        "reason": "docker_unavailable",
                    },
                )
            except Exception:
                logger.debug("runtime isolation degrade observe emit failed", exc_info=True)
        use_docker = False
        mode = "host_sanitized"
    # Browser/CDP work (Chrome DevTools on :9250/:9222, webSocketDebuggerUrl)
    # must run in the SDK worker lane, not sandboxed_run: a docker_ephemeral
    # container runs --network=none by default and its localhost is not the
    # host's loopback, so CDP connections silently fail. Emit an observe event
    # when a CDP-shaped command is routed here so the latent failure is visible
    # instead of a bare connection-refused.
    if use_docker and not policy.network_enabled and _looks_like_cdp_command(command):
        if observe is not None:
            try:
                observe.emit(
                    "runtime_cdp_command_routed_to_networkless_container",
                    payload={
                        "requested_mode": mode,
                        "effective_runner": "docker",
                        "network_enabled": False,
                        "reason": "cdp_requires_host_network",
                    },
                )
            except Exception:
                logger.debug("cdp networkless container observe emit failed", exc_info=True)
    if use_docker:
        result = _docker_run(command, cwd=cwd, policy=policy, shell=shell, env_result=env_result)
    else:
        result = _limited_run(command, cwd=cwd, policy=policy, shell=shell, env_result=env_result)
    _emit_env_event(observe, policy=policy, mode=mode, env_result=env_result)
    return result


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
    env_result: SanitizedChildEnv | None = None,
) -> subprocess.CompletedProcess:
    env_result = env_result or sanitize_child_env()
    return subprocess.run(
        command,
        shell=shell,
        cwd=str(cwd),
        env=env_result.env,
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
    env_result: SanitizedChildEnv | None = None,
) -> subprocess.CompletedProcess:
    cwd_str = str(Path(cwd).resolve())
    cmd_str = (
        command if isinstance(command, str) else " ".join(shlex.quote(item) for item in command)
    )
    docker_image = policy.docker_image or "python:3.12-slim"
    env_result = env_result or sanitize_child_env()
    docker_args = [
        "docker",
        "run",
        "--rm",
        f"--cpus={policy.cpu_count}",
        f"--memory={policy.memory_mb}m",
        f"--pids-limit={policy.max_processes}",
        "--read-only",
        "-v",
        f"{cwd_str}:{cwd_str}",
        "-w",
        cwd_str,
    ]
    if not policy.network_enabled:
        docker_args.append("--network=none")
    for key, value in sorted(env_result.env.items()):
        docker_args.extend(["--env", f"{key}={value}"])
    docker_args.extend([docker_image, "/bin/sh", "-c", cmd_str])
    return subprocess.run(
        docker_args,
        env=env_result.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=policy.timeout_seconds,
    )


def docker_available() -> bool:
    # P2: a present binary is not enough — the daemon must respond. shutil.which
    # alone returned True even when `docker run` would fail with "Cannot connect
    # to the Docker daemon". Probe with a short-timeout `docker info`.
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _emit_env_event(
    observe: object | None,
    *,
    policy: ContainerPolicy,
    mode: str,
    env_result: SanitizedChildEnv,
) -> None:
    if observe is None:
        return
    try:
        observe.emit(
            "runtime_child_env_sanitized",
            payload={
                **env_result.to_metadata(),
                "runner": "container.sandboxed_run",
                "isolation_mode": mode,
                "network_enabled": bool(policy.network_enabled),
            },
        )
    except Exception:
        logger.debug("runtime child env observe emit failed", exc_info=True)
