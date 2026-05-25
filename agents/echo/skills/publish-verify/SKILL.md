---
name: publish-verify
description: >
  Verify that a social post, comment, or reply actually appeared on the
  target platform after submission, using artifact-specific selectors —
  NEVER `body.innerText` substring matching. Use after any Tier 3 publish
  action (Post button click, Tweet button, Comment submit). The selectors
  per surface are canonical and live in MEMORY.md.
---

# Publish Verify

Echo's safety net. Closes the loop on "did this actually publish?" — a question that has burned Hector before. Memory rule from 2026-05-22: never declare publish success from `body.innerText` containing the text. The text can live in an unsubmitted editor, a draft, or a modal.

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Surface | `instagram_post` / `instagram_reel` / `instagram_comment` / `linkedin_post` / `linkedin_comment` / `linkedin_reply` / `x_tweet` / `x_reply` / `threads_post` | ✅ Sí |
| Submitted text | The exact text Echo just submitted | ✅ Sí |
| Expected author handle | Hector's handle on that platform | ✅ Sí |
| CDP URL | Default `http://localhost:9250` | Default |
| Pre-submit screenshot path (optional) | Useful for diffing before/after | Recomendado |

## Canonical selectors (memory: feedback_verify_publish_actually_succeeded)

| Surface | Selector that PROVES publish |
|---|---|
| LinkedIn post | `[data-urn*="activity:"]` containing the submitted text |
| LinkedIn comment | `article.comments-comment-entity` containing the submitted text + Hector's handle |
| LinkedIn reply (nested) | `article.comments-comment-entity[data-test-id*="reply"]` |
| X tweet | `article[data-testid="tweet"]` containing the submitted text + Hector's handle |
| X reply | Same as tweet — replies use the same tweet article container |
| Instagram post | Top of the profile grid at `/<handle>/` shows the new post (verify by navigation, not by overlay) |
| Instagram reel | `/reels/` tab on the profile shows the new reel as first item |
| Instagram comment | Comment list on the post URL contains the submitted text + Hector's handle |
| Threads post | The post appears in the user's profile `/threads` feed |

If the canonical selector returns zero matches → the publish FAILED. Period. Do not soften this with "may have published" — report the failure plainly.

## Proceso

### 1. Wait briefly after submission
Most platforms need 1-3 seconds for the DOM to update after a successful Submit. Wait ~2s before verifying. Do NOT wait 30s — if the post isn't there in a few seconds, it didn't post.

### 2. Run the canonical selector via Chrome CDP
For LinkedIn / X / Threads: query the selector on the current page.
For Instagram: navigate to `/<handle>/` (or `/p/<id>` if the URL was returned post-submit) and check the canonical location.

### 3. Match against submitted text
- The element must contain the submitted text (or its first 80 chars for long captions where IG truncates).
- The element must reference Hector's handle (so we don't confuse Hector's reply with the original post's text containing the same words).

### 4. If verified → record + report
- Pull the canonical URL from the verified element (LinkedIn: `data-urn` → activity URL; X: `data-permalink` → tweet URL; IG: `/p/<id>` or `/reel/<id>`).
- Take a screenshot of the verified element.
- Persist `social_publish_verified` event with surface, URL, screenshot path, submitted text, timestamp.

### 5. If NOT verified → retry submit OR fail loudly
- **First fail:** retry the submit ONCE with the alternate submit method.
  - LinkedIn comment failed via Cmd+Enter? → retry via explicit "Comment" button click. **MEMORY:** LinkedIn comment editor does NOT accept Cmd+Enter as submit — button click is the only working path.
  - X reply failed via button click? → retry via Cmd+Enter (X DOES accept Cmd+Enter).
  - LinkedIn post modal failed via Cmd+Enter? → retry via the "Post" button click at coords (1010, 482) in viewport 1400×950 — canonical coords from memory.
- **Second fail:** do NOT retry again. Report the failure with: the surface, what selector returned zero, the alternate submit attempted, a fresh screenshot, and a recommended Hector-side fallback (e.g. "open the platform manually, the draft is still in the editor at <url>").

## Output format

```
## Publish Verify — <surface>

**Status:** ✅ verified / 🔴 FAILED
**Submitted text (first 80 chars):** <text>
**Canonical selector used:** <selector>

### If verified:
- **Public URL:** <url>
- **Screenshot:** <path>
- **Author handle confirmed:** <handle>
- **Timestamp:** <ISO timestamp>

### If FAILED:
- **What the selector returned:** <count = 0 / N matches but none containing submitted text>
- **First submit method tried:** <button click / Cmd+Enter>
- **Retry method tried:** <the alternate>
- **Retry result:** <still failed / not attempted>
- **Recommended fallback:** <one concrete next step Hector can take>
- **Fresh screenshot:** <path>
```

## Done criteria

- [ ] Canonical selector for the surface was used — NOT `body.innerText` substring matching.
- [ ] Match required both submitted text + Hector's handle on the same element.
- [ ] If selector returned zero, the failure is reported plainly without softening language.
- [ ] On verified publish, the public URL was captured.
- [ ] On failed publish, exactly one retry was attempted with the alternate submit method, and the result was reported honestly.

## Hard rules

- **NEVER use `body.innerText`-contains-text as a publish proof.** This is the rule that originated the whole skill.
- **NEVER claim "may have published" or "appears to have published".** Either the canonical selector matched, or it didn't.
- **NEVER swallow the failure to keep the conversation moving.** A failed publish is a hard stop until Hector decides the fallback.
- **NEVER retry more than once.** Echo retries once with the alternate submit method, then escalates.

## Related memory anchors

- `feedback_verify_publish_actually_succeeded` (2026-05-22) — origin incident on Andrew Ng post comment that failed silently.
- `project_weekly_content_cadence` (2026-05-22) — canonical LinkedIn UI coords for the Post button.
- `feedback_linkedin_easyapply_works` (2026-05-15) — proves Chrome CDP submit works on native LinkedIn forms when verification is selector-specific.
