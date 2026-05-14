# claw_v2/chrome.py
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CHROME_NAMES = ("google chrome", "chrome", "chromium")


class ChromeStartError(RuntimeError):
    """Raised when ManagedChrome cannot start."""


class ManagedChrome:
    """Auto-managed Chrome process with CDP for the bot."""

    def __init__(self, port: int = 9250, profile_dir: str = "~/.claw/chrome-profile") -> None:
        self.port = port
        self.profile_dir = str(Path(profile_dir).expanduser())
        self._process: subprocess.Popen | None = None
        self._attached_pid: int | None = None

    @property
    def cdp_url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self, *, headless: bool = True) -> None:
        """Start or attach to a Chrome CDP process without corrupting profiles."""
        pids = _check_port_pids(self.port)
        if pids and _is_cdp_ready(self.port, timeout=1):
            if all(any(cn in name.lower() for cn in _CHROME_NAMES) for _, name in pids):
                self._process = None
                self._attached_pid = pids[0][0]
                logger.info("ManagedChrome reusing existing CDP Chrome on port %d (PID %d)", self.port, self._attached_pid)
                return
        for pid, name in pids:
            if any(cn in name.lower() for cn in _CHROME_NAMES):
                logger.info("Killing stale Chrome (PID %d) on port %d", pid, self.port)
                _kill_pid(pid)
            else:
                raise ChromeStartError(
                    f"Port {self.port} occupied by '{name}' (PID {pid}). "
                    f"Set CLAW_CHROME_PORT to use a different port."
                )
        if pids:
            _wait_for_port_free(self.port, timeout=5)

        chrome_path = _find_chrome()
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        _wait_for_profile_free(self.profile_dir, timeout=5)
        profile_pids = _profile_user_data_pids(self.profile_dir)
        if profile_pids:
            joined_pids = ", ".join(str(pid) for pid in profile_pids)
            raise ChromeStartError(
                f"Chrome profile {self.profile_dir} is already in use by PID(s) {joined_pids}, "
                f"but CDP is not ready on port {self.port}. Close that Chrome or use a different "
                "CLAW_CHROME_PORT/profile before starting ManagedChrome."
            )
        _remove_stale_singleton_lock(self.profile_dir)

        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--disable-default-apps",
        ]
        if headless:
            cmd.append("--headless=new")

        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_for_cdp_ready(self.port, timeout=10)
        logger.info("ManagedChrome started on port %d (PID %d)", self.port, self._process.pid)

    def stop(self) -> None:
        """Stop Chrome. Kills the process whether it was launched or attached — that is
        the contract /chrome_login needs to transition from headless to visible."""
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("ManagedChrome stopped")
        elif self._attached_pid is not None:
            import os, signal
            pid = self._attached_pid
            try:
                os.kill(pid, signal.SIGTERM)
                _wait_for_port_free(self.port, timeout=5)
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            self._attached_pid = None
            logger.info("ManagedChrome detached and killed attached Chrome PID %d", pid)

    def ensure(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.start()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


def _find_chrome() -> str:
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise ChromeStartError("Chrome not found. Install Google Chrome.")


def _check_port_pids(port: int) -> list[tuple[int, str]]:
    try:
        output = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    if not output:
        return []
    results = []
    for line in output.splitlines():
        pid = int(line.strip())
        try:
            name = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "comm="], text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            name = "unknown"
        results.append((pid, name))
    return results


def _profile_user_data_pids(profile_dir: str) -> list[int]:
    target = str(Path(profile_dir).expanduser().resolve(strict=False))
    try:
        output = subprocess.check_output(
            ["ps", "-axww", "-o", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    pids: list[int] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if not pid_text.isdigit():
            continue
        lowered = command.lower()
        if not any(cn in lowered for cn in _CHROME_NAMES):
            continue
        for candidate in _extract_user_data_dirs(command):
            if _same_profile_path(candidate, target):
                pids.append(int(pid_text))
                break
    return pids


def _extract_user_data_dirs(command: str) -> list[str]:
    dirs: list[str] = []
    patterns = (
        r"--user-data-dir=(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
        r"--user-data-dir\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, command):
            value = next(group for group in match.groups() if group)
            dirs.append(value)
    return dirs


def _same_profile_path(candidate: str, target: str) -> bool:
    try:
        resolved = str(Path(candidate).expanduser().resolve(strict=False))
    except OSError:
        return False
    return resolved == target


def _remove_stale_singleton_lock(profile_dir: str) -> None:
    lock_path = Path(profile_dir) / "SingletonLock"
    try:
        if lock_path.exists() or lock_path.is_symlink():
            lock_path.unlink()
            logger.info("Removed stale Chrome SingletonLock at %s", lock_path)
    except OSError as exc:
        raise ChromeStartError(f"Could not remove stale Chrome lock at {lock_path}: {exc}") from exc


def _kill_pid(pid: int) -> None:
    import signal
    import os
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _wait_for_port_free(port: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _check_port_pids(port):
            return
        time.sleep(0.5)
    logger.warning("Port %d not free after %ds, proceeding anyway", port, timeout)


def _wait_for_profile_free(profile_dir: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _profile_user_data_pids(profile_dir):
            return
        time.sleep(0.5)


def _is_cdp_ready(port: int, timeout: float = 2) -> bool:
    import urllib.request
    url = f"http://localhost:{port}/json/version"
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _wait_for_cdp_ready(port: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_cdp_ready(port, timeout=2):
            return
        time.sleep(0.5)
    raise ChromeStartError(f"Chrome CDP not responding on port {port} after {timeout}s")
