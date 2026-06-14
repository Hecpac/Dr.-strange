# claw_v2/chrome.py
from __future__ import annotations

import logging
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_CHROME_NAMES = ("google chrome", "chrome", "chromium")


class ChromeStartError(RuntimeError):
    """Raised when ManagedChrome cannot start."""


class ManagedChrome:
    """Auto-managed Chrome process with CDP for the bot."""

    def __init__(
        self,
        port: int = 9250,
        profile_dir: str = "~/.claw/chrome-profile",
        *,
        observe: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.port = port
        self.profile_dir = str(Path(profile_dir).expanduser().resolve(strict=False))
        self._process: subprocess.Popen | None = None
        self._attached_pid: int | None = None
        # P0 hotfix C: callback for cdp_unavailable observability events.
        # Optional so unit tests can omit it.
        self._observe = observe

    @property
    def cdp_url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self, *, headless: bool = False) -> None:
        """Start or attach to a Chrome CDP process without corrupting profiles."""
        pids = _check_port_pids(self.port)
        profile_pids = set(_profile_user_data_pids(self.profile_dir)) if pids else set()
        if pids and _is_cdp_ready(self.port, timeout=1):
            if all(any(cn in name.lower() for cn in _CHROME_NAMES) for _, name in pids):
                matching_profile_pids = [
                    pid for pid, _name in pids if pid in profile_pids
                ]
                if not matching_profile_pids:
                    raise ChromeStartError(
                        f"Port {self.port} has a ready Chrome CDP process, but it is "
                        f"not using managed profile {self.profile_dir} (different profile). "
                        f"Stop that Chrome or set CLAW_CHROME_PORT to a different port."
                    )
                if not headless and any(_pid_is_headless(pid) for pid in matching_profile_pids):
                    logger.info(
                        "Ready CDP Chrome on port %d is headless; relaunching visible",
                        self.port,
                    )
                    for pid in matching_profile_pids:
                        _kill_pid(pid)
                    _wait_for_port_free(self.port, timeout=5)
                else:
                    self._process = None
                    self._attached_pid = matching_profile_pids[0]
                    if not headless:
                        self._ensure_visible_window(self._attached_pid)
                    logger.info("ManagedChrome reusing existing CDP Chrome on port %d (PID %d)", self.port, self._attached_pid)
                    return
        for pid, name in pids:
            if any(cn in name.lower() for cn in _CHROME_NAMES):
                if pid not in profile_pids:
                    raise ChromeStartError(
                        f"Port {self.port} occupied by Chrome PID {pid}, but it is "
                        f"not using managed profile {self.profile_dir} (different profile). "
                        f"Stop that Chrome or set CLAW_CHROME_PORT to use a different port."
                    )
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

        # P0 hotfix C: kill stale PIDs holding *our* profile, retry once,
        # then degrade with cdp_unavailable. Replaces the prior raise-on-first-
        # conflict + "Port not free, proceeding anyway" silent fall-through.
        last_error: ChromeStartError | None = None
        for attempt in range(2):
            self._reclaim_profile_if_busy()
            _remove_stale_singleton_lock(self.profile_dir)
            try:
                self._spawn_chrome(chrome_path, headless=headless)
                return
            except ChromeStartError as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning(
                        "Chrome launch attempt 1 failed (%s); reclaiming profile and retrying",
                        exc,
                    )
                    if self._process is not None:
                        try:
                            self._process.terminate()
                        except Exception:
                            logger.debug("Could not terminate failed Chrome subprocess", exc_info=True)
                        self._process = None
                    continue
                break

        self._emit_observe(
            "cdp_unavailable",
            {
                "port": self.port,
                "profile_dir": self.profile_dir,
                "error": str(last_error) if last_error else "unknown",
            },
        )
        if last_error is not None:
            raise last_error
        raise ChromeStartError(
            f"Chrome failed to launch on port {self.port}; CDP unavailable"
        )

    def _reclaim_profile_if_busy(self) -> None:
        """Kill ONLY PIDs whose --user-data-dir matches self.profile_dir.

        ``_profile_user_data_pids`` filters by exact user-data-dir, so the
        user's regular Chrome (running a different profile) is never touched.
        """
        profile_pids = _profile_user_data_pids(self.profile_dir)
        if not profile_pids:
            return
        logger.warning(
            "Reclaiming profile %s held by PID(s) %s (only PIDs matching --user-data-dir)",
            self.profile_dir,
            profile_pids,
        )
        for pid in profile_pids:
            _kill_pid(pid)
        _wait_for_profile_free(self.profile_dir, timeout=5)

    def _spawn_chrome(self, chrome_path: str, *, headless: bool) -> None:
        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            # Scope the DevTools origin allowlist to the loopback client instead of
            # "*": a wildcard lets any local web origin attach to and drive this
            # authenticated profile over CDP. The executor connects via 127.0.0.1.
            f"--remote-allow-origins=http://127.0.0.1:{self.port},http://localhost:{self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--disable-default-apps",
        ]
        if headless:
            cmd.append("--headless=new")
        else:
            cmd.extend([
                "--start-maximized",
                "--window-position=0,0",
                "--window-size=1440,1000",
            ])

        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_for_cdp_ready(self.port, timeout=10)
        if not headless:
            self._ensure_visible_window(self._process.pid)
        logger.info(
            "ManagedChrome started on port %d (PID %d)", self.port, self._process.pid
        )

    def _ensure_visible_window(self, pid: int) -> None:
        if _focus_visible_chrome(pid=pid):
            return
        if _focus_existing_cdp_page(self.port):
            time.sleep(0.5)
            _focus_visible_chrome(pid=pid)
            return
        if _cdp_page_targets(self.port):
            return
        _open_cdp_target(self.port, "about:blank")
        time.sleep(0.5)
        _focus_visible_chrome(pid=pid)

    def _emit_observe(self, event_type: str, payload: dict) -> None:
        if self._observe is None:
            return
        try:
            self._observe(event_type, payload)
        except Exception:
            logger.debug("observe callback raised in ManagedChrome", exc_info=True)

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
            import os
            import signal
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

    def ensure(self, *, headless: bool = False) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.start(headless=headless)

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
            ["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if not output:
        return []
    results = []
    for line in output.splitlines():
        pid = int(line.strip())
        try:
            name = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "comm="], text=True, stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            name = "unknown"
        results.append((pid, name))
    return results


def _pid_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _pid_is_headless(pid: int) -> bool:
    return "--headless" in _pid_command(pid).lower()


def _focus_visible_chrome(*, pid: int | None = None) -> bool:
    process_condition = "((count windows of p) > 0)"
    if pid is not None:
        process_condition = (
            f"((count windows of p) > 0) and (((unix id of p) as integer) is {int(pid)})"
        )
    script = f"""
tell application "Finder" to set desktopBounds to bounds of window of desktop
set targetPid to missing value
tell application "System Events"
  repeat with p in (processes whose name is "Google Chrome")
    if {process_condition} then
      set targetPid to ((unix id of p) as integer)
      set visible of p to true
      set frontmost of p to true
      set position of window 1 of p to {{item 1 of desktopBounds, item 2 of desktopBounds}}
      set size of window 1 of p to {{(item 3 of desktopBounds) - (item 1 of desktopBounds), (item 4 of desktopBounds) - (item 2 of desktopBounds)}}
      return "focused"
      exit repeat
    end if
  end repeat
end tell
if targetPid is missing value then
  tell application "Google Chrome"
    activate
    if (count of windows) > 0 then
      set bounds of window 1 to desktopBounds
      return "focused"
    end if
  end tell
end if
return "missing"
"""
    try:
        result = subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return "focused" in (result.stdout or "")
    except Exception:
        logger.debug("Could not focus visible Chrome", exc_info=True)
        return False


def _open_cdp_target(port: int, url: str) -> None:
    encoded = urllib.parse.quote(url, safe=":/?=&%#")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{encoded}",
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=3):
            return
    except Exception as exc:
        raise ChromeStartError(f"Could not create visible Chrome CDP tab on port {port}: {exc}") from exc


def _focus_existing_cdp_page(port: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                for context in browser.contexts:
                    for page in context.pages:
                        if page.url.startswith("chrome://"):
                            continue
                        page.bring_to_front()
                        return True
            finally:
                browser.close()
    except Exception:
        logger.debug("Could not focus existing CDP page", exc_info=True)
    return False


def _cdp_page_targets(port: int) -> list[dict[str, str]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            raw = response.read(256_000)
    except Exception:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    targets: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "page":
            continue
        targets.append({str(key): str(value) for key, value in item.items()})
    return targets


def _profile_user_data_pids(profile_dir: str) -> list[int]:
    target = str(Path(profile_dir).expanduser().resolve(strict=False))
    try:
        output = subprocess.check_output(
            ["ps", "-axww", "-o", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
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
    """Block until the port is free or raise ChromeStartError.

    P0 hotfix C: no more "proceeding anyway" warning. If the port is still
    held after timeout, fail loudly so the watchdog can degrade.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _check_port_pids(port):
            return
        time.sleep(0.5)
    raise ChromeStartError(f"Port {port} still busy after {timeout}s")


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
