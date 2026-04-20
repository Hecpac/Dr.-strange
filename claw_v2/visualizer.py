"""Trace Visualizer — renders ObserveStream events as a self-contained HTML waterfall."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from claw_v2.observe import ObserveStream

EVENT_COLORS: dict[str, str] = {
    "ood_detected": "#e8a838",
    "critical_action_verification": "#4ea8de",
    "experience_replay_retrieved": "#4ade80",
    "brain_turn_start": "#6b7280",
    "brain_turn_complete": "#6b7280",
    "llm_response": "#a78bfa",
    "llm_decision": "#a78bfa",
    "llm_fallback": "#f87171",
    "nlm_research_started": "#38bdf8",
    "nlm_research_completed": "#38bdf8",
    "soul_update_suggestion": "#f472b6",
    "lessons_graph_hit": "#34d399",
}
DEFAULT_COLOR = "#9ca3af"


@dataclass
class SpanNode:
    span_id: str
    parent_span_id: str | None
    events: list[dict] = field(default_factory=list)
    children: list[SpanNode] = field(default_factory=list)


class TraceVisualizerService:
    def __init__(self, observe: ObserveStream, output_dir: Path | None = None) -> None:
        self.observe = observe
        self.output_dir = output_dir or Path("/tmp/claw-traces")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, trace_id: str, *, limit: int = 500) -> Path:
        events = self.observe.trace_events(trace_id, limit=limit)
        if not events:
            events = self._fallback_recent(trace_id)
        tree = self._build_tree(events)
        html = self._render_html(trace_id, events, tree)
        out_path = self.output_dir / f"trace_{trace_id[:12]}.html"
        out_path.write_text(html, encoding="utf-8")
        return out_path

    def _fallback_recent(self, trace_id: str) -> list[dict]:
        recent = self.observe.recent_events(limit=200)
        return [e for e in recent if (e.get("trace_id") or "").startswith(trace_id)]

    def _build_tree(self, events: list[dict]) -> list[SpanNode]:
        nodes: dict[str, SpanNode] = {}
        no_span: list[dict] = []
        for ev in events:
            sid = ev.get("span_id")
            if not sid:
                no_span.append(ev)
                continue
            if sid not in nodes:
                nodes[sid] = SpanNode(span_id=sid, parent_span_id=ev.get("parent_span_id"))
            nodes[sid].events.append(ev)

        roots: list[SpanNode] = []
        visited: set[str] = set()
        for sid, node in nodes.items():
            pid = node.parent_span_id
            if pid and pid in nodes and pid != sid:
                nodes[pid].children.append(node)
            else:
                roots.append(node)

        if no_span:
            orphan = SpanNode(span_id="__root__", parent_span_id=None, events=no_span)
            roots.insert(0, orphan)

        self._break_cycles(roots, visited)
        return roots

    def _break_cycles(self, nodes: list[SpanNode], visited: set[str]) -> None:
        for node in nodes:
            if node.span_id in visited:
                node.children = []
                continue
            visited.add(node.span_id)
            self._break_cycles(node.children, visited)

    def _render_html(self, trace_id: str, events: list[dict], tree: list[SpanNode]) -> str:
        lanes = sorted({e.get("lane") or "unknown" for e in events})
        timestamps = [e.get("timestamp", "") for e in events if e.get("timestamp")]
        t_start = min(timestamps) if timestamps else "?"
        t_end = max(timestamps) if timestamps else "?"
        event_count = len(events)
        span_count = sum(1 for e in events if e.get("span_id"))

        waterfall_html = self._render_tree(tree, depth=0)
        timeline_html = self._render_timeline(events)
        lane_options = "".join(f'<option value="{escape(l)}">{escape(l)}</option>' for l in lanes)

        return _HTML_TEMPLATE.format(
            trace_id=escape(trace_id),
            t_start=escape(t_start),
            t_end=escape(t_end),
            event_count=event_count,
            span_count=span_count,
            lane_options=lane_options,
            waterfall=waterfall_html,
            timeline=timeline_html,
        )

    def _render_tree(self, nodes: list[SpanNode], depth: int) -> str:
        if depth > 20:
            return '<li class="span-node">... (max depth)</li>'
        parts: list[str] = []
        for node in nodes:
            color = DEFAULT_COLOR
            ev_types: list[str] = []
            for ev in node.events:
                et = ev.get("event_type", "")
                ev_types.append(et)
                if et in EVENT_COLORS:
                    color = EVENT_COLORS[et]

            label = ", ".join(dict.fromkeys(ev_types)) or "span"
            lane = node.events[0].get("lane") or "" if node.events else ""
            ts = node.events[0].get("timestamp") or "" if node.events else ""
            sid_short = node.span_id[:8] if node.span_id != "__root__" else "root"

            payload_blocks = []
            for ev in node.events:
                p = ev.get("payload") or {}
                ev_lane = ev.get("lane") or "unknown"
                ev_ts = ev.get("timestamp") or ""
                ev_type = ev.get("event_type") or ""
                payload_blocks.append(
                    f'<div class="ev-entry" data-lane="{escape(ev_lane)}">'
                    f'<span class="ev-ts">{escape(ev_ts)}</span> '
                    f'<span class="ev-type" style="color:{EVENT_COLORS.get(ev_type, DEFAULT_COLOR)}">'
                    f'{escape(ev_type)}</span>'
                    f'<pre class="payload">{escape(json.dumps(p, indent=2, ensure_ascii=False, default=str))}</pre>'
                    f'</div>'
                )
            payloads_html = "\n".join(payload_blocks)

            children_html = ""
            if node.children:
                children_html = f'<ul class="span-children">{self._render_tree(node.children, depth + 1)}</ul>'

            parts.append(
                f'<li class="span-node" data-lane="{escape(lane)}">'
                f'<div class="span-header" onclick="togglePayload(this)" '
                f'style="border-left: 3px solid {color}; padding-left:{8 + depth * 16}px">'
                f'<span class="span-id">{escape(sid_short)}</span> '
                f'<span class="span-label" style="color:{color}">{escape(label)}</span> '
                f'<span class="span-lane">{escape(lane)}</span> '
                f'<span class="span-ts">{escape(ts)}</span> '
                f'<span class="ev-count">({len(node.events)} ev)</span>'
                f'</div>'
                f'<div class="span-payload" style="display:none">{payloads_html}</div>'
                f'{children_html}'
                f'</li>'
            )
        return "\n".join(parts)

    def _render_timeline(self, events: list[dict]) -> str:
        parts: list[str] = []
        for ev in events:
            et = ev.get("event_type") or ""
            color = EVENT_COLORS.get(et, DEFAULT_COLOR)
            ts = ev.get("timestamp") or ""
            lane = ev.get("lane") or "unknown"
            parts.append(
                f'<div class="tl-event" data-lane="{escape(lane)}" '
                f'style="border-left:3px solid {color}">'
                f'<span class="tl-ts">{escape(ts)}</span> '
                f'<span class="tl-type" style="color:{color}">{escape(et)}</span> '
                f'<span class="tl-lane">{escape(lane)}</span>'
                f'</div>'
            )
        return "\n".join(parts)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trace {trace_id}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:13px;padding:16px}}
h1{{font-size:18px;color:#58a6ff;margin-bottom:4px}}
.meta{{color:#8b949e;margin-bottom:16px;font-size:12px}}
.meta span{{margin-right:16px}}
.tabs{{display:flex;gap:8px;margin-bottom:12px}}
.tab{{padding:6px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px;cursor:pointer;color:#8b949e}}
.tab.active{{color:#58a6ff;border-color:#58a6ff}}
.panel{{display:none}}
.panel.active{{display:block}}
.filters{{margin-bottom:12px;display:flex;gap:8px;align-items:center}}
.filters select,.filters input{{background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:4px 8px;border-radius:4px;font-size:12px}}
.breadcrumbs{{color:#8b949e;font-size:11px;margin-bottom:8px}}

/* Waterfall */
ul.span-tree,ul.span-children{{list-style:none;padding-left:0}}
.span-node{{margin:2px 0}}
.span-header{{display:flex;align-items:center;gap:8px;padding:4px 8px;cursor:pointer;border-radius:4px}}
.span-header:hover{{background:#161b22}}
.span-id{{color:#8b949e;font-size:11px;min-width:60px}}
.span-label{{font-weight:600}}
.span-lane{{color:#8b949e;font-size:11px}}
.span-ts{{color:#484f58;font-size:11px;margin-left:auto}}
.ev-count{{color:#484f58;font-size:11px}}
.span-payload{{margin-left:24px;border-left:1px solid #21262d;padding-left:12px}}
.ev-entry{{margin:4px 0;padding:4px 0;border-bottom:1px solid #161b22}}
.ev-ts{{color:#484f58;font-size:11px}}
.ev-type{{font-weight:600;font-size:12px}}
pre.payload{{color:#7d8590;font-size:11px;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;margin-top:4px;padding:6px;background:#0d1117;border-radius:4px;border:1px solid #21262d}}

/* Timeline */
.tl-event{{display:flex;align-items:center;gap:8px;padding:3px 8px;font-size:12px}}
.tl-event:hover{{background:#161b22}}
.tl-ts{{color:#484f58;font-size:11px;min-width:180px}}
.tl-type{{font-weight:600}}
.tl-lane{{color:#8b949e;font-size:11px}}

/* Legend */
.legend{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}}
.legend-item{{display:flex;align-items:center;gap:4px;font-size:11px;color:#8b949e}}
.legend-dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
</style>
</head>
<body>
<h1>Trace: {trace_id}</h1>
<div class="meta">
<span>Start: {t_start}</span>
<span>End: {t_end}</span>
<span>Events: {event_count}</span>
<span>Spans: {span_count}</span>
</div>

<div class="legend">
<div class="legend-item"><span class="legend-dot" style="background:#e8a838"></span>OOD Detected</div>
<div class="legend-item"><span class="legend-dot" style="background:#4ea8de"></span>Critical Verification</div>
<div class="legend-item"><span class="legend-dot" style="background:#4ade80"></span>Experience Replay</div>
<div class="legend-item"><span class="legend-dot" style="background:#6b7280"></span>Brain Turn</div>
<div class="legend-item"><span class="legend-dot" style="background:#a78bfa"></span>LLM Call</div>
<div class="legend-item"><span class="legend-dot" style="background:#f87171"></span>Fallback</div>
</div>

<div class="breadcrumbs" id="breadcrumbs">Trace &gt; {trace_id}</div>

<div class="filters">
<label>Lane:</label>
<select id="laneFilter" onchange="filterByLane(this.value)">
<option value="all">All</option>
{lane_options}
</select>
<input type="text" id="searchBox" placeholder="Search events..." oninput="searchEvents(this.value)">
</div>

<div class="tabs">
<div class="tab active" onclick="switchTab('waterfall',this)">Waterfall</div>
<div class="tab" onclick="switchTab('timeline',this)">Timeline</div>
</div>

<div id="waterfall" class="panel active">
<ul class="span-tree">
{waterfall}
</ul>
</div>

<div id="timeline" class="panel">
{timeline}
</div>

<script>
function switchTab(id,el){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
}}
function togglePayload(header){{
  var payload=header.nextElementSibling;
  payload.style.display=payload.style.display==='none'?'block':'none';
  if(payload.style.display!=='none')updateBreadcrumbs(header);
}}
function updateBreadcrumbs(header){{
  var parts=['Trace','{trace_id}'];
  var el=header.closest('.span-node');
  var path=[];
  while(el){{
    var h=el.querySelector(':scope > .span-header');
    if(h)path.unshift(h.querySelector('.span-label').textContent);
    el=el.parentElement?el.parentElement.closest('.span-node'):null;
  }}
  document.getElementById('breadcrumbs').textContent=parts.concat(path).join(' > ');
}}
function filterByLane(lane){{
  document.querySelectorAll('[data-lane]').forEach(el=>{{
    el.style.display=(lane==='all'||el.dataset.lane===lane)?'':'none';
  }});
}}
function searchEvents(q){{
  var lower=q.toLowerCase();
  document.querySelectorAll('.span-node,.tl-event').forEach(el=>{{
    el.style.display=(!q||el.textContent.toLowerCase().includes(lower))?'':'none';
  }});
}}
</script>
</body>
</html>"""
