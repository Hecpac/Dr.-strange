# Deployment

PR#6 introduces a process manager layer for running Claw with the same lifecycle
surface on macOS, Linux/systemd, and Docker.

The managers are intentionally thin wrappers over platform-native supervisors.
They generate commands and service definitions, and all command execution goes
through an injectable runner for tests and future safety checks.

## CLI

Show the detected supervisor status:

```bash
python -m claw_v2.process_cli status --backend auto --repo-root /path/to/repo
```

Render a service definition:

```bash
python -m claw_v2.process_cli definition --backend systemd --repo-root /path/to/repo
python -m claw_v2.process_cli definition --backend launchd --repo-root /path/to/repo
python -m claw_v2.process_cli definition --backend docker --repo-root /path/to/repo
```

Inspect the command that would be executed:

```bash
python -m claw_v2.process_cli plan --backend systemd --plan-action restart --repo-root /path/to/repo
```

Supported actions:

- `install`
- `uninstall`
- `start`
- `stop`
- `restart`
- `status`
- `definition`
- `plan`

## Backends

`LaunchdProcessManager` manages macOS user agents with `launchctl`.

`SystemdProcessManager` manages Linux services with `systemctl --user` by
default. Pass `--system` for system-level units on VPS/Pi deployments.

`DockerProcessManager` manages a named container with `docker run/start/stop`.
It uses `--restart unless-stopped` so the container survives restarts.

## Notes

The existing `ops/claw-launcher.sh` and launchd plists remain valid. This PR adds
the common abstraction needed before splitting Claw-Core onto a VPS and leaving
Computer Use on a Mac edge node.
