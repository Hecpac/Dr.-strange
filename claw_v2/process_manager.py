from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol


ProcessAction = Literal["install", "uninstall", "start", "stop", "restart", "status"]
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class ProcessManager(Protocol):
    backend: str

    def plan(self, action: ProcessAction) -> list[str]:
        ...

    def run(self, action: ProcessAction) -> "ProcessCommandResult":
        ...

    def render_definition(self) -> str:
        ...


@dataclass(slots=True)
class ProcessCommandResult:
    backend: str
    action: str
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(slots=True)
class ProcessSpec:
    name: str = "claw"
    repo_root: Path = field(default_factory=Path.cwd)
    launcher_path: Path | None = None
    env_file: Path | None = None
    log_dir: Path | None = None
    python_module: str = "claw_v2.main"
    docker_image: str = "claw-core:latest"
    container_name: str = "claw-core"

    def launcher(self) -> Path:
        return self.launcher_path or self.repo_root / "ops" / "claw-launcher.sh"

    def env(self) -> Path:
        return self.env_file or Path.home() / ".claw" / "env"

    def logs(self) -> Path:
        return self.log_dir or Path.home() / ".claw"


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


@dataclass(slots=True)
class LaunchdProcessManager:
    spec: ProcessSpec
    label: str = "com.pachano.claw"
    domain: str | None = None
    plist_path: Path | None = None
    runner: Runner = default_runner
    backend: str = "launchd"

    def plan(self, action: ProcessAction) -> list[str]:
        target = f"{self._domain()}/{self.label}"
        if action == "install":
            return ["launchctl", "bootstrap", self._domain(), str(self._plist())]
        if action == "uninstall":
            return ["launchctl", "bootout", self._domain(), str(self._plist())]
        if action == "start":
            return ["launchctl", "kickstart", target]
        if action == "stop":
            return ["launchctl", "kill", "TERM", target]
        if action == "restart":
            return ["launchctl", "kickstart", "-k", target]
        return ["launchctl", "print", target]

    def run(self, action: ProcessAction) -> ProcessCommandResult:
        return _run(self.backend, action, self.plan(action), self.runner)

    def render_definition(self) -> str:
        out = self.spec.logs() / f"{self.spec.name}.stdout.log"
        err = self.spec.logs() / f"{self.spec.name}.stderr.log"
        return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\"
  \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key><string>{self.label}</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>{self.spec.launcher()}</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{out}</string>
    <key>StandardErrorPath</key><string>{err}</string>
</dict>
</plist>
"""

    def _domain(self) -> str:
        return self.domain or f"gui/{os.getuid()}"

    def _plist(self) -> Path:
        return self.plist_path or self.spec.repo_root / "ops" / f"{self.label}.plist"


@dataclass(slots=True)
class SystemdProcessManager:
    spec: ProcessSpec
    service_name: str = "claw.service"
    user: bool = True
    runner: Runner = default_runner
    backend: str = "systemd"

    def plan(self, action: ProcessAction) -> list[str]:
        base = ["systemctl", "--user"] if self.user else ["systemctl"]
        if action == "install":
            return [*base, "enable", "--now", self.service_name]
        if action == "uninstall":
            return [*base, "disable", "--now", self.service_name]
        return [*base, action, self.service_name]

    def run(self, action: ProcessAction) -> ProcessCommandResult:
        return _run(self.backend, action, self.plan(action), self.runner)

    def render_definition(self) -> str:
        env_line = f"EnvironmentFile=-{self.spec.env()}\n" if self.spec.env() else ""
        return f"""[Unit]
Description=Claw autonomous agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory={self.spec.repo_root}
{env_line}ExecStart={self.spec.repo_root}/.venv/bin/python -m {self.spec.python_module}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


@dataclass(slots=True)
class DockerProcessManager:
    spec: ProcessSpec
    runner: Runner = default_runner
    backend: str = "docker"

    def plan(self, action: ProcessAction) -> list[str]:
        name = self.spec.container_name
        if action == "install":
            command = ["docker", "run", "-d", "--name", name, "--restart", "unless-stopped"]
            if self.spec.env().exists():
                command.extend(["--env-file", str(self.spec.env())])
            command.extend(["-v", f"{self.spec.repo_root}:/app", "-w", "/app", self.spec.docker_image])
            return command
        if action == "uninstall":
            return ["docker", "rm", "-f", name]
        if action == "status":
            return ["docker", "inspect", "-f", "{{.State.Status}}", name]
        return ["docker", action, name]

    def run(self, action: ProcessAction) -> ProcessCommandResult:
        return _run(self.backend, action, self.plan(action), self.runner)

    def render_definition(self) -> str:
        return f"""services:
  claw-core:
    image: {self.spec.docker_image}
    container_name: {self.spec.container_name}
    restart: unless-stopped
    working_dir: /app
    volumes:
      - {self.spec.repo_root}:/app
    env_file:
      - {self.spec.env()}
    command: [\".venv/bin/python\", \"-m\", \"{self.spec.python_module}\"]
"""


def detect_process_manager(
    *,
    spec: ProcessSpec | None = None,
    system: str | None = None,
    containerized: bool | None = None,
    runner: Runner = default_runner,
) -> ProcessManager:
    spec = spec or ProcessSpec()
    if containerized is None:
        containerized = Path("/.dockerenv").exists()
    if containerized:
        return DockerProcessManager(spec=spec, runner=runner)
    system = system or platform.system()
    if system == "Darwin":
        return LaunchdProcessManager(spec=spec, runner=runner)
    return SystemdProcessManager(spec=spec, runner=runner)


def _run(backend: str, action: str, command: list[str], runner: Runner) -> ProcessCommandResult:
    completed = runner(command)
    return ProcessCommandResult(
        backend=backend,
        action=action,
        command=command,
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
