"""Poll Claude Design generation, capture final screenshot, deliver to Telegram.

Runs as a background companion to cdp_claude_design_driver.py. Polls the
claude.ai/design tab on CDP 9250, detects generation completion (no
'Shelling'/'Generating' markers + visible canvas output), captures
full-page screenshot, sends via Telegram sendPhoto.

Env required (loaded from ~/.claw/env):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_ALLOWED_USER_ID

Token is never logged. Failures append to ~/.claw/qts-design-poll.log.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import websocket

CDP_HOST = "http://127.0.0.1:9250"
LOG = Path.home() / ".claw" / "claude-design-poll.log"
SHOTS = Path.home() / ".claw" / "screenshots"
POLL_INTERVAL_S = 30
MAX_WAIT_S = 30 * 60  # 30 min hard cap

DONE_NEGATIVE_MARKERS = ["Shelling", "Generating", "Thinking", "Planning", "Working"]


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def list_tabs():
    return json.loads(urlopen(f"{CDP_HOST}/json/list").read())


def find_design_tab():
    for t in list_tabs():
        if t.get("type") == "page" and "claude.ai/design/p/" in t.get("url", ""):
            return t
    return None


class CDPSession:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._mid = 0

    def call(self, method, params=None, timeout=30):
        self._mid += 1
        self.ws.send(json.dumps({"id": self._mid, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        try: self.ws.close()
        except Exception: pass


def probe_state(sess):
    js = (
        "JSON.stringify({"
        "url: location.href, "
        "ready: document.readyState, "
        "bodyText: (document.body && document.body.innerText || '').slice(0, 4000), "
        "iframes: document.querySelectorAll('iframe').length, "
        "images: document.querySelectorAll('img').length, "
        "buttons_play: Array.from(document.querySelectorAll('button')).filter(function(b){return /preview|play|run/i.test(b.textContent||'');}).length"
        "})"
    )
    res = sess.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    return json.loads(res["result"]["value"])


def is_done(state):
    body = state.get("bodyText", "")
    has_negative = any(m in body for m in DONE_NEGATIVE_MARKERS)
    has_output = state.get("iframes", 0) > 0 or state.get("buttons_play", 0) > 0
    return (not has_negative) and has_output


def capture_screenshot(sess, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    res = sess.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, timeout=60)
    path = SHOTS / name
    path.write_bytes(base64.b64decode(res.get("data", "")))
    return path


def load_env():
    env_path = Path.home() / ".claw" / "env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def telegram_send_photo(token, chat_id, photo_path, caption):
    import mimetypes
    boundary = "----StrangeBoundary" + str(int(time.time()))
    body = []
    def add_field(name, value):
        body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    def add_file(name, fname, content, ctype):
        body.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode()
        )
        body.append(content)
        body.append(b"\r\n")
    add_field("chat_id", chat_id)
    add_field("caption", caption)
    with open(photo_path, "rb") as f:
        add_file("photo", photo_path.name, f.read(), mimetypes.guess_type(str(photo_path))[0] or "image/png")
    body.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(body)
    req = Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def telegram_send_message(token, chat_id, text):
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    log("poller_start")
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_ALLOWED_USER_ID") or os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    if not token or not chat_id:
        log("missing_telegram_creds")
        return 2

    deadline = time.time() + MAX_WAIT_S
    last_state = None
    stable_done_count = 0
    while time.time() < deadline:
        tab = find_design_tab()
        if not tab:
            log("no_design_tab_yet")
            time.sleep(POLL_INTERVAL_S)
            continue
        try:
            sess = CDPSession(tab["webSocketDebuggerUrl"])
            sess.call("Page.enable")
            sess.call("Runtime.enable")
            state = probe_state(sess)
            done = is_done(state)
            log(f"probe iframes={state.get('iframes')} images={state.get('images')} "
                f"play_btns={state.get('buttons_play')} done={done}")
            if done:
                stable_done_count += 1
            else:
                stable_done_count = 0
            # Require 2 consecutive done polls (~1 min stable) before declaring complete
            if stable_done_count >= 2:
                shot = capture_screenshot(sess, f"claude-design-final-{int(time.time())}.png")
                log(f"completed_shot={shot}")
                project_url = state.get("url", "")
                caption = (
                    "[Dr. Strange] AI Lead Gen — Landing B2B prototype listo en Claude Design.\n"
                    f"Project: {project_url}"
                )
                resp = telegram_send_photo(token, chat_id, shot, caption)
                log(f"telegram_sendPhoto ok={resp.get('ok')}")
                sess.close()
                return 0
            sess.close()
        except Exception as exc:
            log(f"probe_error {type(exc).__name__}: {exc}")
        time.sleep(POLL_INTERVAL_S)
    # Timeout
    log("timeout_reached")
    telegram_send_message(token, chat_id,
                           "[Dr. Strange] Claude Design tardó >30min. Revisalo directo: "
                           "https://claude.ai/design/p/035e6b5d-3120-4eb6-9f85-5f66d9338958")
    return 3


if __name__ == "__main__":
    sys.exit(main())
