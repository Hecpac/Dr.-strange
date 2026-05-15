# AI Lead Gen — Sierra Methodology Applied — Business Model & Operational Plan

date: 2026-05-15
status: draft
context: this doc lives in Dr.-strange because that is the active workspace today. When the work moves to the ai-lead-gen repo, copy this file to `~/Projects/ai-lead-gen/docs/business_model.md`.
related: project_ai_leadgen_repo, project_ai_leadgen_phase3_resume, project_ai_leadgen_cowork_resume (Dr. Strange memory)
references: Bret Taylor / Sierra public methodology, Sequoia Training Data podcast, Cheeky Pint episode, Sierra AI Agent Playbook

## Why this doc exists

The Sierra methodology (Bret Taylor's playbook) maps cleanly onto a vertical AI agent for home services. Four operating decisions follow from that mapping. This doc fixes those decisions so the next time work moves to `~/Projects/ai-lead-gen/`, the strategic frame is set and only execution remains.

Without this doc the risk is: we add features (better detection, more parcels, plugin install) without converging on a business model. With it, every feature ladders into a measurable outcome.

## Decision 1 — Outcome-based pricing

### Choice

Charge per **signed contract**, not per lead. Subsidize the funnel above the contract step.

### Funnel attribution

```
parcel_scored → lead_qualified → contact_attempted → tour_booked → contract_signed
   (free)         (free)            (free)           (free)         ($$$)
```

A contract is "signed" when the customer commits in writing to the installation. The home services contractor (our client) reports it back via a closed-loop webhook or weekly reconciliation file.

### Pricing math (pool installation example, Tarrant County)

- Average pool installation ticket in DFW: $45,000–$80,000 (verify by getting 3 quotes before launch).
- Lead → signed conversion rate target: 8% (industry baseline for paid lead gen in pool, will refine with data).
- Charge per signed contract: 10% of ticket = $4,500–$8,000 effective revenue per win.

To break even on a 100-parcel batch where 8 become signed contracts at $6,000 average comp: 8 × $6,000 = $48,000 gross. Burn allowance per batch (aerial costs + outbound + CRM): keep under $15,000 (≤ 30% gross margin allocated to ops, leaves runway for the 92 non-converting parcels).

### Hybrid fallback

If a client refuses pure outcome-based: $X fixed per qualified lead (e.g. $250) + 5% bonus on signed contract. More conservative. Still aligns incentives.

### Non-replicable until

We have 60–90 days of burn coverage. Without that runway, fall back to lead-only pricing for the first 30 contracts to validate conversion, then pivot to outcome-based.

## Decision 2 — Sub-vertical: pool installation first

### Choice

Launch with **pool installation / replacement** as the single served sub-vertical for the first 12 months. Expand horizontally only after $1M ARR or 12 months, whichever comes first.

### Why pool installation specifically

| Criterion | Pool installation | Roofing | HVAC replacement | Solar |
|---|---|---|---|---|
| Avg ticket | $45–80K | $15–30K | $8–15K | $20–40K |
| Aerial detection signal strength | High (mask presence + backyard area + grass condition) | Medium (shadow + texture) | Low (small unit + roof solar gain) | High (orientation + shadow) |
| Existing Dr. Strange / ai-lead-gen tooling fit | Strong (evf-sam already calibrated for pool masks) | Medium (would need different model) | Weak (door data needed) | Medium |
| Tarrant County TAM | ~150K homes with $200K+ valuation | All homes | All homes | Subset with south-facing roof |
| Competitive density | Medium | High | High | High |
| Owner buying cycle | Spring/early summer, 4–8 week deliberation | Reactive (storm/damage) | Reactive (failure) | Considered, 8–16 weeks |

Pool installation maximizes ticket × signal strength × existing tooling fit. Roofing is the obvious second sub-vertical when we expand.

### Definition of done for sub-vertical lock-in

- 50 signed contracts in pool installation across 1–3 contractors before opening a second sub-vertical.
- All operational artifacts (CRM templates, postcards, scripts, threshold rules) are pool-specific. We do not abstract prematurely.

## Decision 3 — Memory by property (not by owner)

### Choice

The persistent memory unit is **`parcel_id`**, not the homeowner. Owners change; parcels persist.

### Minimum viable schema

```sql
CREATE TABLE parcel_memory (
    parcel_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    county TEXT NOT NULL,

    -- Aerial history: each new aerial captures a snapshot.
    aerial_history JSONB NOT NULL DEFAULT '[]',
    -- shape: [{ts, source, mask_sqft, pool_score, factors: {...}}]

    -- Contact history: every outbound touchpoint logged.
    contact_history JSONB NOT NULL DEFAULT '[]',
    -- shape: [{ts, channel, owner_name, outcome, response_text}]

    -- Score history: each scoring pass logged for explorer review.
    score_history JSONB NOT NULL DEFAULT '[]',
    -- shape: [{ts, threshold, score, factors, model_version}]

    -- Owner changes (track sales, refis, name changes).
    owner_changes JSONB NOT NULL DEFAULT '[]',
    -- shape: [{ts, owner_prev, owner_new, source}]

    last_postcard_sent TIMESTAMPTZ,
    contract_status TEXT NOT NULL DEFAULT 'cold',
    -- enum: 'cold' | 'qualified' | 'contacted' | 'tour_booked' | 'signed' | 'lost' | 'optout'

    notes TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_parcel_status ON parcel_memory(contract_status);
CREATE INDEX idx_parcel_county ON parcel_memory(county);
CREATE INDEX idx_parcel_last_postcard ON parcel_memory(last_postcard_sent);
```

### Operational benefit (concrete examples)

- Agent decides whether to send a postcard. Reads `last_postcard_sent`. If >180 days ago → send. If <30 days ago → skip. If 30–180 days → A/B test.
- Owner-facing message references the prior touchpoint: "We sent you info about your pool 90 days ago — wanted to follow up." Cold outreach becomes warm.
- When ownership changes (capture via county deed records monthly), reset `contract_status` to `cold` but keep `aerial_history` and `score_history`. New owner = new lead, same parcel knowledge.

### Why not by owner

- Owners move (~7% of US homes change hands annually). Owner-keyed memory loses the parcel signal at every transition.
- Pool presence is a property fact, not an owner fact. The pool persists across ownership.
- B2B context: the contractor's installation territory is geographic, not demographic. Parcel-keyed memory matches.

## Decision 4 — Explorer lite (manual review, deferred ghostwriter)

### Choice

Implement Sierra's Explorer pattern as a **manual weekly review of logged failures**. Defer Ghostwriter (automated improvement loop) to phase 2 when annual revenue exceeds $1M.

### Implementation

```python
# scripts/explorer_lite.py
import json
import os
from datetime import datetime, timezone

FAILURE_LOG = "data/failures.jsonl"

def log_failure(parcel_id: str, stage: str, reason: str, sample: dict) -> None:
    """
    stage: one of {scoring, qualifying, contact, conversion}
    reason: short text or "ground_truth_disagrees"
    sample: arbitrary dict captured at point of failure
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "parcel_id": parcel_id,
        "stage": stage,
        "reason": reason,
        "sample": sample,
    }
    os.makedirs(os.path.dirname(FAILURE_LOG), exist_ok=True)
    with open(FAILURE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
```

### Weekly review ritual (30 minutes, every Friday)

1. Read the last 7 days of `data/failures.jsonl`.
2. Cluster failures by `stage` and `reason`.
3. Identify the top 1 pattern (>30% of failures).
4. Decide one of:
   - Add a heuristic to `aerial_pipeline.detect_for_parcel()` to catch the pattern.
   - Adjust the threshold for that pattern.
   - Update the outbound script if `stage=contact`.
5. Log the decision in `docs/explorer_log.md` with date, pattern, hypothesis, action taken.

### Promotion to Ghostwriter (automated)

When ARR > $1M AND we have 6 months of `failures.jsonl` AND we hire a second engineer: invest in automated A/B testing of heuristic variants. Until then, manual is faster and cheaper.

## Sequencing — what to do in the next ai-lead-gen session

This sequence assumes the next time Hector opens `~/Projects/ai-lead-gen/`.

1. **Close Phase 3 first** — do not skip.
   - Finish labeling the 20 Tarrant seed parcels in `data/seed/label.html`.
   - Move resulting `labels.json` into `data/seed/`.
   - Create `scripts/threshold_sweep.py` (sweep 0.001..0.50 over `pool_coverage`, report precision/recall/F1).
   - Apply optimal threshold as `FAL_POOL_MASK_THRESHOLD` default in `apps/api/services/aerial_detect.py`.
   - Commit. Acceptance: F1 > 0.7 on seed set.

2. **Lock sub-vertical** (decision 2) — create `docs/vertical_lock_pool_installation.md` in ai-lead-gen with the table from §Decision 2 and the success metric (50 signed contracts before second vertical).

3. **Migrate schema for parcel memory** (decision 3) — write `migrations/0001_parcel_memory.sql` with the schema from §Decision 3. Apply against local Postgres. Add ORM model in `apps/api/models/parcel_memory.py`.

4. **Wire failure log** (decision 4) — create `scripts/explorer_lite.py`. Add `log_failure()` calls into:
   - `aerial_pipeline.detect_for_parcel()` on detector error or zero-mask result.
   - `qualifying.score_parcel()` on threshold borderline cases.
   - `outbound.send_postcard()` on bounce or hard opt-out.

5. **Document pricing** (decision 1) — write `docs/business_model.md` in ai-lead-gen with the funnel attribution, math, and fallback from §Decision 1. This becomes the artifact you show to the first contractor pilot.

Each step is 1–3 hours of focused work. Total ≈ 1 day if Phase 3 calibration passes on first sweep. 2 days if threshold sweep requires expanding the seed set to 50 parcels.

## What NOT to do

- Do not install the Cowork plugin (per `project_ai_leadgen_cowork_resume` memory) until F1 > 0.7 on the seed set. The plugin amplifies whatever scoring is underneath; bad scoring × amplification = false-positive flood.
- Do not pitch outcome-based pricing to a real contractor until at least 30 of your own internally-tracked synthetic conversions are recorded. You need real-world variance data first.
- Do not abstract the schema to handle multiple sub-verticals on day one. Pool-specific fields are fine. Refactor when adding the second sub-vertical, not before.

## Open questions

- Pricing: 10% of ticket vs 8% vs 12% — needs first contractor conversation to anchor.
- Tarrant County deed record refresh cadence — monthly enough? Or need biweekly to catch ownership changes faster?
- Postcard send threshold (>180 days re-touch) — is this the right cadence for pool buyers? Industry benchmark TBD.
- Should `aerial_history` capture only most-recent + delta, or full history? Storage vs analysis tradeoff.

## Success criteria for this strategy (12 months out)

- 50 signed pool installation contracts attributed to ai-lead-gen across ≥3 contractor clients.
- Average revenue per signed contract: $4–8K (target $6K).
- Annualized ARR: ≥$300K at month 12.
- F1 on aerial pool detection: ≥0.85 (improving from 0.7 Phase 3 target).
- Failure log cluster size: top pattern <25% (proxy for system getting more general).
- One sub-vertical (pool) locked in; second sub-vertical (roofing) scoped but not yet active.

If these criteria are missed by month 12, the right move is to inspect Decision 2 (was pool the right vertical?) before doubting the methodology itself.
