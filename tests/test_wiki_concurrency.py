from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.wiki import WikiService


class WikiConcurrencyTests(unittest.TestCase):
    def test_concurrent_embedding_mutation_and_iteration(self) -> None:
        # 2026-05-29 audit (HIGH): self._embeddings was mutated (search re-embed,
        # _index_page_embedding) without the lock while other paths iterate it
        # (_find_duplicate, quality_report) -> "dictionary changed size during
        # iteration" RuntimeError under concurrency.
        #
        # Reproduction: a Python-level loop over a large, growing dict (the
        # _find_duplicate cosine loop) while writers keep adding keys. With no
        # lock/snapshot the reader's loop raises mid-iteration.
        with tempfile.TemporaryDirectory() as tmpdir:
            wiki = WikiService(router=None, wiki_root=Path(tmpdir))
            # Wide iteration window: pre-seed many embeddings directly.
            for i in range(3000):
                wiki._embeddings[f"seed{i}"] = [0.01] * 128

            errors: list[BaseException] = []
            stop = threading.Event()
            start = threading.Barrier(4)

            def writer(n: int) -> None:
                start.wait()
                try:
                    for i in range(900):
                        if stop.is_set():
                            return
                        wiki._index_page_embedding(f"w{n}-{i}", f"content {n} {i}")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    stop.set()

            def reader() -> None:
                start.wait()
                try:
                    for _ in range(150):
                        if stop.is_set():
                            return
                        wiki._find_duplicate("some content to compare against")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    stop.set()

            threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
            threads += [threading.Thread(target=reader) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], msg=f"concurrent wiki access raised: {[repr(e) for e in errors[:3]]}")


if __name__ == "__main__":
    unittest.main()
