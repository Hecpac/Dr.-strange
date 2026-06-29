# F5 Knowledge Loop Core

## Spec

- Goal: make the wiki research loop inspectable and useful as durable agent knowledge, following the LLM Wiki pattern of raw evidence first, candidate queue second, compiled wiki pages last.
- Scope: persist auto-research candidates, expose read-only wiki quality/research commands, and return per-source/per-item scrape diagnostics so zero-ingest runs are explainable.
- Out of scope: changing brain retrieval behavior, enforcing AI-news validation, adding new external scrapers, or touching F2 RuntimeDb/F3 leases/F4 browser orchestration.
- Expected files:
  - `claw_v2/wiki.py`
  - `claw_v2/wiki_handler.py`
  - `claw_v2/scheduled_background_jobs.py`
  - `tests/test_wiki.py`
  - `tests/test_wiki_handler.py`
  - `tests/test_scheduled_background_jobs.py`
- Acceptance criteria:
  - `WikiService.auto_research()` writes a bounded candidate queue artifact with status metadata and does not create wiki pages.
  - Scheduled job summaries include bounded candidate previews while avoiding full raw candidate bodies.
  - `WikiService.auto_scrape_sources()` returns enough diagnostics to explain scraped-but-not-ingested runs.
  - `/wiki quality` and `/wiki research` provide concise read-only operator visibility.

## Verification

- Focused commands:
  - `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_handler.py tests/test_scheduled_background_jobs.py -q`
- Broader gate if focused tests touch scheduler/runtime integration:
  - `.venv/bin/python -m pytest tests/test_runtime.py tests/test_semantic_scheduler.py -q`

## Stop Conditions

- If the implementation requires live Firecrawl/OpenAI calls, stop and split the PR.
- If candidate persistence needs schema changes in `data/claw.db`, stop and split into an F2/F5 coordinated PR.
- If changing the brain prompt/retrieval becomes necessary, stop and do it as a follow-up PR after this loop is observable.
