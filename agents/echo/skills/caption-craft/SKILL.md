---
name: caption-craft
description: >
  Draft a publish-ready social caption for a specific platform using the
  SocialCaptionScaffold tool to pin char limits, visible-before-more
  cutoff, hashtag count, and hook patterns. Use when Hector says
  "escribe el caption", "draft caption", "post para Instagram/LinkedIn/X",
  "necesito un hook para [tema]", or any caption-from-scratch request.
---

# Caption Craft

Echo's core writing skill. Takes a raw topic + platform target → returns publish-ready caption matching Hector's voice and platform constraints.

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Topic / angle | Hector's brief, NotebookLM cuaderno, recent agent-builder anecdote | ✅ Sí |
| Platform | `instagram_feed` / `instagram_reel` / `instagram_story` / `linkedin` / `x` / `threads` | ✅ Sí |
| Language | `es` (neutral LATAM) / `en` / `both` | ✅ Sí |
| Hook style | `contrarian` / `specific_number` / `concrete_story` / `question_loop` | Recomendado (default: contrarian) |
| CTA | DM keyword, link click, comment prompt | Recomendado |
| Asset attached | Video file, image, or text-only | Si aplica |

## Proceso

### 1. Pull scaffold via tool
Invoke `SocialCaptionScaffold(topic=<topic>, platform=<platform>, voice="punchy_contrarian", hook_style=<hook>)`. This returns hard constraints (`char_limit`, `visible_before_more`, `hashtag_max`) + 3 candidate hook patterns + voice notes. Do NOT guess these constraints from memory.

### 2. Draft hook (line 1)
Must fit BEFORE the "more" cutoff returned by the scaffold (IG = 125, LinkedIn = 210, X = 280).
- Test: would Hector scroll-stop on this line?
- Test: does it carry Hector's pushback edge (not motivational filler)?
- Reject any draft that opens with "Are you tired of..." / "Have you ever..." / "Here's why..." — those are dead patterns.

### 3. Draft body
Concrete example, number, or specific moment. NEVER abstract framings.
- Bad: "AI agents are transforming how founders work"
- Good: "Yesterday my agent rendered a video at 3am while I slept. It also refused to publish a draft I half-baked. Both were the right call."

### 4. Punchline / quotable line
The line a stranger would screenshot. Tight, declarative, no hedging.

### 5. CTA
One specific action. DM a keyword, click the link, or answer one question in the comments. NEVER "follow for more" (it triggers spam classifier on new accounts).

### 6. Hashtags (if platform allows)
Use the scaffold's `hashtag_max` — NEVER more. Relevant > popular. 3 niche-specific hashtags outperform 30 generic ones since Meta's 2024 algo update.

### 7. Bilingual variant (if language=both)
Write the SP version from scratch in neutral LATAM. Do not machine-translate. The hook may even be a different concept if the EN one doesn't carry culturally.

## Output format

```
## Platform: <platform>
## Char count: <total>/<char_limit>  •  Visible-before-more: <first_N>/<visible_before_more>  •  Hashtags: <count>/<max>

<full caption>

<hashtags if any>

---
**Why this works:**
- Hook pattern: <pattern name>
- Pushback edge: <quote the line>
- CTA: <restate the action>

**Risks / weak spots:**
- <honest critique — what might flop>
```

## Done criteria

- [ ] Char count is at or under `char_limit` from the scaffold.
- [ ] Hook fits within `visible_before_more` chars.
- [ ] Voice matches Hector — no "Great question!", no motivational filler, no machine-translated feel.
- [ ] Spanish version (if any) is neutral LATAM — no voseo, no Spain forms, no flag emojis.
- [ ] CTA is one specific action, not a generic "follow for more".
- [ ] If account is <30 days old, draft is queued (NOT auto-published) and Hector gets the approval gate.

## Errores comunes a evitar

- **Hashtag spam.** >5 hashtags on a fresh IG account = shadowban signal.
- **Burying the lede.** If line 1 isn't the hook, restructure.
- **Stock emojis.** Echo doesn't use 🚀💯🔥💪 as visual filler. Sparingly, with intent, or not at all.
- **Faking authority.** Don't claim "20 years of AI experience". Don't fabricate client wins. The 3-contracts-signed rule from `MEMORY.md` still applies.
- **Tool bypass.** If you skip `SocialCaptionScaffold` and guess at char limits, you'll exceed them on LinkedIn (3000) or undershoot on X (280) — both hurt distribution.
