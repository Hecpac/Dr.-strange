---
name: linkedin-2026-dom-knowledge
description: LinkedIn 2026 profile edit DOM map + working CDP patterns + known blockers. Use before any LinkedIn profile automation to skip re-discovery.
metadata:
  type: reference
---

# LinkedIn 2026 — Profile Edit DOM Knowledge Map

**Source:** discovered via Chrome CDP on Hector's profile (`linkedin.com/in/hector-pachano-32b922205`) on 2026-05-25. Profile language = English. ~10 scripts in `scripts/_linkedin_*.py` + ~30 screenshots in `artifacts/job_search/` document the iteration.

## TL;DR — what works vs what doesn't

| Section | CDP edit possible? | Path |
|---|---|---|
| **Headline** | ✅ YES | Deep link `/edit/intro/` → modal opens → headline is `<div contenteditable="true">` (NOT input/textarea) → set innerText + dispatch InputEvent |
| **Basic info** (First/Last/Industry/Country/Postal) | ✅ Likely YES | Same `/edit/intro/` modal, fields are `<input>` |
| **Position (Add new)** | 🔄 95% (form opens, fill works, **Year dropdown blocker**) | Click "Add section" → expand "Core" → click "Add position" → form modal opens |
| **About edit** | ❌ NO known path | LinkedIn 2026 removed inline edit pencil for About. 7 deep-link URLs tested = all 404. About section markup has only "... more" expand button, no edit |
| **Skills (Add)** | ⏳ Pattern expected same as Position | Add section → Core → Add skill |
| **Featured (Edit)** | ⏳ Untested | "Featured overflow menu" button exists (aria-label confirmed) — opens dropdown with likely Edit option |

## Working selectors (DO use)

### Headline (proven working)

```python
# Open /edit/intro/ in CDP-attached existing tab OR new page with 7s wait
# The Headline field is a contenteditable div without id/aria-label.
# Identify it by current value match or by `isContentEditable: true` + value length > 30
# Set via:
el.innerText = new_value
el.dispatchEvent(new InputEvent('input', {bubbles: true}))
el.dispatchEvent(new Event('change', {bubbles: true}))
# Then click Save button anywhere in [role="dialog"]
```

Working canonical script: `scripts/_linkedin_edit_v5.py`

### Add Position (form opens, dates blocker)

```python
# Step 1: Click "Add section" — element with text "Add section" (any tag, walk up to BUTTON/A)
# Step 2: Click "Core" (div with text starting with "Core") to expand category
# Step 3: Click "Add position" (button/a with exact text "Add position")
# Form opens with these field placeholders for identification:
#   - Title: placeholder == "Ex: Retail Sales Manager"
#   - Company: placeholder == "Ex: Microsoft"
#   - Location: placeholder == "Ex: London, United Kingdom"
#   - Description: aria-label == "Description, maximum 2,000 characters"
#   - Start Month/Year: <select> with <label for=id>Month/Year</label>
# Fill via type+autocomplete pick for company/location, React setter for description, select_option for dates
```

Direct deep link after first interaction: `linkedin.com/in/me/edit/forms/position/new/` (doesn't auto-open in fresh tab — needs the Add section flow first time, then URL is set in router)

Canonical script: `scripts/_linkedin_position_full.py`. Known blocker: Year select label match needs `if "year" in lbl.lower()` instead of `if lbl == "Year"` (because LinkedIn renders `Year*` with asterisk for required).

## Selectors that DON'T work (DO NOT retry)

| Selector attempted | Why it fails |
|---|---|
| `button[aria-label='Edit intro']` | LinkedIn 2026 has zero `aria-label` containing "edit" on profile view |
| `/details/about/`, `/edit/about/`, `/edit/summary/`, `/edit/about-summary/`, `/edit/forms/profile/summary/` | All 404 |
| `/edit/work-experience/`, `/add-edit/POSITION/`, `/add-edit/position/` | Don't auto-open modal in fresh tab |
| Section pencil button via `section button[aria-label*='Edit']` | About section has only "... more" expand button, no edit pencil |
| CSS escape: `#:r1d:` | LinkedIn uses `:rN:` IDs that need `[id='...']` escaped form |

## Critical 2026 patterns

1. **Headline is `<div contenteditable="true">`, NOT `<input>` or `<textarea>`.** This was the breakthrough — search for `[contenteditable="true"]` in DOM scans.
2. **React-friendly setters required for value:** plain `el.value = x` doesn't trigger React update. Must use `Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, x)` + dispatch input + change events.
3. **Native `<select>` elements need Playwright's `select_option(label="...")`** — NOT React setter (React doesn't update view from setter alone for selects).
4. **Modal lifecycle:** opening `/edit/intro/` in a fresh tab DOES auto-open the modal after ~5-7s wait. Other deep links don't.
5. **Existing tab reuse:** if a modal is open in an existing Chrome tab, attaching via Playwright to that tab is more reliable than re-opening.
6. **"Add section" only shows secondary categories by default** (Recommended: featured, licenses, projects, courses, recommendations). Core sections (position, education, skills) require expanding the "Core" accordion first.

## Next session plan (Path B kickoff)

**Estimated time:** 30-60 min focused session.

1. **Fix Position year blocker** (1-line script change: `_linkedin_year_only.py` with `"year" in lbl.lower()`). ~5 min.
2. **Add Skills** via same Add section → Core → Add skill pattern. Skills modal uses a search/multi-select widget — needs separate exploration. ~15 min.
3. **Edit Featured** via "Featured overflow menu" → dropdown → Edit. ~10 min.
4. **About**: try ONE more approach — click the section heading itself (some LinkedIn versions enable inline edit on H2 click). If fails, deliver About via paste manual. ~10 min.

## Knowledge artifacts on disk

- `scripts/_linkedin_*.py` — 10 scripts iterating on the DOM
- `artifacts/job_search/` — 30+ screenshots, JSON diagnostics, body text dump
- `artifacts/job_search/section_buttons_report.json` — overflow menu coords
- `artifacts/job_search/linkedin_final_edits_2026-05-25.md` — paste-ready texts (msg 11113)
- `artifacts/job_search/linkedin_gap_analysis_2026-05-25.md` — strategic analysis (msg 11110)

Updated 2026-05-25.
