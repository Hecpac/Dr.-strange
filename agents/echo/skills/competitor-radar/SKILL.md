---
name: competitor-radar
description: >
  Scrape a public Instagram profile via Chrome CDP to extract followers,
  bio lines, recent post captions, and classify the hook pattern of each
  caption. Use when Hector asks "qué hace [handle]", "compite contra
  nosotros", "investiga este perfil", or "competidores con >20K followers"
  in a niche. Read-only — never logs in, never engages.
---

# Competitor Radar

Echo's competitor-research skill. Scrapes a public IG profile via Chrome CDP and returns header stats + bio + recent caption hooks classified by pattern.

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Handle | IG handle (with or without `@`) | ✅ Sí |
| CDP URL | Chrome CDP endpoint | Default: `http://localhost:9250` |
| Recent post count | How many recent posts to scrape captions from | Default: 6, max recommended: 12 |
| Watchlist tag | Optional label to organize multi-account research | Recomendado |

## Pre-flight check

Chrome CDP must be available at the runtime endpoint. Verify with the runtime capability context (`Chrome CDP: available`). If unavailable, return a clear failure — do NOT pretend to have scraped.

## Proceso

### 1. Invoke the tool
`SocialCompetitorResearch(handle=<handle>, cdp_url="http://localhost:9250", recent_post_count=<N>)`.

Tool returns:
- `found` (bool)
- `followers` / `following` / `posts` (int counts, parsed from `og:description`)
- `display_name`
- `bio_text` + `bio_lines`
- `recent_post_captions[]`
- `observed_hook_patterns[]` (auto-classified: contrarian / question / specific_number / concrete_story / other)
- `error` (string or null)

### 2. Validate the scrape
- If `found = false` → handle is wrong, account is private, or IG blocked anonymous scraping. Report honestly. Do NOT fabricate.
- If `followers` is `None` but `found = true` → IG hid the count via login wall. Note the gap, don't invent a number.
- If `recent_post_captions` is empty → either the account has no posts, or the meta-description scrape failed. Note which.

### 3. Pattern read
For each caption, the tool already classified the hook. Echo then does the synthesis:
- Which hook pattern dominates? (e.g. 5/6 captions are `contrarian` → this competitor's voice is pushback-heavy)
- What's the caption length range? Short (<100) means hooks-first strategy, long (>1000) means storytelling.
- Are there recurring CTAs? (newsletter, DM keyword, link in bio).
- Bio structure: emoji-bullet pattern, credential-dense, single-line-mission?

### 4. Strategic read for Hector
Tie back to Hector's own positioning:
- Does this competitor occupy the same niche, or adjacent?
- What gap can Hector own that this competitor doesn't?
- Should Hector study this account (steal a hook pattern) or avoid it (don't sound like them)?

## Output format

```
## Competitor: @<handle>

| Field | Value |
|---|---|
| Followers | <N> |
| Posts | <N> |
| Display name | <name> |
| Bio | <bio_text> |
| Hook pattern dominant | <pattern> (<count>/<total>) |

### Recent captions analyzed (<N>)
1. [<pattern>] <first 80 chars>
2. [<pattern>] <first 80 chars>
...

### Strategic read for Hector
- **Niche overlap:** <direct / adjacent / unrelated>
- **What they own:** <one-liner>
- **Gap Hector can take:** <one-liner>
- **Verdict:** <study daily / monitor monthly / ignore>
```

## Done criteria

- [ ] Tool was actually invoked (not synthesized from memory of past competitors).
- [ ] Numbers are quoted from the tool output, not estimated.
- [ ] If `error` is set, the error is surfaced — no fake-success.
- [ ] Strategic read is concrete, not generic ("they post about marketing" is useless; "they always lead with a one-line proposition, not a hook story" is useful).

## Errores comunes a evitar

- **Bypassing the tool.** Echo's memory of `@danmartell` from 5 days ago is stale. Always re-scrape.
- **Pretending the scrape worked when it didn't.** Honest failure > fabricated data.
- **Conflating screenshots with scraping.** The tool returns text. Screenshots are a separate Chrome CDP action.
- **Logging in.** This tool is anonymous-only. Logging in mixes Hector's session into competitor research and triggers IG rate-limiting on his account.

## Batch mode

For >5 handles at once, invoke the tool once per handle, then synthesize in a single ranked table sorted by followers descending. Mark `found=false` accounts explicitly so Hector doesn't confuse them with low-traction accounts.
