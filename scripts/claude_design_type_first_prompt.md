Build a type-first brutalist personal website prototype that could win an Awwwards Site of the Day. Single-page React + Tailwind v4. No images. No 3D. Pure typography as art, with one cursor-reactive WebGL hero moment.

PERSON
- Name: Hector Pachano
- Tagline: Project planner since 2014. Built an autonomous agent so I could plan more. Now it runs while I sleep.
- Past: Project Planner at Y&V, Proyecto San Diego de Cabrutica (Faja del Orinoco, Venezuela oil & gas).
- Now: Founder of Pachano Design (DFW, Texas). Building Dr. Strange, a 24/7 autonomous personal agent, and AI Lead Gen, a satellite + AI render service for home services B2B.
- Languages: Spanish + English. Default copy: English.
- Location: DFW, Texas.

DESIGN DIRECTION (non-negotiable)
- Black background (#0A0A0A), warm off-white text (#F5F1EB), one accent: muted copper (#B87333).
- Typography: use Sentient and Boska from Fontshare (load via <link>). Sentient for display kinetic moments, Boska for editorial body. Variable weight axes used actively, not statically.
- No images, no stock photography, no icons except 1-2 hand-drawn SVG glyphs if needed.
- Layout: editorial brutalist. Asymmetric grids, oversized type that breaks margins, intentional whitespace, monospace footnotes for metadata (year, location, status).
- Motion: subtle but deliberate. Letters splitting on scroll, weight axis animating with cursor proximity, ScrollTrigger pinning on key moments.

REQUIRED SECTIONS (in order)

1. HERO
   - Full viewport. Hector's name "HECTOR PACHANO" rendered massive (12-18vw), centered, kinetic.
   - The name reacts to cursor: letters near the cursor distort using a WebGL canvas behind the text (subtle GLSL displacement shader, like basement.studio's wordmark). If WebGL feels heavy, simulate with CSS transforms per-letter on mousemove — fine as fallback.
   - Subtle vertical eyebrow text in monospace, top-left: "PORTFOLIO // 2026 // DFW"
   - Bottom-right monospace: "SCROLL ↓"
   - No nav, no logo, no buttons. Pure presence.

2. MANIFESTO (3-screen scroll-locked sequence)
   - Each screen is one sentence as oversized type, ScrollTrigger-pinned.
   - Screen 1: "I started planning oil refineries." (with monospace footnote: "Y&V, Faja del Orinoco, 2014–2024")
   - Screen 2: "I learned every project fails the same way: someone forgot the next step." (footnote: "10 years, 4 megaprojects")
   - Screen 3: "So I built an agent that never forgets." (footnote: "Dr. Strange, running 24/7 since 2025")

3. WORK
   - Title: "Currently running" — monospace eyebrow.
   - Asymmetric list of 4 projects, each as oversized type with hover state where the text gains an outline / weight shifts:
     - Dr. Strange — Autonomous personal agent
     - AI Lead Gen — Satellite + AI render for B2B
     - Pachano Design — Digital systems studio
     - QTS — Quant trading research
   - Each list item has small monospace metadata: status (ACTIVE/RESEARCH), year, scope.
   - Hover reveals a one-sentence description in muted copper.

4. PRINCIPLES (editorial typographic composition)
   - 4 short principles as typographic art, not a bullet list. Mixed sizes, intentional broken hierarchy.
   - "Plan obsessively. Execute autonomously. Verify everything. Ship before you're ready."

5. CONTACT
   - Massive type: "Let's plan something." (or copper-accent variant)
   - Underneath, monospace: hector@pachanodesign.com — clickable mailto with custom hover.
   - Footer line at very bottom in tiny mono: "Site v2 — built in public — Dallas–Fort Worth — 2026"

TECH
- React + TypeScript.
- Tailwind CSS v4.
- Smooth scroll: Lenis.
- Animation: GSAP + ScrollTrigger for pin/scrub. Framer Motion for component-level micro-interactions.
- WebGL hero: react-three-fiber + drei + a simple fragment shader for the cursor distortion, OR a pure CSS+JS fallback that splits the name into individual <span> letters and translates them based on cursor distance.
- Font loading: <link rel="preconnect" href="https://api.fontshare.com"> + Fontshare Sentient + Boska variable.
- Performance target: Lighthouse 95+ desktop. No CLS. Hero paints in <1s.
- Accessibility: respect prefers-reduced-motion (kill GSAP timelines, freeze hero canvas), proper heading hierarchy, focus states on all interactive elements.

DELIVERABLE
- One working preview in claude.ai/design that I can scroll through end-to-end.
- All copy in English exactly as written above.
- No placeholders, no "Lorem ipsum", no "your name here". Real content only.
- Do NOT add a nav bar, hero CTA buttons, "Get in touch" cards, testimonials, logos grid, or any SaaS-template element. If it would look at home on a Webflow template, do not include it.

INSPIRATION (do not copy, use as taste reference)
- basement.studio (kinetic wordmark hero)
- vladburca.com (personality density)
- plain.com (editorial restraint)
- leerob.io (typographic confidence)

The site must feel like a person with a worldview made it, not a template. Awwwards judges reward: design innovation, creativity, content quality, usability, mobile responsiveness, typography craft. Optimize for those.
