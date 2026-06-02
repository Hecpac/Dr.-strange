from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.evidence_ledger import EvidenceRef, record_claim
from claw_v2.goal_contract import create_goal
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.property_graph import PropertyGraphProjection
from claw_v2.task_ledger import TaskLedger


class PropertyGraphSchemaTests(unittest.TestCase):
    def test_creates_projection_tables_without_replacing_existing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ledger = TaskLedger(db_path)
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="keep existing task table",
                runtime="coordinator",
            )

            graph = PropertyGraphProjection(db_path)

            tables = {
                row["name"]
                for row in graph._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("agent_tasks", tables)
            self.assertIn("graph_nodes", tables)
            self.assertIn("graph_edges", tables)

    def test_upserts_nodes_and_edges_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            graph = PropertyGraphProjection(Path(tmpdir) / "claw.db")

            source = graph.upsert_node(
                kind="goal",
                ref_table="goal_contract",
                ref_id="g_1",
                label="Ship feature",
            )
            same_source = graph.upsert_node(
                kind="goal",
                ref_table="goal_contract",
                ref_id="g_1",
                label="Ship feature v2",
            )
            target = graph.upsert_node(
                kind="evidence",
                ref_table="evidence_ledger",
                ref_id="c_1",
                label="Tests passed",
            )
            graph.upsert_edge(src_id=source.id, dst_id=target.id, kind="has_evidence")
            graph.upsert_edge(src_id=source.id, dst_id=target.id, kind="has_evidence")

            self.assertEqual(source.id, same_source.id)
            self.assertEqual(len(graph.list_nodes()), 2)
            self.assertEqual(len(graph.list_edges()), 1)
            self.assertEqual(graph.get_node_by_ref("goal_contract", "g_1").label, "Ship feature v2")


class PropertyGraphMaterializationTests(unittest.TestCase):
    def test_materializes_existing_goal_evidence_task_observe_and_wiki_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "claw.db"
            telemetry_root = root / "telemetry"
            wiki_root = root / "wiki-root"
            wiki_dir = wiki_root / "wiki"
            wiki_dir.mkdir(parents=True)
            (wiki_dir / "alpha.md").write_text("# Alpha\n\nLinks to [[beta]].", encoding="utf-8")
            (wiki_dir / "beta.md").write_text("---\ntitle: Beta Page\n---\n\nBack to [[alpha]].", encoding="utf-8")

            observe = ObserveStream(db_path)
            ledger = TaskLedger(db_path, observe=observe)
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="Build graph projection",
                runtime="coordinator",
                status="running",
            )
            observe.emit(
                "llm_decision",
                trace_id="trace-1",
                span_id="span-1",
                parent_span_id="root-span",
                job_id="task-1",
                payload={"task_id": "task-1"},
            )

            memory = MemoryStore(db_path)
            memory.store_task_outcome(
                task_type="coding",
                task_id="task-1",
                description="Graph projection task",
                approach="materialize existing rows",
                outcome="success",
                lesson="Keep projection separate from sources.",
                tags=["property_graph"],
            )
            memory.store_fact(
                "graph.fact",
                "Property graph is projected, not primary storage.",
                source="test",
                entity_tags=["property_graph"],
            )

            goal = create_goal(telemetry_root, objective="Add graph projection")
            claim = record_claim(
                telemetry_root,
                goal_id=goal.goal_id,
                claim_text="Focused tests passed",
                claim_type="fact",
                evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest tests/test_property_graph.py -q")],
                verification_status="verified",
                confidence=1.0,
            )

            graph = PropertyGraphProjection(db_path)
            result = graph.materialize(telemetry_root=telemetry_root, wiki_root=wiki_root)

            self.assertIn("goal_contract", result.sources)
            self.assertIn("evidence_ledger", result.sources)
            self.assertIn("task_ledger", result.sources)
            self.assertIn("observe_stream", result.sources)
            self.assertIn("wiki", result.sources)

            goal_node = graph.get_node_by_ref("goal_contract", goal.goal_id)
            claim_node = graph.get_node_by_ref("evidence_ledger", claim.claim_id)
            task_node = graph.get_node_by_ref("agent_tasks", "task-1")
            event_nodes = graph.list_nodes(kind="observe_event")
            wiki_alpha = graph.get_node_by_ref("wiki_page", "alpha")
            entity = graph.get_node_by_ref("outcome_entity_edges", "property_graph")

            self.assertIsNotNone(goal_node)
            self.assertIsNotNone(claim_node)
            self.assertIsNotNone(task_node)
            self.assertTrue(event_nodes)
            self.assertIsNotNone(wiki_alpha)
            self.assertIsNotNone(entity)

            assert goal_node is not None
            assert claim_node is not None
            assert task_node is not None
            assert wiki_alpha is not None
            goal_neighbors = graph.neighbors(goal_node.id, edge_kind="has_evidence")
            task_neighbors = graph.neighbors(task_node.id, edge_kind="emitted_event")
            wiki_neighbors = graph.neighbors(wiki_alpha.id, edge_kind="wiki_link")

            self.assertEqual([node.id for node in goal_neighbors], [claim_node.id])
            self.assertTrue(any(node.kind == "observe_event" for node in task_neighbors))
            self.assertEqual(wiki_neighbors[0].ref_id, "beta")

    def test_materialization_is_incremental_and_does_not_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "claw.db"
            telemetry_root = root / "telemetry"
            goal = create_goal(telemetry_root, objective="Idempotent projection")
            record_claim(
                telemetry_root,
                goal_id=goal.goal_id,
                claim_text="Claim one",
                claim_type="fact",
                evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest")],
                verification_status="verified",
            )

            graph = PropertyGraphProjection(db_path)
            graph.materialize(telemetry_root=telemetry_root)
            first_counts = _graph_counts(graph)
            graph.materialize(telemetry_root=telemetry_root)
            second_counts = _graph_counts(graph)

            self.assertEqual(first_counts, second_counts)


def _graph_counts(graph: PropertyGraphProjection) -> tuple[int, int]:
    node_count = graph._conn.execute("SELECT COUNT(*) AS count FROM graph_nodes").fetchone()["count"]
    edge_count = graph._conn.execute("SELECT COUNT(*) AS count FROM graph_edges").fetchone()["count"]
    return int(node_count), int(edge_count)


if __name__ == "__main__":
    unittest.main()
