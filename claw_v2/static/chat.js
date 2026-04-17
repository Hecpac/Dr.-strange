const transcript = document.getElementById("transcript");
const form = document.getElementById("chat-form");
const input = document.getElementById("message");
const traceList = document.getElementById("trace-list");
const traceDetail = document.getElementById("trace-detail");
const refreshTracesButton = document.getElementById("refresh-traces");
const sessionId = "mac-main";

function resolveAuthToken() {
  const params = new URLSearchParams(window.location.search);
  const queryToken = params.get("token");
  if (queryToken) {
    window.localStorage.setItem("claw.webChatToken", queryToken);
    return queryToken;
  }
  return window.localStorage.getItem("claw.webChatToken") || "";
}

function authHeaders(base = {}) {
  const token = resolveAuthToken();
  if (!token) return base;
  return {...base, "X-Chat-Token": token};
}

function append(role, text) {
  const item = document.createElement("article");
  item.className = `message ${role}`;
  const pre = document.createElement("pre");
  pre.textContent = text;
  item.appendChild(pre);
  transcript.appendChild(item);
  transcript.scrollTop = transcript.scrollHeight;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  append("user", text);
  input.value = "";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: authHeaders({"Content-Type": "application/json"}),
      body: JSON.stringify({session_id: sessionId, text}),
    });
    const payload = await response.json();
    append("assistant", payload.reply || payload.error || "No reply");
  } catch (error) {
    append("assistant", `Request failed: ${String(error)}`);
  }
});

async function loadTraces() {
  traceList.innerHTML = "";
  try {
    const response = await fetch("/api/traces?limit=10", {headers: authHeaders()});
    const payload = await response.json();
    const traces = payload.traces || [];
    if (!traces.length) {
      traceList.textContent = "No traces yet.";
      return;
    }
    for (const trace of traces) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "trace-item";
      button.innerHTML = `<strong>${trace.trace_id}</strong><span>${trace.last_event_type || "unknown"}</span>`;
      button.addEventListener("click", () => loadTraceDetail(trace.trace_id));
      traceList.appendChild(button);
    }
  } catch (error) {
    traceList.textContent = `Could not load traces: ${String(error)}`;
  }
}

async function loadTraceDetail(traceId) {
  traceDetail.textContent = "Loading...";
  try {
    const response = await fetch(`/api/traces/${encodeURIComponent(traceId)}`, {headers: authHeaders()});
    const payload = await response.json();
    traceDetail.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    traceDetail.textContent = `Could not load trace ${traceId}: ${String(error)}`;
  }
}

refreshTracesButton.addEventListener("click", () => {
  void loadTraces();
});

void loadTraces();
