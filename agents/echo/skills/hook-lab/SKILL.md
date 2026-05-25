---
name: hook-lab
description: >
  Generate 5 hook candidates for a specific topic and platform, drawn from
  the four named hook patterns (contrarian, specific_number, concrete_story,
  question_loop) plus a wildcard. Use when Hector says "necesito hooks
  para [tema]", "dame opciones de línea 1", "qué hook abrimos con",
  "scroll-stopper para [reel/post]", or any hook-brainstorm request.
---

# Hook Lab

Echo's hook-development skill. Generates 5 candidate first-lines for a topic, each from a different pattern, with a one-line "why this works" so Hector can pick.

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Topic / angle | Hector's brief or the bigger caption brief | ✅ Sí |
| Platform | Determines char ceiling for line 1 | ✅ Sí |
| Hector's lived detail | Specific moment, number, agent quote, client result | Strongly recommended |
| Target audience | Founders / designers / ML practitioners / SMB owners | Recomendado |

## Proceso

### 1. Pull hook patterns from the runtime
The `social_media.py` HOOK_PATTERNS dict defines four named patterns:
- **contrarian** — "Everyone says X. They are wrong because..."
- **specific_number** — "I tested N tools. Only one survived."
- **concrete_story** — "At 3am on a Tuesday, this happened..."
- **question_loop** — "Why does X happen? Three reasons..."

Use these as scaffolds, not as fill-in-the-blanks templates. The output must feel like Hector wrote it.

### 2. Generate one hook per pattern (4) + one wildcard (5)
Each hook:
- Fits in the platform's visible-before-more window (IG = 125, LinkedIn = 210, X = 280).
- Carries Hector's pushback edge. No motivational filler. No "Here's what I learned about..."
- Uses one concrete element — a number, a moment, a quote — not abstractions.
- The wildcard is freeform: a hook that doesn't fit any pattern but Echo thinks might land harder.

### 3. Stress-test each hook
For each draft, ask:
- Would a stranger scroll-stop on this?
- Does it imply a payoff in the body, or does it spoil it?
- Could it be misread as a generic gurupost?

Discard and rewrite any hook that fails any of the three.

### 4. Rank by Echo's call
Echo picks one as the recommended hook, with reasoning. Hector overrides freely.

## Output format

```
## Hook Lab — <topic>
**Platform:** <platform>  •  **Visible ceiling:** <N> chars

### 1. Contrarian
> "<hook line>"
- **Chars:** <N>/<ceiling>
- **Why it works:** <one line>

### 2. Specific number
> "<hook line>"
- **Chars:** <N>/<ceiling>
- **Why it works:** <one line>

### 3. Concrete story
> "<hook line>"
- **Chars:** <N>/<ceiling>
- **Why it works:** <one line>

### 4. Question loop
> "<hook line>"
- **Chars:** <N>/<ceiling>
- **Why it works:** <one line>

### 5. Wildcard
> "<hook line>"
- **Chars:** <N>/<ceiling>
- **Why it works:** <one line>

---
**Echo's pick:** #<N> — <one-line reasoning>
**Risk to flag:** <if any — e.g. "Hook #2 claims a number Hector hasn't verified yet">
```

## Done criteria

- [ ] Exactly 5 hooks, one per pattern + 1 wildcard.
- [ ] Each fits the visible-before-more ceiling.
- [ ] Each uses one concrete detail (number / moment / quote), not abstractions.
- [ ] Echo's pick is justified in one line.
- [ ] No fabricated stats. If a number is in the hook, Echo confirms Hector can defend it.
- [ ] Spanish hooks are neutral LATAM. No voseo / Spain forms.

## Errores comunes a evitar

- **Recycled gurupost.** "If you're not doing X, you're losing." → dead.
- **Spoiling the payoff.** "Here's the 5-step framework for AI agents:" → no scroll, the title already gave it away.
- **Stat-stuffing without source.** "AI agents save 73% of founder time" → if Hector can't cite where this number comes from, kill it.
- **Bilingual sloppiness.** Don't translate "Everyone says X" → "Todos dicen X" — instead, write a Spanish hook that fits the Spanish-speaking founder's ear ("La verdad incómoda sobre los AI agents...").
- **All 5 hooks sounding the same.** If pattern variety isn't audible, restart.
