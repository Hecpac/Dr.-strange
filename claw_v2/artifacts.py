from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    artifact_id: str
    task_id: str
    session_id: str
    objective: str
    mode: str
    planned_phases: list[str]
    created_at: float = field(default_factory=time.time)
    kind: str = "plan"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionArtifact:
    artifact_id: str
    task_id: str
    session_id: str
    status: str
    runtime: str
    provider: str | None = None
    model: str | None = None
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    kind: str = "execution"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerificationArtifact:
    artifact_id: str
    task_id: str
    session_id: str
    status: str
    summary: str
    pending_action: str = ""
    created_at: float = field(default_factory=time.time)
    kind: str = "verification"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OutcomeArtifact:
    artifact_id: str
    task_id: str
    session_id: str
    status: str
    summary: str
    error: str = ""
    verification_status: str = "unknown"
    created_at: float = field(default_factory=time.time)
    kind: str = "outcome"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JobArtifact:
    artifact_id: str
    task_id: str
    session_id: str
    lifecycle_status: str
    artifact_ids: list[str]
    created_at: float = field(default_factory=time.time)
    kind: str = "job"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_artifact_id(kind: str) -> str:
    return f"{kind}:{uuid.uuid4().hex[:12]}"


def append_lifecycle_artifacts(
    artifacts: dict[str, Any] | None,
    *items: PlanArtifact | ExecutionArtifact | VerificationArtifact | OutcomeArtifact | JobArtifact | dict[str, Any],
) -> dict[str, Any]:
    payload = dict(artifacts or {})
    lifecycle = dict(payload.get("lifecycle") or {})
    events = list(lifecycle.get("events") or [])
    artifact_ids = list(lifecycle.get("artifact_ids") or [])
    for item in items:
        artifact = item if isinstance(item, dict) else item.to_dict()
        kind = str(artifact.get("kind") or "artifact")
        artifact_id = str(artifact.get("artifact_id") or new_artifact_id(kind))
        artifact["artifact_id"] = artifact_id
        lifecycle[kind] = artifact
        events.append(
            {
                "kind": kind,
                "artifact_id": artifact_id,
                "created_at": artifact.get("created_at", time.time()),
            }
        )
        if artifact_id not in artifact_ids:
            artifact_ids.append(artifact_id)
    lifecycle["artifact_ids"] = artifact_ids
    lifecycle["events"] = events
    payload["lifecycle"] = lifecycle
    return payload


def planned_phases_for_mode(mode: str) -> list[str]:
    if mode == "coding":
        return ["research", "synthesis", "implementation", "verification"]
    return ["research", "synthesis", "verification"]
