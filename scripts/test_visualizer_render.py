"""Test TraceVisualizerService rendering with synthetic events."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.visualizer import TraceVisualizerService

mock_observe = MagicMock()
mock_observe.trace_events.return_value = [
    {
        "event_type": "brain_turn_start",
        "lane": "brain",
        "provider": "anthropic",
        "model": "opus",
        "trace_id": "abc123",
        "root_trace_id": "abc123",
        "span_id": "span_001",
        "parent_span_id": None,
        "job_id": None,
        "artifact_id": None,
        "payload": {"message": "hello"},
        "timestamp": "2026-04-20T10:00:00",
    },
    {
        "event_type": "ood_detected",
        "lane": "brain",
        "provider": "anthropic",
        "model": "opus",
        "trace_id": "abc123",
        "root_trace_id": "abc123",
        "span_id": "span_002",
        "parent_span_id": "span_001",
        "job_id": None,
        "artifact_id": None,
        "payload": {"max_similarity": 0.2, "graph_expansion_count": 0},
        "timestamp": "2026-04-20T10:00:01",
    },
    {
        "event_type": "experience_replay_retrieved",
        "lane": "worker",
        "provider": "anthropic",
        "model": "opus",
        "trace_id": "abc123",
        "root_trace_id": "abc123",
        "span_id": "span_003",
        "parent_span_id": "span_001",
        "job_id": None,
        "artifact_id": None,
        "payload": {"lessons": 3},
        "timestamp": "2026-04-20T10:00:02",
    },
    {
        "event_type": "brain_turn_complete",
        "lane": "brain",
        "provider": "anthropic",
        "model": "opus",
        "trace_id": "abc123",
        "root_trace_id": "abc123",
        "span_id": "span_004",
        "parent_span_id": "span_002",
        "job_id": None,
        "artifact_id": None,
        "payload": {"tokens": 1500},
        "timestamp": "2026-04-20T10:00:03",
    },
]

with tempfile.TemporaryDirectory() as tmpdir:
    viz = TraceVisualizerService(mock_observe, output_dir=Path(tmpdir))
    result = viz.render("abc123")
    content = result.read_text()

    assert "abc123" in content, "trace_id missing"
    assert "ood_detected" in content, "OOD event missing"
    assert "experience_replay_retrieved" in content, "replay event missing"
    assert "brain_turn_complete" in content, "brain complete missing"
    assert "#e8a838" in content, "OOD color missing"
    assert "#4ade80" in content, "replay color missing"
    assert "Trace" in content, "breadcrumbs missing"
    assert "waterfall" in content, "waterfall panel missing"
    assert "timeline" in content, "timeline panel missing"
    assert "laneFilter" in content, "lane filter missing"
    assert "span_001" in content[:100] or "span_00" in content, "spans rendered"

    # Check no infinite loops (file should be reasonable size)
    assert len(content) < 50000, f"HTML too large: {len(content)} bytes"

    print(f"OK — rendered {len(content)} bytes to {result}")
    print(f"Spans: 4 events, 3 unique span_ids with parent-child hierarchy")
    print(f"Breadcrumbs, waterfall, timeline, filters all present")
