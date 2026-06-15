from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore


class MemoryStoreConcurrencyTests(unittest.TestCase):
    def test_concurrent_reads_and_writes_do_not_raise(self) -> None:
        # 2026-05-29 audit (HIGH): MemoryStore shares one sqlite3 connection
        # (check_same_thread=False) across threads; reads bypassed self._lock,
        # so a read interleaved with a write could raise sqlite3.ProgrammingError
        # ("Recursive use of cursors not allowed") / OperationalError or read
        # corrupt rows. Stress read+write concurrency and assert it stays clean.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "claw.db")
            session = "tg-concurrency"
            for i in range(20):
                store.store_message(session, "user", f"seed {i}")

            errors: list[BaseException] = []
            start = threading.Barrier(8)

            def reader() -> None:
                start.wait()
                try:
                    for _ in range(200):
                        store.get_recent_messages(session, limit=20)
                        store.count_messages(session)
                        store.get_session_state(session)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def writer(n: int) -> None:
                start.wait()
                try:
                    for i in range(200):
                        store.store_message(session, "assistant", f"w{n}-{i}")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=reader) for _ in range(4)]
            threads += [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(
                errors, [], msg=f"concurrent access raised: {[repr(e) for e in errors[:3]]}"
            )


class MergeActiveObjectTests(unittest.TestCase):
    # AM-STATEWR/M16 (2026-06-12): the read-copy-write caller pattern lost
    # updates when a worker thread and a chat turn interleaved. The atomic
    # merge re-reads under the store lock so concurrent writers compose.
    def test_concurrent_merges_compose_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "claw.db")
            session = "tg-merge"
            start = threading.Barrier(2)

            def worker_side() -> None:
                start.wait()
                for i in range(50):
                    store.merge_active_object(session, {"active_task": {"step": i}})

            def chat_side() -> None:
                start.wait()
                for i in range(50):
                    store.merge_active_object(session, {"pending_tool_approval": {"n": i}})

            threads = [threading.Thread(target=worker_side), threading.Thread(target=chat_side)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            active_object = store.get_session_state(session)["active_object"]
            self.assertEqual(active_object["active_task"], {"step": 49})
            self.assertEqual(active_object["pending_tool_approval"], {"n": 49})

    def test_merge_supports_remove_and_state_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "claw.db")
            store.merge_active_object("s1", {"a": 1, "b": 2})
            store.merge_active_object(
                "s1", {}, remove=("a",), pending_action="retry", verification_status="pending"
            )
            state = store.get_session_state("s1")
            self.assertEqual(state["active_object"], {"b": 2})
            self.assertEqual(state["pending_action"], "retry")
            self.assertEqual(state["verification_status"], "pending")


if __name__ == "__main__":
    unittest.main()
