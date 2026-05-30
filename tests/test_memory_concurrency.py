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

            self.assertEqual(errors, [], msg=f"concurrent access raised: {[repr(e) for e in errors[:3]]}")


if __name__ == "__main__":
    unittest.main()
