"""Shadowed by claw_v2/verification/ package — kept only as a tombstone.

Initial F1 draft of the success-condition contract was written here on
2026-05-26 but the existing `claw_v2/verification/` package wins at import
time. The real implementation now lives at
`claw_v2/verification/success_contract.py` and is re-exported from
`claw_v2/verification/__init__.py`.

This file is not imported by Python (package precedence). It exists only
because the runtime sandbox does not allow `rm`. Safe to delete in a
follow-up commit.
"""
