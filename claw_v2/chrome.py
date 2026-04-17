# claw_v2/chrome.py
from __future__ import annotations

import logging
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

    @property
    def cdp_url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self, *, headless: bool = True) -> None:
        """Kill any Chrome on our port, launch fresh."""
        pids = _check_port_pids(self.port)
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
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("ManagedChrome stopped")

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


def _wait_for_cdp_ready(port: int, timeout: float = 10) -> None:
    import urllib.request
    url = f"http://localhost:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise ChromeStartError(f"Chrome CDP not responding on port {port} after {timeout}s")
