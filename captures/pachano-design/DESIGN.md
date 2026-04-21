# Design System

## Overview

Premium Home Design uses a cinematic dark-to-light visual strategy. The hero opens with a dramatic full-bleed kitchen interior under a dark overlay, then transitions to warm cream (#F7F5F0) sections for process/services content, and returns to deep black (#1C1C1C) for the full-bleed project showcase gallery. The signature red (#CB2131) is used sparingly but with high impact — CTAs, the PHD logo mark, and section accents. Typography pairs a geometric variable sans with a monospace for technical credibility. A Three.js 3D element adds spatial depth to the hero. The overall feel is architectural luxury with engineering discipline.

## Colors

- **Dark Surface**: `#21201B` — primary dark background, hero overlay, headings
- **Deep Black**: `#1C1C1C` — project gallery section, immersive showcase areas
- **Signature Red**: `#CB2131` — CTAs, logo mark, accent borders, decision gate markers
- **Red Highlight**: `#D42D3E` — hover states, active elements, emphasis
- **Warm Cream**: `#F7F5F0` — primary light section background, main content areas
- **Soft Blush**: `#F4EEEE` — blueprint section background, secondary light surface
- **Pure White**: `#FFFFFF` — text on dark backgrounds, project titles
- **Dark Maroon**: `#2A0D13` — deep gradient stops, DFW service area section

## Typography

- **Sans-Serif**: Plus Jakarta Sans (variable 200-800). All headings and body text. Hero at 86px/700, section headings at 39px/700, card titles at 22-28px/700, body at 16px/400.
- **Monospace**: IBM Plex Mono (400, 500, 600). Section labels, process step markers ("STEP"), technical identifiers, and meta text. All-caps with moderate tracking.

## Elevation

The site achieves depth through layered photography rather than glassmorphism or shadows. Full-bleed project images sit behind semi-transparent dark overlays with large white serif-weight titles. Cards use subtle background color shifts (#FFFBF5 on #F7F5F0) with thin borders rather than box-shadows. The Three.js 3D logo element creates literal spatial depth in the hero. The sketch-to-render transition image (render-3d-poster.jpg) uses a split-screen effect implying transformation from concept to reality.

## Components

- **Cinematic Hero**: Full-viewport dark interior photo with gradient overlay, oversized display type, and floating 3D logo element
- **Sketch-to-Render Banner**: Split image showing architectural line drawing morphing into photorealistic render — the signature brand visual
- **Decision Gate Cards**: Bordered cards with red "STEP" markers, deliverable tags, and gate descriptions in monospace
- **Full-Bleed Project Slides**: Immersive project showcases with large italic titles overlaid on architectural photography, "Explore Project" CTAs
- **Bento Grid**: Multi-column layout with stat callouts (60+ homes, 94% on-time) mixing icon cards and metric highlights
- **Trust Pillars**: Three-column cards with thin borders on dark maroon gradient background
- **Testimonial Carousel**: Horizontal scroll with star ratings, blockquote text, client names and city tags
- **DFW Service Area**: Radial SVG pattern background with location-specific content

## Do's and Don'ts

### Do's
- Use warm cream (#F7F5F0) as the default section background — dark sections are reserved for hero and project showcases
- Pair signature red exclusively with dark backgrounds or as isolated CTA buttons on light backgrounds
- Use IBM Plex Mono in all-caps for labels, categories, and process markers
- Maintain cinematic aspect ratios on project photography — full-bleed, edge-to-edge
- Use the sketch-to-render split as the key brand motif — transformation from concept to built reality

### Don'ts
- Do not use red as a background color — it is always a foreground accent (text, borders, buttons)
- Do not use drop shadows — depth comes from photography layering and color shifts
- Do not mix serif fonts — the site is entirely sans-serif + monospace
- Do not break the dark-light rhythm — dark sections must be separated by cream sections
- Do not use rounded corners larger than 12px — the aesthetic is architectural, not playful
