# Storyboard — Premium Home Design

**Format:** 1920×1080
**Audio:** ElevenLabs voiceover + underscore + SFX
**VO direction:** Male, calm authority, measured pace. Think architecture firm partner presenting to a client — confident, precise, unhurried. Slight warmth. No hype.
**Style basis:** DESIGN.md — dark charcoal + warm cream palette, signature red accents, Plus Jakarta Sans + IBM Plex Mono
**Duration:** ~26.5 seconds, 5 beats

**Underscore:** Minimal ambient. Warm sustained pad with subtle sub-bass. Sits under VO, never competes. Gentle swell during stats beat, resolves on final chord at CTA.

---

## Asset Audit

| Asset | Type | Assign to Beat | Role |
|-------|------|----------------|------|
| render-3d-poster.jpg | Hero image | Beat 1 | Sketch-to-render reveal — the signature brand visual |
| image.jpg | Photo | Beat 3 | Modern home with pool — stats background |
| og-image.jpg | Photo | Beat 2 | White brick house — brand credibility |
| svgs/logo-6.svg through logo-29.svg | SVG logos | Beat 5 | PHD brand mark |
| favicon.ico | Icon | SKIP | Too small |
| scroll-000.png | Screenshot | Beat 1 | Dark kitchen hero reference |
| scroll-031.png | Screenshot | Beat 3 | Rockcliff Residence — project evidence |
| scroll-048.png | Screenshot | Beat 3 | Pueblo Residence — project variety |

---

## BEAT 1 — THE REVEAL (0.00–3.20s)

**VO:** "From sketch to standing structure."

**Concept:** The video opens on the architectural sketch — raw pencil lines, blueprints, the left side of the render-3d-poster image. Then the frame splits and reveals the photorealistic render on the right, as if the building materializes from the drawing. This is the brand's core promise in one visual: we turn concepts into reality.

**Visual:** Full-bleed render-3d-poster.jpg starts cropped to show ONLY the sketch half (left side). At 0.8s, a vertical wipe sweeps right-to-left revealing the photorealistic render. The wipe edge glows with signature red (#CB2131) — a 2px line that leads the reveal. As the render appears, warm light blooms subtly behind the building. "FROM SKETCH" in IBM Plex Mono (all-caps, tracked, #F4EEEE) sits upper-left, "TO STANDING STRUCTURE" reveals with the wipe on the right.

**Mood:** Cinematic title sequence. Architectural precision. The moment a client sees their dream become real.

**Assets:**
- `assets/render-3d-poster.jpg` — full-bleed, split-reveal animation
- PHD logo mark — fades in bottom-right at 3s, small, red (#CB2131)

**Animation choreography:**
- Sketch half: slight slow zoom 1→1.02, 5s
- Red wipe line: SWEEPS right-to-left, 1.5s, power2.inOut
- Render reveal: follows wipe with 0.1s delay
- "FROM SKETCH" types on at 0.3s, monospace, 0.4s
- "TO STANDING STRUCTURE" SLIDES in from right at wipe completion, 0.5s power2.out
- Logo mark fades in at 3s, 0.3s

**Depth layers:** BG: sketch image with subtle paper texture. MG: reveal wipe with red edge glow. FG: monospace text labels.

**SFX:** Pencil scratch texture as sketch appears. Clean architectural "ping" as wipe completes.

**Transition OUT:** Velocity-matched upward — y:-150, blur:30px, 0.33s power2.in

---

## BEAT 2 — THE PROMISE (3.20–11.00s)

**VO:** "Premium Home Design builds custom homes across Dallas-Fort Worth. One team. One contract. Design through handover."

**Concept:** The brand declares what it is — cleanly, confidently. We're in the warm cream world of the site's content sections. The og-image house sits centered with text framing the value proposition. Three short phrases stamp in like architectural specs on a drawing.

**Visual:** Warm cream (#F7F5F0) background. `og-image.jpg` centered at 60% width with subtle Ken Burns zoom 1→1.03. "PREMIUM HOME DESIGN" in Plus Jakarta Sans 700 at top, dark charcoal (#21201B), scales in from 95%→100%. Below the image, three phrases appear in staggered sequence: "One team." / "One contract." / "Design through handover." — each in IBM Plex Mono, all-caps, with red (#CB2131) bullet markers.

**Mood:** Clean workspace. Confident but not boastful. An architect's portfolio, not a billboard.

**Assets:**
- `assets/og-image.jpg` — centered with thin 1px border at rgba(33,32,27,0.1)
- PHD logo mark — top-left corner, small

**Animation choreography:**
- Background: instant warm cream
- "PREMIUM HOME DESIGN": SCALES in 95%→100%, opacity 0→1, 0.5s power2.out
- House image: fades in 0→1, 0.4s, starts 0.3s delay
- "One team.": STAMPS in at 6.0s, y:20→0, 0.25s
- "One contract.": STAMPS in at 6.5s
- "Design through handover.": STAMPS in at 7.0s
- Each phrase gets a thin red underline that DRAWS left→right, 0.3s

**Depth layers:** BG: warm cream fill. MG: house photo with subtle shadow. FG: typography and red accent lines.

**SFX:** Soft paper settle on each stamp.

**Transition OUT:** Blur through — blur:20px, 0.3s → entry blur:20px→0, 0.25s power3.out

---

## BEAT 3 — THE PROOF (11.00–16.40s)

**VO:** "Eight hundred thirty five projects completed. Ninety four percent on time."

**Concept:** The dark world returns. We're in the project gallery section — cinematic, immersive. Two massive stats counter-animate while project photography slides behind them at reduced opacity. This is the flex beat — evidence, not claims.

**Visual:** Deep black (#1C1C1C) background. Two project photos (scroll-031 Rockcliff, scroll-048 Pueblo) alternate as Ken Burns pans at 30% opacity behind the stats. "835+" counts up from 0 in Plus Jakarta Sans 700, 120px, white (#FFFFFF). Below it: "PROJECTS COMPLETED" in IBM Plex Mono, red (#CB2131). Second stat "94%" counts up from 0, same treatment. "ON-TIME COMPLETION" label below.

**Mood:** Cinematic authority. The numbers speak. Think architectural photography exhibition — dramatic lighting, confident silence between stats.

**Assets:**
- `captures/pachano-design/screenshots/scroll-031.png` — Rockcliff Residence, Ken Burns slow drift right
- `captures/pachano-design/screenshots/scroll-048.png` — Pueblo Residence, crossfade at 3s
- `assets/image.jpg` — modern home with pool, subtle background layer

**Animation choreography:**
- Background photos: slow drift, 0.5px/frame, 30% opacity, blur(2px)
- "835+": COUNTS UP from 0 over 2s, power2.out easing on final digits
- "PROJECTS COMPLETED": types on monospace at count completion, 0.4s
- Pause 0.5s
- "94%": COUNTS UP from 0 over 1.5s
- "ON-TIME COMPLETION": types on at count completion
- Red accent line between the two stats, DRAWS left→right, full width

**Depth layers:** BG: blurred project photography. MG: thin red divider line. FG: large counter numbers + monospace labels.

**SFX:** Low sub-bass pulse on each counter start. Quiet metronome tick during count.

**Transition OUT:** Zoom through — scale:1→1.2, blur:20px, 0.2s power3.in

---

## BEAT 4 — THE PROCESS (16.40–22.50s)

**VO:** "Every phase has a decision gate. You approve scope and cost before anything moves forward."

**Concept:** The brand's differentiator visualized. Four decision gates appear as a horizontal pipeline — each one a checkpoint that must be passed. It's a system, not a pitch. The red gate markers activate sequentially like signals on a control board.

**Visual:** Dark maroon gradient (#2A0D13 → #21201B) background with subtle radial SVG pattern (from the DFW section). Four gate nodes arranged horizontally, connected by a thin line that DRAWS left→right. Each node: circle with IBM Plex Mono label below. Gate labels: "DISCOVERY" / "CONCEPT" / "DOCS" / "BUILD". As the line reaches each gate, the circle fills with red (#CB2131) and the label fades to white. A checkmark SVG draws inside each completed gate.

**Mood:** Control room precision. Process engineering. Bauhaus clarity.

**Assets:**
- DFW radial SVG pattern from site background — tiled at 8% opacity
- PHD logo mark — bottom-right, persistent

**Animation choreography:**
- Gate line: DRAWS left→right across full width, 3s, linear
- Gate 1 "DISCOVERY": FILLS red at 0.5s, label fades white, checkmark draws 0.3s
- Gate 2 "CONCEPT": FILLS at 1.2s
- Gate 3 "DOCS": FILLS at 2.0s
- Gate 4 "BUILD": FILLS at 2.8s with subtle scale pulse 100%→105%→100%
- Below gates: "You approve before anything moves forward" types on in Plus Jakarta Sans 400, warm cream (#F4EEEE), 0.5s

**Depth layers:** BG: dark maroon gradient + radial pattern. MG: gate pipeline. FG: labels and completion markers.

**SFX:** Crisp digital "gate clear" chime at each fill. Final gate gets a deeper resonant tone.

**Transition OUT:** Velocity-matched upward — y:-150, blur:30px, 0.33s power2.in

---

## BEAT 5 — THE CTA (22.50–26.50s)

**VO:** "Start your project at premium home dot design."

**Concept:** The logo takes center stage. Clean, resolute, final. The signature red PHD mark assembles from its geometric components, surrounded by breathing space. This is the architect's stamp on the finished blueprint.

**Visual:** Warm cream (#F7F5F0) background. PHD logo mark centered, large (200px), assembles from 4 geometric pieces that SLIDE into position. Below: "premiumhome.design" in IBM Plex Mono 500, dark charcoal (#21201B), types on. Subtle tagline: "Custom homes across DFW & North Texas" in Plus Jakarta Sans 400, smaller, fades in below. Thin red line frames the logo — draws as a rectangle border.

**Mood:** Resolution. The architect signs the drawing. Clean, warm, confident.

**Assets:**
- PHD logo SVG — centered, large, animated assembly
- Red accent border — rectangular frame around logo

**Animation choreography:**
- Logo pieces: SLIDE in from 4 directions, stagger 0.15s each, 0.4s power2.out
- Red frame: DRAWS clockwise from top-left, 1s, starting at 0.5s
- "premiumhome.design": types on at 1.2s, monospace, 0.5s
- Tagline: fades in at 2s, 0.3s
- Final 1s: everything holds. Stillness. Breathing room.

**Depth layers:** BG: warm cream. MG: red frame border. FG: logo + text.

**SFX:** Soft architectural ping on logo assembly complete. Underscore resolves to final warm chord.

**Transition OUT:** None — hold to black fade, 0.5s.

---

## Production Architecture

```
captures/pachano-design/
├── index.html                    root — VO + underscore + beat orchestration
├── DESIGN.md                     brand reference
├── SCRIPT.md                     narration text
├── STORYBOARD.md                 THIS FILE — creative north star
├── transcript.json               word-level timestamps
├── narration.wav                 TTS audio
├── compositions/
│   ├── beat-1-reveal.html
│   ├── beat-2-promise.html
│   ├── beat-3-proof.html
│   ├── beat-4-process.html
│   └── beat-5-cta.html
└── assets/                       captured website assets
```
