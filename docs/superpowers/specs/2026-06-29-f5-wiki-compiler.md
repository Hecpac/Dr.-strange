# F5 Wiki Compiler

## Spec

- Goal: close the next Knowledge Loop step by compiling researched raw evidence into wiki pages through an explicit quality gate.
- Scope: process bounded `researched` candidates, generate one summary wiki page from each raw evidence artifact, update candidate status, and include compile counters in scheduled job summaries.
- Out of scope: brain retrieval changes, AI-news validation, multi-page concept expansion, external scraper changes, F2 RuntimeDb schema changes, and production restarts.
- Expected files:
  - `claw_v2/wiki.py`
  - `claw_v2/main.py`
  - `claw_v2/scheduled_background_jobs.py`
  - `tests/test_wiki.py`
  - `tests/test_scheduled_background_jobs.py`
- Acceptance criteria:
  - `WikiService.compile_researched_candidates()` writes wiki pages only from existing raw evidence.
  - Compiled pages must pass a local quality gate before write: safe filename, frontmatter, and `sources` includes the raw source slug.
  - Candidates move from `researched` to `compiled`, or to `compile_blocked` with a reason.
  - Scheduled `wiki_research` can process one research candidate and one compile candidate per off-tick run.

## Verification

- Focused commands:
  - `.venv/bin/python -m pytest tests/test_wiki.py tests/test_scheduled_background_jobs.py -q`
- Broader gate:
  - `.venv/bin/python -m pytest tests/test_runtime.py tests/test_semantic_scheduler.py -q`
  - `.venv/bin/python -m pytest tests/test_architecture_invariants.py -q`

## Stop Conditions

- If compilation requires changing brain prompt retrieval, split a follow-up PR.
- If compilation requires database schema changes, split a coordinated F2/F5 PR.
- If review asks for live source fetching in the compiler, reject that scope: the compiler consumes only raw evidence.
