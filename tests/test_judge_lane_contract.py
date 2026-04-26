from __future__ import annotations

import ast
from pathlib import Path
import unittest


class JudgeLaneContractTests(unittest.TestCase):
    def test_product_code_passes_evidence_pack_to_judge_lane(self) -> None:
        offenders: list[str] = []
        for path in sorted(Path("claw_v2").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                lane = _keyword_value(node, "lane")
                if not isinstance(lane, ast.Constant) or lane.value != "judge":
                    continue
                if _keyword_value(node, "evidence_pack") is None:
                    offenders.append(f"{path}:{node.lineno}")

        self.assertEqual(offenders, [])


def _keyword_value(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


if __name__ == "__main__":
    unittest.main()
