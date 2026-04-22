from __future__ import annotations

import subprocess
from pathlib import Path

from claw_v2.process_cli import main as process_cli_main
from claw_v2.process_manager import (
    DockerProcessManager,
    LaunchdProcessManager,
    ProcessSpec,
    SystemdProcessManager,
    detect_process_manager,
)


def _runner(calls: list[list[str]]):
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    return run


def test_launchd_manager_plans_restart_and_renders_plist(tmp_path: Path) -> None:
    spec = ProcessSpec(repo_root=tmp_path)
    manager = LaunchdProcessManager(spec=spec, label="com.test.claw", domain="gui/501")

    assert manager.plan("restart") == ["launchctl", "kickstart", "-k", "gui/501/com.test.claw"]
    definition = manager.render_definition()
    assert "com.test.claw" in definition
    assert str(tmp_path / "ops" / "claw-launcher.sh") in definition


def test_systemd_manager_uses_user_scope_and_renders_unit(tmp_path: Path) -> None:
    spec = ProcessSpec(repo_root=tmp_path, env_file=tmp_path / "env")
    manager = SystemdProcessManager(spec=spec, service_name="claw.service", user=True)

    assert manager.plan("start") == ["systemctl", "--user", "start", "claw.service"]
    assert manager.plan("install") == ["systemctl", "--user", "enable", "--now", "claw.service"]
    definition = manager.render_definition()
    assert f"WorkingDirectory={tmp_path}" in definition
    assert f"EnvironmentFile=-{tmp_path / 'env'}" in definition


def test_docker_manager_plans_container_lifecycle(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("KEY=value\n", encoding="utf-8")
    spec = ProcessSpec(
        repo_root=tmp_path,
        env_file=env_file,
        docker_image="ghcr.io/hecpac/claw-core:test",
        container_name="claw-test",
    )
    manager = DockerProcessManager(spec=spec)

    install = manager.plan("install")
    assert install[:6] == ["docker", "run", "-d", "--name", "claw-test", "--restart"]
    assert ["--env-file", str(env_file)] == install[7:9]
    assert manager.plan("status") == ["docker", "inspect", "-f", "{{.State.Status}}", "claw-test"]
    assert "container_name: claw-test" in manager.render_definition()


def test_run_uses_injected_runner_without_shell(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    manager = SystemdProcessManager(spec=ProcessSpec(repo_root=tmp_path), runner=_runner(calls))

    result = manager.run("status")

    assert result.ok
    assert result.stdout == "ok\n"
    assert calls == [["systemctl", "--user", "status", "claw.service"]]


def test_detect_process_manager_selects_expected_backend(tmp_path: Path) -> None:
    spec = ProcessSpec(repo_root=tmp_path)

    assert detect_process_manager(spec=spec, system="Darwin", containerized=False).backend == "launchd"
    assert detect_process_manager(spec=spec, system="Linux", containerized=False).backend == "systemd"
    assert detect_process_manager(spec=spec, system="Linux", containerized=True).backend == "docker"


def test_process_cli_prints_definition(capsys, tmp_path: Path) -> None:
    result = process_cli_main(["definition", "--backend", "systemd", "--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert "[Service]" in captured.out
    assert str(tmp_path) in captured.out


def test_process_cli_prints_selected_plan(capsys, tmp_path: Path) -> None:
    result = process_cli_main([
        "plan",
        "--backend",
        "systemd",
        "--repo-root",
        str(tmp_path),
        "--plan-action",
        "restart",
    ])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.strip() == "systemctl --user restart claw.service"
