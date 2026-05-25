---
name: engagement-audit
description: >
  Review the last 7-30 days of Hector's social posts across IG, LinkedIn,
  and X, identify which landed (above-median reach / engagement rate) and
  which flopped, attribute likely causes, and recommend the next 1-3
  concrete experiments. Use when Hector says "qué funcionó esta semana",
  "engagement audit", "cómo voy en redes", or "revisa mis últimos posts".
---

# Engagement Audit

Echo's reflection skill. Closes the loop between what was published and what worked, so the next post is informed not aspirational.

## Inputs requeridos

| Input | Source | Required |
|---|---|---|
| Window | Last 7d / 14d / 30d / custom date range | ✅ Sí (default 7d) |
| Platforms | `instagram` / `linkedin` / `x` / `all` | ✅ Sí (default all-active) |
| Insights data source | Native analytics via Chrome CDP (LI Page Analytics, IG Insights, X Premium analytics) OR Hector-provided screenshot/CSV | ✅ Sí |
| Hector's stated objective | Reach / DMs / link clicks / followers / qualified leads | Recomendado |

## Proceso

### 1. Gather data
Echo never fabricates engagement numbers. For each platform, get real data:
- **Instagram (`@pachanodesign` Creator)** — read native Insights via Chrome CDP (`/insights` on the post). If the account is <30 days old and Insights data is thin, say so honestly.
- **LinkedIn** — read post-level analytics from the post's "View analytics" dropdown via Chrome CDP.
- **X** — open `/analytics` on each tweet via Chrome CDP, or use the X Premium analytics dashboard if Hector subscribes.

If Chrome CDP can't reach the platform (auth required, rate-limited), ask Hector to drop the screenshot or CSV. Do NOT estimate.

### 2. Median anchor
For each platform, compute Hector's own rolling median (impressions / reach / engagement rate) over the window. "Landed" = above median. "Flopped" = below 70% of median. "Neutral" = in between.

This is critical for new accounts where industry benchmarks are useless — Hector's own median is the only honest baseline.

### 3. Attribute, don't speculate
For each landed and flopped post, identify the most likely driver:
- Hook pattern (which one from the 4)
- Posting time (was it the cadence Tuesday slot or off-schedule?)
- Format (reel / carousel / single image / text-only)
- Topic (founder-pain / tool-stack / contrarian-take / personal-story)
- External boost (was it cross-posted, did a bigger account engage, was it a reply to a trend?)

Echo can mark attribution as `high_confidence`, `plausible`, or `unclear`. Honesty beats false certainty.

### 4. Recommend 1-3 experiments
Each recommendation is testable in the next post or two:
- "Repeat the contrarian hook pattern that landed Tuesday's post → next reel uses same pattern with different topic."
- "Drop the carousel format — both carousels this week underperformed reels by 2x. Test 2 more reels before reintroducing."
- "Tuesday 9am CT outperformed Wednesday 3pm CT 3:1. Lock Tuesday 9am as the canonical slot for the next 4 weeks."

NOT recommendations: "post more", "engage more with comments", "use trending sounds". Vague = useless.

## Output format

```
## Engagement Audit — <window>  •  Platforms: <list>

### Median anchor (Hector's own baseline)
| Platform | Impressions median | Engagement rate median | Posts in window |
|---|---|---|---|
| IG | <N> | <%> | <N> |
| LinkedIn | <N> | <%> | <N> |
| X | <N> | <%> | <N> |

### 🟢 Landed (above median)
| Post | Platform | Hook | Format | Impressions | Engagement | Attribution |
|---|---|---|---|---|---|---|
| <title/url> | <p> | <pattern> | <format> | <N> | <%> | <reason / confidence> |

### 🔴 Flopped (<70% of median)
| Post | Platform | Hook | Format | Impressions | Engagement | Attribution |
|---|---|---|---|---|---|---|

### Patterns observed
- **Voice signal that consistently lands:** <one-liner>
- **Format/topic combo that consistently flops:** <one-liner>
- **Surprise:** <something Echo did not expect>

### Recommended experiments (1-3)
1. <concrete, testable>
2. <concrete, testable>
3. <concrete, testable>

### Open questions (honest)
- <if any data was missing>
- <if any attribution is unclear>
```

## Done criteria

- [ ] All numbers come from real analytics (Chrome CDP scrape OR Hector-provided source) — none fabricated.
- [ ] Median anchor is Hector's own, not an industry benchmark.
- [ ] Each landed/flopped post has an attribution + confidence level.
- [ ] Experiments are concrete and testable in the next 1-2 posts.
- [ ] If data was incomplete, the gap is named, not papered over.

## Errores comunes a evitar

- **Industry benchmarks.** "1% engagement rate is the IG average" is meaningless for a 0-30 day old account. Use Hector's own median.
- **Reading too much into single posts.** A single landed reel could be luck. Look for repeat patterns across 3+ posts before declaring a finding.
- **Vanity metrics.** Impressions without engagement = the algo tested distribution, audience didn't respond. Followers gained without engagement spike = bot follows. Mark these explicitly.
- **Recommending more output.** "Post more" is not a strategy. If the recommendation is volume, restart.
- **Ignoring follower quality.** 100 new followers from a viral reel might all be unqualified. Cross-check with profile-visit-to-follow ratio when available.

## Cadence-specific guidance

For the cadence compromise (1 LinkedIn post + 1 X thread per week, Tuesday 9-11am CT), the audit must specifically check:
- Did the Tuesday slot fire on time?
- Did the LinkedIn post outperform the X thread or vice versa?
- Did the bilingual variant (if any) split the audience or reach two distinct pools?

If 4 consecutive Tuesdays have underperformed the median, escalate the cadence experiment to Hector — maybe the slot is wrong, maybe the topic mix is wrong, maybe the cadence itself needs revisiting.
