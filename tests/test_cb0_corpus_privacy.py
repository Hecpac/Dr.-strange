from __future__ import annotations

import json
import unittest
from pathlib import Path

# CB0 evidence gate: the redacted routing corpus is derived from prod
# observe_stream. This test is the privacy invariant — it fails if the corpus
# ever regains an identifier or a user-content field, so the ADR evidence can
# be committed without leaking session ids, approval/task ids, or message text.

_REPO = Path(__file__).resolve().parents[1]
CORPUS = _REPO / "docs/adr/CB0-computer-vs-browser-routing/evidence-corpus.json"

# The redaction contract: sample payloads may carry ONLY these structural
# routing keys. Everything else (session_id, turn_id, approval_id, task_id,
# instruction_hash, current_url, text_preview, text_len, …) is dropped.
STRUCTURAL_ALLOWLIST = frozenset(
    {
        "route",
        "selected_route",
        "reason",
        "selected_handler",
        "handler",
        "backend",
        "status",
        "mode",
        "verification_status",
        "task_status",
        "tools_count",
        "captured",
        "matched_pattern",
        "explicit_command",
    }
)


class CB0CorpusPrivacyTests(unittest.TestCase):
    def _corpus(self) -> dict:
        self.assertTrue(CORPUS.exists(), f"evidence corpus missing at {CORPUS}")
        return json.loads(CORPUS.read_text(encoding="utf-8"))

    def test_corpus_parses_and_declares_window(self) -> None:
        corpus = self._corpus()
        self.assertIn("_meta", corpus)
        self.assertIn("window_start", corpus["_meta"])
        self.assertIn("caveat", corpus["_meta"])  # low-N honesty stays in the record

    def test_samples_carry_only_allowlisted_structural_keys(self) -> None:
        corpus = self._corpus()
        for event_type, samples in corpus.get("samples", {}).items():
            for sample in samples:
                leaked = set(sample) - STRUCTURAL_ALLOWLIST
                self.assertEqual(
                    leaked,
                    set(),
                    f"{event_type} sample leaked non-structural keys: {sorted(leaked)}",
                )

    def test_no_identifiers_leak_in_samples_or_counts(self) -> None:
        corpus = self._corpus()
        # Only inspect the data sections, not _meta (which names the redacted
        # fields as documentation).
        blob = json.dumps(corpus.get("samples", {})) + json.dumps(corpus.get("counts", {}))
        self.assertNotRegex(blob, r"tg-\d+", "telegram/session id leaked into the corpus")
        self.assertNotRegex(
            blob, r"\b[0-9a-f]{16}\b", "a 16-hex id (turn/approval/task/hash) leaked"
        )
        self.assertNotIn("text_preview", blob)


if __name__ == "__main__":
    unittest.main()
