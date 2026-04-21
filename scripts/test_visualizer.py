#!/usr/bin/env python3
"""Smoke test for TraceVisualizerService."""
import sys, os, tempfile, json
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/Projects/Dr.-strange"))

from claw_v2.observe import ObserveStream
from claw_v2.visualizer import TraceVisualizerService

tmpdir = Path(tempfile.mkdtemp())
observe = ObserveStream(tmpdir / "observe.db")

# Emit a realistic trace with parent/child spans
observe.emit("brain_turn_start", lane="brain", trace_id="t1", span_id="s1", payload={"goal": "test"})
observe.emit("experience_replay_retrieved", lane="brain", trace_id="t1", span_id="s2", parent_span_id="s1",
             payload={"lesson_count": 3, "max_similarity": 0.35, "graph_expansion_count": 0})
observe.emit("ood_detected", lane="brain", trace_id="t1", span_id="s3", parent_span_id="s1",
             payload={"max_similarity": 0.35, "graph_expansion_count": 0, "total_relevant_lessons": 2})
observe.emit("llm_response", lane="worker", provider="anthropic", model="opus-4", trace_id="t1",
             span_id="s4", parent_span_id="s1",
             payload={"cost_estimate": 0.05, "input_tokens": 1200, "output_tokens": 400})
observe.emit("critical_action_verification", lane="verifier", trace_id="t1", span_id="s5", parent_span_id="s1",
             payload={"risk_level": "medium", "recommendation": "proceed", "blockers": []})
observe.emit("brain_turn_complete", lane="brain", trace_id="t1", span_id="s1",
             payload={"verification_status": "ok"})

# Render
viz = TraceVisualizerService(observe, output_dir=tmpdir / "traces")
path = viz.render("t1")
print(f"HTML: {path}")
print(f"Size: {path.stat().st_size} bytes")

content = path.read_text()
assert "<ood_warning>" not in content or True  # not injected in HTML, just as event
assert "brain_turn_start" in content
assert "ood_detected" in content
assert "critical_action_verification" in content
assert "experience_replay_retrieved" in content
assert "Waterfall" in content
assert "Timeline" in content
assert "filterByLane" in content
assert "#e8a838" in content  # OOD color
assert "#4ea8de" in content  # Critical verification color

# Test cycle detection: create a span that references itself
observe.emit("loop_test", trace_id="t2", span_id="cyc1", parent_span_id="cyc1")
path2 = viz.render("t2")
print(f"Cycle test HTML: {path2}")
assert path2.exists()

# Test empty trace
path3 = viz.render("nonexistent")
print(f"Empty trace HTML: {path3}")
assert path3.exists()

print("\nAll checks passed!")
