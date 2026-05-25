---
name: reply-craft
description: >
  Draft a reply to a specific incoming comment on IG, LinkedIn, or X using
  the SocialReplyScaffold tool for tone + length + structure guidance. Use
  when Hector forwards a comment screenshot, says "qué le respondo a este",
  "draft reply", "responde a este comment", or "what should I say back?".
---

# Reply Craft

Echo's comment-reply skill. Takes one incoming comment + platform + desired tone → returns a reply Hector can paste (or that Echo can submit via Tier 3 approval).

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Incoming comment | Verbatim text | ✅ Sí |
| Platform | `instagram_feed` / `instagram_reel` / `linkedin` / `x` / `threads` | ✅ Sí |
| Desired tone | `warm` / `expert` / `playful` / `direct` | Default: warm |
| Commenter profile (optional) | Handle, follower count, bio one-liner | Recomendado for high-value replies |
| Hector's goal | Build relationship / answer briefly / qualify lead / shut down hostility | Recomendado |

## Proceso

### 1. Read the comment for the implied question or emotion
Most comments aren't just text — they're a tone signal. Is the commenter:
- **Curious** (asking how/why)? → expert tone, answer the question with one number or specific.
- **Validating** ("This is great!")? → warm tone, mirror back one specific detail of what they liked.
- **Pushing back** (disagreement, skepticism)? → expert tone, acknowledge the disagreement, hold the position with one reason.
- **Hostile / off-topic / political**? → STOP. Echo drafts a single calm one-line reply OR recommends ignore-and-block. Hector decides.

### 2. Pull scaffold via tool
Invoke `SocialReplyScaffold(incoming_comment=<text>, platform=<platform>, tone=<tone>)`. This returns target length, tone guidance, and 4-step structure. Do NOT skip — different platforms have different attention spans and the scaffold pins the right target.

### 3. Draft following the scaffold's 4-step structure
1. Acknowledge specifically — quote a word or phrase from the comment.
2. Add value — one concrete detail, number, follow-up question, or example.
3. Stop. No padding, no engagement-bait questions like "what do you think?".
4. Match the language the commenter wrote in.

### 4. Char-count check against target
- `warm` → 150 chars target
- `expert` → 200 chars target
- `playful` → 120 chars target
- `direct` → 100 chars target

Going long signals "I'm trying too hard". Going much shorter than target is fine.

### 5. Voice check
- No "Great question!" / "Love this!" / "100%" — Hector doesn't talk like that.
- Use the commenter's name only if they used Hector's first. Otherwise neutral.
- No emoji unless the commenter used one first and matching is natural.

## Output format

```
**Reply (paste-ready):**

<the reply text>

**Char count:** <N>/<target>
**Tone applied:** <tone>
**Why this works:**
- <one-line reason>

**Optional alternates:**
1. <shorter variant>
2. <slightly different angle>

**Risk flag:** <none / "commenter handle is suspicious" / "topic could escalate">
```

## Done criteria

- [ ] Reply is at or under target char count.
- [ ] Quotes or references one specific word/phrase from the original comment.
- [ ] No filler openers ("Great question!", "Thanks for sharing!").
- [ ] Language matches the commenter's language (or Spanish neutral LATAM if defaulting).
- [ ] If platform = LinkedIn and reply involves committing to a meeting / sending a doc, draft includes the next concrete step.

## Hostile / risky comment protocol

If the incoming comment is:
- A personal attack on Hector or his family → DO NOT draft an engagement reply. Output: *"Recommendation: ignore. Report if it crosses harassment threshold."*
- Political bait → Output: *"Recommendation: ignore. Engaging amplifies."*
- Direct competitor smear → Output: *"Recommendation: ignore unless it spreads. Don't dignify."*
- Spam / scam DM in comments → Output: *"Recommendation: report + block."*

Echo never engages hostility on Hector's behalf.

## Publish protocol

Drafting a reply is Tier 1 (autonomous). **Hitting Submit on the platform is Tier 3** — requires Hector's explicit go in the same turn or via active capability grant.

When submitting (after approval):
- **LinkedIn comment** — click the explicit "Comment" button. Cmd+Enter does NOT submit. Then verify the comment appears under selector `article.comments-comment-entity` matching the drafted text.
- **X reply** — Cmd+Enter works. Then verify via selector `article[data-testid="tweet"]` for the reply text.
- **Instagram comment** — submit via the post button. Then verify the comment appears in the comment list with Hector's handle.

If the verification selector returns nothing, the reply did NOT publish. Do NOT report success.
