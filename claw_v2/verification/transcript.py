"""Two-timeline transcript writer for the Petri-backed verifier.

Spec: ``docs/superpowers/specs/2026-05-01-petri-evidence-verifier-design.md``
section 4.1. Each task emits two parallel JSONL streams keyed on the same
``task_id``:

- ``{task_id}-target.jsonl``   — what the agent did for the user / external
  systems. This is what the judge will score.
- ``{task_id}-harness.jsonl``  — verifier calls, retries, internal tool
  selection, scaffolding errors. The judge MUST NOT see this stream.

This commit is intentionally additive: it adds the writers and readers but
does not change the existing single-stream event flow under
``config.telemetry_root``. Wiring the runtime to emit through these helpers
is the job of commit #7.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl


TRANSCRIPT_SCHEMA_VERSION = "petri.transcript.v2"
"""Schema tag stored on every record so legacy ``v1`` consumers (the existing
``events.jsonl`` writers) can tell streams apart at read time. The Petri spec
section 4.1 calls this out as the dispatch field for the verifier."""


class TranscriptStream(str, Enum):
    """Which timeline a record belongs to.

    ``TARGET`` — agent's user-facing actions (judge sees this).
    ``HARNESS`` — verifier/tool/scaffolding events (judge MUST NOT see this).
    """

    TARGET = "target"
    HARNESS = "harness"


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    record_id: str
    schema_version: str
    task_id: str
    stream: TranscriptStream
    event_type: str
    timestamp: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "task_id": self.task_id,
            "stream": self.stream.value,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }


def target_stream_path(telemetry_root: Path | str, task_id: str) -> Path:
    """Path to the target timeline for ``task_id``."""
    return _stream_path(telemetry_root, task_id, TranscriptStream.TARGET)


def harness_stream_path(telemetry_root: Path | str, task_id: str) -> Path:
    """Path to the harness timeline for ``task_id``."""
    return _stream_path(telemetry_root, task_id, TranscriptStream.HARNESS)


def record_target_event(
    telemetry_root: Path | str,
    *,
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> TranscriptRecord:
    """Append a target-timeline event to ``{task_id}-target.jsonl``."""
    return _record(
        telemetry_root,
        task_id=task_id,
        stream=TranscriptStream.TARGET,
        event_type=event_type,
        payload=payload,
    )


def record_harness_event(
    telemetry_root: Path | str,
    *,
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> TranscriptRecord:
    """Append a harness-timeline event to ``{task_id}-harness.jsonl``.

    The judge in commits #7-#9 reads ONLY the target stream. Anything written
    here stays out of the judge context — that is the contract that lets the
    judge avoid grading itself.
    """
    return _record(
        telemetry_root,
        task_id=task_id,
        stream=TranscriptStream.HARNESS,
        event_type=event_type,
        payload=payload,
    )


def read_target_stream(telemetry_root: Path | str, task_id: str) -> list[TranscriptRecord]:
    """Read every record from the target timeline for ``task_id``."""
    return _read_stream(telemetry_root, task_id, TranscriptStream.TARGET)


def read_harness_stream(telemetry_root: Path | str, task_id: str) -> list[TranscriptRecord]:
    """Read every record from the harness timeline for ``task_id``."""
    return _read_stream(telemetry_root, task_id, TranscriptStream.HARNESS)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _stream_path(telemetry_root: Path | str, task_id: str, stream: TranscriptStream) -> Path:
    if not task_id:
        raise ValueError("task_id is required")
    safe_task_id = task_id.replace("/", "_").replace(":", "_")
    return Path(telemetry_root).expanduser() / f"{safe_task_id}-{stream.value}.jsonl"


def _record(
    telemetry_root: Path | str,
    *,
    task_id: str,
    stream: TranscriptStream,
    event_type: str,
    payload: dict[str, Any] | None,
) -> TranscriptRecord:
    if not task_id:
        raise ValueError("task_id is required")
    if not event_type:
        raise ValueError("event_type is required")
    record = TranscriptRecord(
        record_id=generate_id("t"),
        schema_version=TRANSCRIPT_SCHEMA_VERSION,
        task_id=task_id,
        stream=stream,
        event_type=event_type,
        timestamp=now_iso(),
        payload=dict(payload or {}),
    )
    append_jsonl(_stream_path(telemetry_root, task_id, stream), record.to_dict())
    return record


def _read_stream(
    telemetry_root: Path | str, task_id: str, stream: TranscriptStream
) -> list[TranscriptRecord]:
    path = _stream_path(telemetry_root, task_id, stream)
    if not path.exists():
        return []
    records: list[TranscriptRecord] = []
    for row in read_jsonl(path):
        records.append(
            TranscriptRecord(
                record_id=str(row.get("record_id") or generate_id("t")),
                schema_version=str(row.get("schema_version") or TRANSCRIPT_SCHEMA_VERSION),
                task_id=str(row.get("task_id") or task_id),
                stream=TranscriptStream(str(row.get("stream") or stream.value)),
                event_type=str(row.get("event_type") or "unknown"),
                timestamp=str(row.get("timestamp") or now_iso()),
                payload=dict(row.get("payload") or {}),
            )
        )
    return records
