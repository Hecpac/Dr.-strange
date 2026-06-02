from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from claw_v2.sqlite_runtime import connect_runtime_sqlite
from claw_v2.telemetry import now_iso


GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_table TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id TEXT PRIMARY KEY,
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    evidence_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_ref
    ON graph_nodes(ref_table, ref_id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind
    ON graph_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src
    ON graph_edges(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst
    ON graph_edges(dst_id, kind);
"""


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    kind: str
    ref_table: str
    ref_id: str
    label: str
    created_at: str


@dataclass(frozen=True, slots=True)
class GraphEdge:
    id: str
    src_id: str
    dst_id: str
    kind: str
    evidence_ref: str | None
    created_at: str


@dataclass(slots=True)
class MaterializationResult:
    nodes_seen: int = 0
    edges_seen: int = 0
    sources: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes_seen": self.nodes_seen,
            "edges_seen": self.edges_seen,
            "sources": sorted(self.sources),
        }


class PropertyGraphProjection:
    """SQLite property graph projection over existing durable runtime records."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_runtime_sqlite(self.db_path)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self._conn.executescript(GRAPH_SCHEMA)
        self._conn.commit()

    def upsert_node(
        self,
        *,
        kind: str,
        ref_table: str,
        ref_id: str,
        label: str = "",
        created_at: str | None = None,
    ) -> GraphNode:
        node = GraphNode(
            id=node_id(ref_table, ref_id),
            kind=str(kind),
            ref_table=str(ref_table),
            ref_id=str(ref_id),
            label=str(label or ""),
            created_at=str(created_at or now_iso()),
        )
        self._conn.execute(
            """
            INSERT INTO graph_nodes (id, kind, ref_table, ref_id, label, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                ref_table = excluded.ref_table,
                ref_id = excluded.ref_id,
                label = excluded.label
            """,
            (node.id, node.kind, node.ref_table, node.ref_id, node.label, node.created_at),
        )
        self._conn.commit()
        return self.get_node(node.id) or node

    def upsert_edge(
        self,
        *,
        src_id: str,
        dst_id: str,
        kind: str,
        evidence_ref: str | None = None,
        created_at: str | None = None,
    ) -> GraphEdge:
        edge = GraphEdge(
            id=edge_id(src_id, dst_id, kind, evidence_ref),
            src_id=str(src_id),
            dst_id=str(dst_id),
            kind=str(kind),
            evidence_ref=str(evidence_ref) if evidence_ref is not None else None,
            created_at=str(created_at or now_iso()),
        )
        self._conn.execute(
            """
            INSERT INTO graph_edges (id, src_id, dst_id, kind, evidence_ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                src_id = excluded.src_id,
                dst_id = excluded.dst_id,
                kind = excluded.kind,
                evidence_ref = excluded.evidence_ref
            """,
            (edge.id, edge.src_id, edge.dst_id, edge.kind, edge.evidence_ref, edge.created_at),
        )
        self._conn.commit()
        return self.get_edge(edge.id) or edge

    def materialize(
        self,
        *,
        telemetry_root: Path | str | None = None,
        wiki_root: Path | str | None = None,
    ) -> MaterializationResult:
        result = MaterializationResult()
        if telemetry_root is not None:
            self._materialize_goals_and_claims(Path(telemetry_root), result)
        self._materialize_sqlite_runtime(result)
        if wiki_root is not None:
            self._materialize_wiki_pages(Path(wiki_root), result)
        return result

    def get_node(self, graph_id: str) -> GraphNode | None:
        row = self._conn.execute("SELECT * FROM graph_nodes WHERE id = ?", (graph_id,)).fetchone()
        return _node_from_row(row) if row is not None else None

    def get_edge(self, graph_id: str) -> GraphEdge | None:
        row = self._conn.execute("SELECT * FROM graph_edges WHERE id = ?", (graph_id,)).fetchone()
        return _edge_from_row(row) if row is not None else None

    def get_node_by_ref(self, ref_table: str, ref_id: str) -> GraphNode | None:
        row = self._conn.execute(
            "SELECT * FROM graph_nodes WHERE ref_table = ? AND ref_id = ?",
            (ref_table, str(ref_id)),
        ).fetchone()
        return _node_from_row(row) if row is not None else None

    def list_nodes(self, *, kind: str | None = None, limit: int = 100) -> list[GraphNode]:
        params: list[Any] = []
        where = ""
        if kind is not None:
            where = "WHERE kind = ?"
            params.append(kind)
        params.append(_bounded_limit(limit))
        rows = self._conn.execute(
            f"SELECT * FROM graph_nodes {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_node_from_row(row) for row in rows]

    def list_edges(self, *, kind: str | None = None, limit: int = 100) -> list[GraphEdge]:
        params: list[Any] = []
        where = ""
        if kind is not None:
            where = "WHERE kind = ?"
            params.append(kind)
        params.append(_bounded_limit(limit))
        rows = self._conn.execute(
            f"SELECT * FROM graph_edges {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_edge_from_row(row) for row in rows]

    def neighbors(self, graph_id: str, *, edge_kind: str | None = None, limit: int = 100) -> list[GraphNode]:
        params: list[Any] = [graph_id]
        kind_clause = ""
        if edge_kind is not None:
            kind_clause = "AND e.kind = ?"
            params.append(edge_kind)
        params.append(_bounded_limit(limit))
        rows = self._conn.execute(
            f"""
            SELECT n.*
            FROM graph_edges e
            JOIN graph_nodes n ON n.id = e.dst_id
            WHERE e.src_id = ?
              {kind_clause}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_node_from_row(row) for row in rows]

    def _materialize_goals_and_claims(self, telemetry_root: Path, result: MaterializationResult) -> None:
        from claw_v2.evidence_ledger import load_claims
        from claw_v2.goal_contract import load_goals

        result.sources.add("goal_contract")
        result.sources.add("evidence_ledger")
        for goal in load_goals(telemetry_root):
            self._count_node(
                result,
                self.upsert_node(
                    kind="goal",
                    ref_table="goal_contract",
                    ref_id=goal.goal_id,
                    label=goal.objective,
                    created_at=goal.created_at,
                ),
            )
            if goal.parent_goal_id:
                parent = self.upsert_node(
                    kind="goal",
                    ref_table="goal_contract",
                    ref_id=goal.parent_goal_id,
                    label=goal.parent_goal_id,
                )
                self._count_node(result, parent)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=parent.id,
                        dst_id=node_id("goal_contract", goal.goal_id),
                        kind="parent_goal",
                        evidence_ref=f"goal_contract:{goal.goal_id}",
                    ),
                )

        for claim in load_claims(telemetry_root):
            claim_node = self.upsert_node(
                kind="evidence",
                ref_table="evidence_ledger",
                ref_id=claim.claim_id,
                label=claim.claim_text,
                created_at=claim.created_at,
            )
            self._count_node(result, claim_node)
            goal_node = self.upsert_node(
                kind="goal",
                ref_table="goal_contract",
                ref_id=claim.goal_id,
                label=claim.goal_id,
            )
            self._count_node(result, goal_node)
            self._count_edge(
                result,
                self.upsert_edge(
                    src_id=goal_node.id,
                    dst_id=claim_node.id,
                    kind="has_evidence",
                    evidence_ref=f"evidence_ledger:{claim.claim_id}",
                ),
            )
            for dependency in claim.depends_on:
                dep_node = self.upsert_node(
                    kind="evidence",
                    ref_table="evidence_ledger",
                    ref_id=dependency,
                    label=dependency,
                )
                self._count_node(result, dep_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=claim_node.id,
                        dst_id=dep_node.id,
                        kind="depends_on",
                        evidence_ref=f"evidence_ledger:{claim.claim_id}",
                    ),
                )
            for ref in claim.evidence_refs:
                ref_key = f"{ref.kind}:{ref.ref}"
                ref_node = self.upsert_node(
                    kind="evidence_ref",
                    ref_table="evidence_ref",
                    ref_id=ref_key,
                    label=ref_key,
                    created_at=ref.captured_at,
                )
                self._count_node(result, ref_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=claim_node.id,
                        dst_id=ref_node.id,
                        kind="supported_by",
                        evidence_ref=ref_key,
                    ),
                )

    def _materialize_sqlite_runtime(self, result: MaterializationResult) -> None:
        self._materialize_tasks(result)
        self._materialize_observe_events(result)
        self._materialize_task_outcomes(result)
        self._materialize_facts(result)

    def _materialize_tasks(self, result: MaterializationResult) -> None:
        if not self._table_exists("agent_tasks"):
            return
        result.sources.add("task_ledger")
        rows = self._conn.execute(
            "SELECT task_id, objective, created_at FROM agent_tasks ORDER BY updated_at ASC"
        ).fetchall()
        for row in rows:
            self._count_node(
                result,
                self.upsert_node(
                    kind="task",
                    ref_table="agent_tasks",
                    ref_id=str(row["task_id"]),
                    label=str(row["objective"] or row["task_id"]),
                    created_at=str(row["created_at"] or now_iso()),
                ),
            )

    def _materialize_observe_events(self, result: MaterializationResult) -> None:
        if not self._table_exists("observe_stream"):
            return
        result.sources.add("observe_stream")
        rows = self._conn.execute(
            """
            SELECT id, timestamp, event_type, trace_id, root_trace_id, span_id,
                   parent_span_id, job_id, artifact_id, payload
            FROM observe_stream
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            event_node = self.upsert_node(
                kind="observe_event",
                ref_table="observe_stream",
                ref_id=str(row["id"]),
                label=str(row["event_type"]),
                created_at=str(row["timestamp"] or now_iso()),
            )
            self._count_node(result, event_node)
            payload = _json_object(row["payload"])
            task_id = _first_text(payload.get("task_id"), row["job_id"])
            if task_id:
                task_node = self.upsert_node(
                    kind="task",
                    ref_table="agent_tasks",
                    ref_id=task_id,
                    label=task_id,
                )
                self._count_node(result, task_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=task_node.id,
                        dst_id=event_node.id,
                        kind="emitted_event",
                        evidence_ref=f"observe_stream:{row['id']}",
                    ),
                )
            goal_id = _first_text(payload.get("goal_id"))
            if goal_id:
                goal_node = self.upsert_node(
                    kind="goal",
                    ref_table="goal_contract",
                    ref_id=goal_id,
                    label=goal_id,
                )
                self._count_node(result, goal_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=goal_node.id,
                        dst_id=event_node.id,
                        kind="emitted_event",
                        evidence_ref=f"observe_stream:{row['id']}",
                    ),
                )
            self._materialize_observe_span(row, event_node, result)

    def _materialize_observe_span(self, row: sqlite3.Row, event_node: GraphNode, result: MaterializationResult) -> None:
        span_id = _first_text(row["span_id"])
        if not span_id:
            return
        span_node = self.upsert_node(
            kind="observe_span",
            ref_table="observe_span",
            ref_id=span_id,
            label=span_id,
            created_at=str(row["timestamp"] or now_iso()),
        )
        self._count_node(result, span_node)
        self._count_edge(
            result,
            self.upsert_edge(
                src_id=span_node.id,
                dst_id=event_node.id,
                kind="contains_event",
                evidence_ref=f"observe_stream:{row['id']}",
            ),
        )
        parent_span_id = _first_text(row["parent_span_id"])
        if parent_span_id:
            parent = self.upsert_node(
                kind="observe_span",
                ref_table="observe_span",
                ref_id=parent_span_id,
                label=parent_span_id,
            )
            self._count_node(result, parent)
            self._count_edge(
                result,
                self.upsert_edge(
                    src_id=parent.id,
                    dst_id=span_node.id,
                    kind="parent_span",
                    evidence_ref=f"observe_stream:{row['id']}",
                ),
            )

    def _materialize_task_outcomes(self, result: MaterializationResult) -> None:
        if not self._table_exists("task_outcomes"):
            return
        result.sources.add("task_outcomes")
        rows = self._conn.execute(
            "SELECT id, task_id, description, created_at FROM task_outcomes ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            outcome_node = self.upsert_node(
                kind="task_outcome",
                ref_table="task_outcomes",
                ref_id=str(row["id"]),
                label=str(row["description"] or row["task_id"]),
                created_at=str(row["created_at"] or now_iso()),
            )
            self._count_node(result, outcome_node)
            task_id = _first_text(row["task_id"])
            if task_id:
                task_node = self.upsert_node(
                    kind="task",
                    ref_table="agent_tasks",
                    ref_id=task_id,
                    label=task_id,
                )
                self._count_node(result, task_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=task_node.id,
                        dst_id=outcome_node.id,
                        kind="has_outcome",
                        evidence_ref=f"task_outcomes:{row['id']}",
                    ),
                )
        if not self._table_exists("outcome_entity_edges"):
            return
        edge_rows = self._conn.execute(
            "SELECT outcome_id, entity_tag FROM outcome_entity_edges ORDER BY outcome_id ASC"
        ).fetchall()
        for row in edge_rows:
            outcome_node = self.upsert_node(
                kind="task_outcome",
                ref_table="task_outcomes",
                ref_id=str(row["outcome_id"]),
                label=str(row["outcome_id"]),
            )
            entity_node = self.upsert_node(
                kind="knowledge_entity",
                ref_table="outcome_entity_edges",
                ref_id=str(row["entity_tag"]),
                label=str(row["entity_tag"]),
            )
            self._count_node(result, outcome_node)
            self._count_node(result, entity_node)
            self._count_edge(
                result,
                self.upsert_edge(
                    src_id=outcome_node.id,
                    dst_id=entity_node.id,
                    kind="mentions_entity",
                    evidence_ref=f"outcome_entity_edges:{row['outcome_id']}:{row['entity_tag']}",
                ),
            )

    def _materialize_facts(self, result: MaterializationResult) -> None:
        if not self._table_exists("facts"):
            return
        result.sources.add("knowledge_facts")
        rows = self._conn.execute("SELECT id, key, value, entity_tags, created_at FROM facts ORDER BY id ASC").fetchall()
        for row in rows:
            fact_node = self.upsert_node(
                kind="knowledge_fact",
                ref_table="facts",
                ref_id=str(row["id"]),
                label=str(row["key"]),
                created_at=str(row["created_at"] or now_iso()),
            )
            self._count_node(result, fact_node)
            for tag in _json_list(row["entity_tags"]):
                entity_node = self.upsert_node(
                    kind="knowledge_entity",
                    ref_table="memory_entity_tag",
                    ref_id=tag,
                    label=tag,
                )
                self._count_node(result, entity_node)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=fact_node.id,
                        dst_id=entity_node.id,
                        kind="mentions_entity",
                        evidence_ref=f"facts:{row['id']}",
                    ),
                )

    def _materialize_wiki_pages(self, wiki_root: Path, result: MaterializationResult) -> None:
        wiki_dir = wiki_root / "wiki" if (wiki_root / "wiki").is_dir() else wiki_root
        if not wiki_dir.is_dir():
            return
        result.sources.add("wiki")
        pages = sorted(wiki_dir.glob("*.md"))
        slugs = {path.stem for path in pages}
        for path in pages:
            text = path.read_text(encoding="utf-8")
            page_node = self.upsert_node(
                kind="wiki_page",
                ref_table="wiki_page",
                ref_id=path.stem,
                label=_wiki_title(text, fallback=path.stem),
                created_at=now_iso(),
            )
            self._count_node(result, page_node)
            for target_slug in sorted(_wiki_links(text) & slugs):
                target = self.upsert_node(
                    kind="wiki_page",
                    ref_table="wiki_page",
                    ref_id=target_slug,
                    label=target_slug,
                )
                self._count_node(result, target)
                self._count_edge(
                    result,
                    self.upsert_edge(
                        src_id=page_node.id,
                        dst_id=target.id,
                        kind="wiki_link",
                        evidence_ref=f"wiki_page:{path.stem}",
                    ),
                )

    def _table_exists(self, table_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _count_node(result: MaterializationResult, _: GraphNode) -> None:
        result.nodes_seen += 1

    @staticmethod
    def _count_edge(result: MaterializationResult, _: GraphEdge) -> None:
        result.edges_seen += 1


def node_id(ref_table: str, ref_id: str) -> str:
    digest = hashlib.sha256(f"{ref_table}:{ref_id}".encode("utf-8")).hexdigest()[:24]
    return f"n_{digest}"


def edge_id(src_id: str, dst_id: str, kind: str, evidence_ref: str | None = None) -> str:
    digest = hashlib.sha256(
        f"{src_id}:{dst_id}:{kind}:{evidence_ref or ''}".encode("utf-8")
    ).hexdigest()[:24]
    return f"e_{digest}"


def _node_from_row(row: sqlite3.Row) -> GraphNode:
    return GraphNode(
        id=str(row["id"]),
        kind=str(row["kind"]),
        ref_table=str(row["ref_table"]),
        ref_id=str(row["ref_id"]),
        label=str(row["label"] or ""),
        created_at=str(row["created_at"]),
    )


def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
    return GraphEdge(
        id=str(row["id"]),
        src_id=str(row["src_id"]),
        dst_id=str(row["dst_id"]),
        kind=str(row["kind"]),
        evidence_ref=str(row["evidence_ref"]) if row["evidence_ref"] is not None else None,
        created_at=str(row["created_at"]),
    )


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _wiki_title(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            return stripped.split(":", 1)[1].strip().strip('"') or fallback
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def _wiki_links(text: str) -> set[str]:
    slugs: set[str] = set()
    for match in re.finditer(r"\[\[([^\]|#]+)", text):
        slug = match.group(1).strip()
        if slug:
            slugs.add(slug)
    return slugs
