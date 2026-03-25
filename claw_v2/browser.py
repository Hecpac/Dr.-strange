from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable


class BrowserError(Exception):
    """Raised when a dev-browser operation fails."""


@dataclass(slots=True)
class BrowseResult:
    url: str
    title: str
    content: str
    screenshot_path: str | None = None


@dataclass(slots=True)
class ScriptResult:
    stdout: str
    stderr: str
    return_code: int


CommandRunner = Callable[[list[str], str, dict[str, str], int], ScriptResult]


def _default_runner(cmd: list[str], stdin: str, env: dict[str, str], timeout: int) -> ScriptResult:
    merged_env = {**os.environ, **env}
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BrowserError(f"script timed out after {timeout}s") from exc
    return ScriptResult(stdout=result.stdout, stderr=result.stderr, return_code=result.returncode)


def _js_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


class DevBrowserService:
    def __init__(
        self,
        *,
        dev_browser_path: str = "dev-browser",
        browsers_path: str = "/tmp/pw-browsers",
        timeout: int = 30,
        headless: bool = True,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._path = dev_browser_path
        self._browsers_path = browsers_path
        self._timeout = timeout
        self._headless = headless
        self._runner = command_runner or _default_runner

    def run_script(self, script: str, *, timeout: int | None = None, browser_name: str = "default") -> ScriptResult:
        t = timeout or self._timeout
        cmd = [self._path, "--browser", browser_name, "--timeout", str(t)]
        if self._headless:
            cmd.append("--headless")
        env = {"PLAYWRIGHT_BROWSERS_PATH": self._browsers_path}
        return self._runner(cmd, script, env, t + 5)

    def browse(self, url: str, *, page_name: str = "main") -> BrowseResult:
        safe_url = _js_escape(url)
        safe_page = _js_escape(page_name)
        script = f"""
const page = await browser.getPage("{safe_page}");
await page.goto("{safe_url}");
const snapshot = await page.snapshotForAI();
console.log(JSON.stringify({{
  url: page.url(),
  title: await page.title(),
  content: snapshot.full
}}));
"""
        result = self.run_script(script)
        if result.return_code != 0:
            raise BrowserError(f"browse failed (exit {result.return_code}): {result.stderr}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BrowserError(f"invalid JSON from browser: {result.stdout[:200]}") from exc
        return BrowseResult(url=data["url"], title=data["title"], content=data["content"])

    def screenshot(self, url: str, *, name: str = "screenshot.png", page_name: str = "main") -> BrowseResult:
        safe_url = _js_escape(url)
        safe_page = _js_escape(page_name)
        safe_name = _js_escape(name)
        script = f"""
const page = await browser.getPage("{safe_page}");
await page.goto("{safe_url}");
const buf = await page.screenshot();
const path = await saveScreenshot(buf, "{safe_name}");
const snapshot = await page.snapshotForAI();
console.log(JSON.stringify({{
  url: page.url(),
  title: await page.title(),
  content: snapshot.full,
  screenshot_path: path
}}));
"""
        result = self.run_script(script)
        if result.return_code != 0:
            raise BrowserError(f"screenshot failed (exit {result.return_code}): {result.stderr}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BrowserError(f"invalid JSON from browser: {result.stdout[:200]}") from exc
        return BrowseResult(
            url=data["url"], title=data["title"], content=data["content"],
            screenshot_path=data.get("screenshot_path"),
        )
