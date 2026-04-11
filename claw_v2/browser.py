from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover - handled by runtime guard
    sync_playwright = None


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
    """Return *s* safely escaped for embedding inside a JS double-quoted string.

    Uses json.dumps to handle all special characters (backslash, quotes,
    newlines, unicode) then strips the outer quotes so the caller can
    embed the result inside its own ``"..."`` delimiters.
    """
    return json.dumps(s)[1:-1]


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

    _SAFE_BROWSER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

    def run_script(self, script: str, *, timeout: int | None = None, browser_name: str = "default") -> ScriptResult:
        if not self._SAFE_BROWSER_RE.match(browser_name):
            raise BrowserError(f"Unsupported browser name: {browser_name!r}")
        t = timeout or self._timeout
        cmd = [self._path, "--browser", browser_name, "--timeout", str(t)]
        if self._headless:
            cmd.append("--headless")
        env = {"PLAYWRIGHT_BROWSERS_PATH": self._browsers_path}
        return self._runner(cmd, script, env, t + 5)

    def interact(
        self,
        url: str | None = None,
        *,
        actions: list[dict[str, Any]] | None = None,
        page_name: str = "main",
        browser_name: str = "default",
    ) -> BrowseResult:
        safe_page = _js_escape(page_name)
        safe_url = _js_escape(url or "")
        actions_json = _js_escape(json.dumps(actions or []))
        script = f"""
const page = await browser.getPage("{safe_page}");
const initialUrl = "{safe_url}";
const actions = JSON.parse("{actions_json}");
let screenshotPath = null;

async function resolveLocator(action) {{
  if (action.selector) return page.locator(action.selector).first();
  if (action.role) {{
    const options = {{}};
    if (action.name !== undefined) options.name = action.name;
    if (action.exact !== undefined) options.exact = !!action.exact;
    return page.getByRole(action.role, options).first();
  }}
  if (action.label) return page.getByLabel(action.label, {{ exact: !!action.exact }}).first();
  if (action.placeholder) return page.getByPlaceholder(action.placeholder, {{ exact: !!action.exact }}).first();
  if (action.text) return page.getByText(action.text, {{ exact: !!action.exact }}).first();
  throw new Error(`Action ${{action.type}} requires a selector, role, label, placeholder, or text target`);
}}

if (initialUrl) {{
  await page.goto(initialUrl);
}}

for (const action of actions) {{
  switch (action.type) {{
    case "goto":
      if (!action.url) throw new Error("goto action requires url");
      await page.goto(action.url);
      break;
    case "click":
      await (await resolveLocator(action)).click();
      break;
    case "fill":
      await (await resolveLocator(action)).fill(action.value ?? "");
      break;
    case "press":
      if (!action.key) throw new Error("press action requires key");
      await (await resolveLocator(action)).press(action.key);
      break;
    case "check":
      await (await resolveLocator(action)).check();
      break;
    case "uncheck":
      await (await resolveLocator(action)).uncheck();
      break;
    case "select":
      if (action.value === undefined) throw new Error("select action requires value");
      await (await resolveLocator(action)).selectOption(action.value);
      break;
    case "submit": {{
      const locator = await resolveLocator(action);
      const tagName = await locator.evaluate((el) => el.tagName.toLowerCase());
      if (tagName === "form") {{
        await locator.evaluate((form) => form.requestSubmit());
      }} else {{
        await locator.click();
      }}
      break;
    }}
    case "wait_for":
      if (action.ms !== undefined) {{
        await page.waitForTimeout(action.ms);
        break;
      }}
      if (action.url) {{
        await page.waitForURL(action.url, {{ timeout: action.timeout_ms ?? action.timeoutMs }});
        break;
      }}
      await (await resolveLocator(action)).waitFor({{
        state: action.state ?? "visible",
        timeout: action.timeout_ms ?? action.timeoutMs,
      }});
      break;
    case "screenshot": {{
      const buf = await page.screenshot();
      screenshotPath = await saveScreenshot(buf, action.name || "browser-action.png");
      break;
    }}
    default:
      throw new Error(`Unsupported browser action: ${{action.type}}`);
  }}
}}

const snapshot = await page.snapshotForAI();
console.log(JSON.stringify({{
  url: page.url(),
  title: await page.title(),
  content: snapshot.full,
  screenshot_path: screenshotPath
}}));
"""
        result = self.run_script(script, browser_name=browser_name)
        return _parse_browse_result(result, action_name="interact")

    def connect_to_chrome(self, *, cdp_url: str = "http://localhost:9250") -> list[dict[str, str]]:
        with _require_sync_playwright() as pw:
            browser = _cdp_connect(pw, cdp_url)
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                browser.close()
                return []
            pages = [
                {"url": page.url, "title": page.title(), "index": i}
                for i, page in enumerate(context.pages)
            ]
            browser.close()
        return pages

    def chrome_navigate(
        self,
        url: str,
        *,
        cdp_url: str = "http://localhost:9250",
        page_index: int | None = None,
        page_title: str | None = None,
        page_url_pattern: str | None = None,
    ) -> BrowseResult:
        with _require_sync_playwright() as pw:
            browser = _cdp_connect(pw, cdp_url)
            context = browser.contexts[0]
            page = _select_cdp_page(context, page_index=page_index, page_title=page_title, page_url_pattern=page_url_pattern)
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _wait_for_dynamic_content(page, url)
            text = _extract_page_text(page)
            result = BrowseResult(url=page.url, title=page.title(), content=text)
            browser.close()
        return result

    def chrome_screenshot(
        self,
        *,
        cdp_url: str = "http://localhost:9250",
        page_index: int | None = None,
        page_title: str | None = None,
        page_url_pattern: str | None = None,
        name: str = "chrome.png",
    ) -> BrowseResult:
        safe_name = Path(name).name  # strip directory components
        if not safe_name:
            safe_name = "chrome.png"
        with _require_sync_playwright() as pw:
            browser = _cdp_connect(pw, cdp_url)
            context = browser.contexts[0]
            page = _select_cdp_page(context, page_index=page_index, page_title=page_title, page_url_pattern=page_url_pattern)
            page.set_viewport_size({"width": 1280, "height": 900})
            _wait_for_dynamic_content(page, page.url)
            screenshot_path = f"/tmp/claw-{safe_name}"
            page.screenshot(path=screenshot_path)
            text = _extract_page_text(page)
            result = BrowseResult(
                url=page.url,
                title=page.title(),
                content=text,
                screenshot_path=screenshot_path,
            )
            browser.close()
        return result

    def browserbase_browse(
        self,
        url: str,
        *,
        api_key: str,
        project_id: str,
        api_url: str = "https://api.browserbase.com",
        region: str | None = None,
        keep_alive: bool = False,
    ) -> BrowseResult:
        if not api_key.strip():
            raise BrowserError("browserbase api_key is required")
        if not project_id.strip():
            raise BrowserError("browserbase project_id is required")

        session = _browserbase_create_session(
            api_key=api_key,
            project_id=project_id,
            api_url=api_url,
            region=region,
            keep_alive=keep_alive,
        )
        session_id = str(session["id"])
        connect_url = str(session["connectUrl"])
        try:
            with _require_sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(connect_url, timeout=10_000)
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    browser.close()
                    raise BrowserError("browserbase session did not expose a browser context")
                page = context.pages[0] if context.pages else context.new_page()
                page.set_viewport_size({"width": 1280, "height": 900})
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_dynamic_content(page, url)
                text = _extract_page_text(page)
                result = BrowseResult(url=page.url, title=page.title(), content=text)
                browser.close()
                return result
        finally:
            if not keep_alive:
                _browserbase_release_session(
                    api_key=api_key,
                    session_id=session_id,
                    project_id=project_id,
                    api_url=api_url,
                )

    def browse(self, url: str, *, page_name: str = "main") -> BrowseResult:
        safe_url = _js_escape(url)
        safe_page = _js_escape(page_name)
        script = f"""
const page = await browser.getPage("{safe_page}");
await page.goto("{safe_url}");
const text = await page.innerText("body").catch(() => "");
console.log(JSON.stringify({{
  url: page.url(),
  title: await page.title(),
  content: text.substring(0, 4000)
}}));
"""
        result = self.run_script(script)
        return _parse_browse_result(result, action_name="browse")

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
        return _parse_browse_result(result, action_name="screenshot")


_CDP_MAX_RETRIES = 2

# Domains that rely heavily on JS rendering and need extra wait time.
_JS_HEAVY_DOMAINS = ("x.com", "twitter.com", "instagram.com", "facebook.com", "linkedin.com", "reddit.com")

_CONTENT_LIMIT = 8000


def _require_sync_playwright():
    if sync_playwright is None:
        raise BrowserError(
            "playwright is not installed. Install the 'playwright' Python package to enable CDP/browserbase flows."
        )
    return sync_playwright()


def _cdp_connect(pw, cdp_url: str, *, retries: int = _CDP_MAX_RETRIES):
    """Connect to Chrome via CDP with retry logic."""
    import time

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return pw.chromium.connect_over_cdp(cdp_url, timeout=10_000)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1)
    raise BrowserError(f"CDP connection failed after {retries + 1} attempts: {last_exc}") from last_exc


def _is_js_heavy(url: str) -> bool:
    """Check if a URL belongs to a JS-heavy SPA domain."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith(f".{d}") for d in _JS_HEAVY_DOMAINS)


def _wait_for_dynamic_content(page, url: str) -> None:
    """Wait for JS-heavy pages to finish rendering."""
    try:
        if _is_js_heavy(url):
            page.wait_for_load_state("networkidle", timeout=10_000)
            page.wait_for_timeout(1500)
        else:
            page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass  # timeout is acceptable — use whatever loaded


def _extract_page_text(page) -> str:
    """Extract visible text from page with higher limit."""
    try:
        body = page.query_selector("body")
        if body is None:
            return ""
        return body.inner_text()[:_CONTENT_LIMIT]
    except Exception:
        return ""


def _select_cdp_page(context, *, page_index=None, page_title=None, page_url_pattern=None):
    if page_index is not None and 0 <= page_index < len(context.pages):
        return context.pages[page_index]
    if page_url_pattern is not None:
        for page in context.pages:
            if page_url_pattern in page.url:
                return page
    if page_title is not None:
        for page in context.pages:
            if page_title.lower() in page.title().lower():
                return page
    return context.new_page()


def _parse_browse_result(result: ScriptResult, *, action_name: str) -> BrowseResult:
    if result.return_code != 0:
        raise BrowserError(f"{action_name} failed (exit {result.return_code}): {result.stderr}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BrowserError(f"invalid JSON from browser: {result.stdout[:200]}") from exc
    return BrowseResult(
        url=data["url"],
        title=data["title"],
        content=data["content"],
        screenshot_path=data.get("screenshot_path"),
    )


def _browserbase_create_session(
    *,
    api_key: str,
    project_id: str,
    api_url: str,
    region: str | None,
    keep_alive: bool,
) -> dict[str, Any]:
    import httpx

    payload: dict[str, Any] = {
        "projectId": project_id,
        "keepAlive": keep_alive,
    }
    if region:
        payload["region"] = region
    response = httpx.post(
        f"{api_url.rstrip('/')}/v1/sessions",
        headers={
            "Content-Type": "application/json",
            "X-BB-API-Key": api_key,
        },
        json=payload,
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    if "id" not in data or "connectUrl" not in data:
        raise BrowserError("browserbase session response missing id/connectUrl")
    return data


def _browserbase_release_session(
    *,
    api_key: str,
    session_id: str,
    project_id: str,
    api_url: str,
) -> None:
    import httpx

    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/v1/sessions/{session_id}",
            headers={
                "Content-Type": "application/json",
                "X-BB-API-Key": api_key,
            },
            json={
                "projectId": project_id,
                "status": "REQUEST_RELEASE",
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except Exception:
        # Cleanup is best-effort; the session will eventually expire server-side.
        return
