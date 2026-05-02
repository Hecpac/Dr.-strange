from __future__ import annotations

import json
from typing import Any, Callable

from claw_v2.observation_window import ObservationWindowState

StartResponse = Callable[[str, list[tuple[str, str]]], object]


class ObservabilityDashboard:
    def __init__(self, observation_window: ObservationWindowState) -> None:
        self._window = observation_window

    def wsgi_app(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/")).split("?", 1)[0]
        query = str(environ.get("QUERY_STRING", ""))
        if path == "/observability":
            return self._html(start_response)
        if path == "/observability/state":
            return self._json(start_response, 200, self._window.status_payload())
        if path == "/observability/events":
            params = _parse_query(query)
            event_type = params.get("event_type") or None
            limit = _coerce_int(params.get("limit"), 50)
            return self._json(start_response, 200, self._window.events_payload(limit=limit, event_type=event_type))
        if path == "/observability/freeze":
            if method != "POST":
                return self._json(start_response, 405, {"error": "method not allowed", "allowed": ["POST"]})
            self._window.freeze(reason="manual_dashboard", actor="dashboard")
            return self._json(start_response, 200, self._window.status_payload())
        if path == "/observability/unfreeze":
            if method != "POST":
                return self._json(start_response, 405, {"error": "method not allowed", "allowed": ["POST"]})
            self._window.unfreeze(actor="dashboard")
            return self._json(start_response, 200, self._window.status_payload())
        return self._json(start_response, 404, {"error": f"not found: {path}"})

    def _html(self, start_response: StartResponse) -> list[bytes]:
        body = _DASHBOARD_HTML.encode("utf-8")
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def _json(self, start_response: StartResponse, status_code: int, payload: dict[str, Any]) -> list[bytes]:
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        start_response(
            f"{status_code} {_reason(status_code)}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]


def _parse_query(query: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in query.split("&"):
        if not chunk:
            continue
        if "=" not in chunk:
            result[chunk] = ""
            continue
        key, value = chunk.split("=", 1)
        result[key] = value.replace("+", " ")
    return result


def _coerce_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _reason(status_code: int) -> str:
    return {
        200: "OK",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(status_code, "OK")


_DASHBOARD_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Claw Observability</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #15181d;
      --muted: #657080;
      --border: #d7dce3;
      --accent: #146c94;
      --danger: #b42318;
      --ok: #087443;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111317;
        --panel: #181b20;
        --text: #f1f4f8;
        --muted: #a7b0be;
        --border: #303640;
        --accent: #62b6d8;
        --danger: #ff8a7a;
        --ok: #62c48f;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 18px 24px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      margin: 0 0 14px;
      font-size: 22px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(140px, 1fr));
      gap: 10px;
      align-items: stretch;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--bg);
      min-width: 0;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .value {{
      margin-top: 5px;
      font-size: 18px;
      font-weight: 650;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    main {{ padding: 18px 24px 32px; }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-bottom: 14px;
    }}
    button, select {{
      height: 36px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 0 12px;
      font: inherit;
    }}
    button.primary {{ border-color: var(--accent); color: var(--accent); }}
    button.danger {{ border-color: var(--danger); color: var(--danger); }}
    .state {{
      margin-left: auto;
      color: var(--muted);
      font-size: 13px;
      min-width: 180px;
      text-align: right;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: color-mix(in srgb, var(--panel) 88%, var(--border));
    }}
    tr:last-child td {{ border-bottom: 0; }}
    code {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: var(--muted);
    }}
    .ok {{ color: var(--ok); }}
    .danger-text {{ color: var(--danger); }}
    @media (max-width: 920px) {{
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .state {{ margin-left: 0; text-align: left; width: 100%; }}
      th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Claw Observability</h1>
    <section class="metrics">
      <div class="metric"><div class="label">Cost Today</div><div class="value" id="costToday">$0.0000</div></div>
      <div class="metric"><div class="label">Budget Remaining</div><div class="value" id="remaining">n/a</div></div>
      <div class="metric"><div class="label">Actions / Min</div><div class="value" id="actionsMin">0</div></div>
      <div class="metric"><div class="label">Recent Failure Rate</div><div class="value" id="failureRate">0%</div></div>
      <div class="metric"><div class="label">Active Goal</div><div class="value" id="goalId">n/a</div></div>
    </section>
  </header>
  <main>
    <div class="toolbar">
      <button class="danger" id="freezeBtn">Freeze</button>
      <button class="primary" id="unfreezeBtn">Unfreeze</button>
      <select id="eventFilter" aria-label="Filter event type">
        <option value="">All events</option>
      </select>
      <div class="state" id="freezeState">loading</div>
    </div>
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Event</th>
          <th>Lane</th>
          <th>Provider</th>
          <th>Model</th>
          <th>Payload</th>
        </tr>
      </thead>
      <tbody id="eventsBody"></tbody>
    </table>
  </main>
  <script>
    const state = {{ eventTypes: new Set() }};
    const $ = (id) => document.getElementById(id);
    async function post(path) {{
      await fetch(path, {{ method: "POST" }});
      await refresh();
    }}
    function money(value) {{
      return "$" + Number(value || 0).toFixed(4);
    }}
    function pct(value) {{
      return Math.round(Number(value || 0) * 100) + "%";
    }}
    function text(value) {{
      if (value === null || value === undefined || value === "") return "n/a";
      return String(value);
    }}
    function setMetric(id, value) {{ $(id).textContent = value; }}
    function renderStatus(payload) {{
      setMetric("costToday", money(payload.cost_today));
      setMetric("remaining", payload.daily_budget_remaining === null ? "n/a" : money(payload.daily_budget_remaining));
      setMetric("actionsMin", text(payload.actions_per_minute));
      setMetric("failureRate", pct(payload.recent_failure_rate));
      setMetric("goalId", text(payload.active_goal_id));
      const frozen = Boolean(payload.frozen);
      $("freezeState").innerHTML = frozen
        ? '<span class="danger-text">Frozen</span> ' + text(payload.freeze_reason)
        : '<span class="ok">Live</span>';
    }}
    function renderEvents(events) {{
      const filter = $("eventFilter");
      for (const event of events) {{
        if (!state.eventTypes.has(event.event_type)) {{
          state.eventTypes.add(event.event_type);
          const option = document.createElement("option");
          option.value = event.event_type;
          option.textContent = event.event_type;
          filter.appendChild(option);
        }}
      }}
      $("eventsBody").innerHTML = events.map((event) => `
        <tr>
          <td>${{escapeHtml(event.timestamp || "")}}</td>
          <td>${{escapeHtml(event.event_type || "")}}</td>
          <td>${{escapeHtml(event.lane || "")}}</td>
          <td>${{escapeHtml(event.provider || "")}}</td>
          <td>${{escapeHtml(event.model || "")}}</td>
          <td><code>${{escapeHtml(JSON.stringify(event.payload || {{}}, null, 2))}}</code></td>
        </tr>
      `).join("");
    }}
    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }}
    async function refresh() {{
      const status = await fetch("/observability/state").then((r) => r.json());
      renderStatus(status);
      const eventType = encodeURIComponent($("eventFilter").value);
      const path = eventType ? `/observability/events?event_type=${{eventType}}&limit=50` : "/observability/events?limit=50";
      const events = await fetch(path).then((r) => r.json());
      renderEvents(events.events || []);
    }}
    $("freezeBtn").addEventListener("click", () => post("/observability/freeze"));
    $("unfreezeBtn").addEventListener("click", () => post("/observability/unfreeze"));
    $("eventFilter").addEventListener("change", refresh);
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""
