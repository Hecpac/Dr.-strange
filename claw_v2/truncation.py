"""Standard truncation marker — Paso 4 of the 2026-06-11 fused audit.

Every site that drops content for budget reasons (page extraction,
inter-phase summaries, subprocess output) must say so with one format,
so the brain can tell "short result" from "cut result" and ask for the
full artifact instead of reasoning over silently missing data.
"""


def truncation_marker(kept: int, total: int) -> str:
    return f"[truncated: kept {kept} of {total} chars]"
