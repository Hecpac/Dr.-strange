---
name: design-review
description: >
  Evaluación visual y de UX de un deploy comparando contra un diseño de
  referencia (Figma, mockup, o criterios de marca). Usa este skill cuando se
  pida evaluar la calidad visual de un deploy, comparar contra un diseño de
  Figma, o cuando el evaluador necesite calificar aspectos de diseño más allá
  de funcionalidad. También aplica para "¿se ve bien?", "compara con el
  diseño", "revisa el UI".
---

# Design Review — Evaluación Visual y UX

Evalúa la calidad visual de un deploy live contra criterios de diseño profesional, opcionalmente comparando con un diseño de referencia (Figma, mockup).

## Inputs requeridos

| Input                    | Fuente                                    | Requerido |
|--------------------------|-------------------------------------------|-----------|
| URL del deploy           | Vercel preview URL, PR link, o URL directa | Sí        |
| Referencia de diseño     | Figma URL, screenshot del mockup, o brief  | Recomendado |
| Brand guidelines         | Colores, tipografía, tokens del proyecto    | Si disponible |

## Proceso

### 1. Capture Baseline
- Navegar al deploy, esperar carga completa
- Capturar full-page screenshot en desktop (1280×800)
- Si hay referencia Figma: obtener screenshot del diseño via MCP

### 2. Visual Coherence Audit
Evaluar sin referencia (standalone quality):
- **Color system:** ¿Los colores son coherentes? ¿Hay colores fuera de paleta?
- **Typography:** ¿Jerarquía clara (h1 > h2 > body)? ¿Máximo 2-3 font families?
- **Spacing:** ¿Ritmo visual consistente? ¿Padding/margin uniformes?
- **Layout:** ¿Grid alineado? ¿Elementos bien distribuidos? ¿No hay overflow?
- **Imagery:** ¿Imágenes bien dimensionadas? ¿No pixeladas? ¿Alt text presente?

### 3. Design Fidelity (si hay referencia)
Comparar deploy vs. diseño original:
- Layout structure: ¿misma disposición de elementos?
- Colors: ¿match exacto o cercano con los tokens del diseño?
- Typography: ¿mismos tamaños, pesos, line-heights?
- Spacing: ¿márgenes y paddings respetados?
- Components: ¿los componentes se ven como en el diseño?
- Anotar cada desviación con screenshot side-by-side

### 4. Interaction Design
- Hover states: ¿existen? ¿son coherentes?
- Focus states: ¿visibles para keyboard navigation?
- Transitions/animations: ¿smooth? ¿con propósito? ¿No distraen?
- Loading states: ¿skeleton, spinner, o nada?
- Error states: ¿diseñados o genéricos?
- Empty states: ¿hay contenido para "sin datos"?

### 5. Accessibility Basics
- Contrast: ¿texto legible sobre su fondo? (WCAG AA: 4.5:1 para texto normal)
- Touch targets: ¿botones ≥ 44×44px en mobile?
- Focus order: ¿lógico al tabular?
- Alt text: ¿imágenes descriptivas tienen alt?

### 6. Responsive Quality
Capturar y evaluar en 3 breakpoints:
- Desktop (1280×800): layout completo
- Tablet (768×1024): adaptación de grid
- Mobile (375×667): stack vertical, menú colapsado

### 7. Grading

| Criterio | Peso | Qué evaluar |
|----------|------|-------------|
| **Design Quality** | 35% | Coherencia visual: colores, typo, layout, spacing |
| **Originality** | 15% | Decisiones custom vs. template genérico |
| **Craft** | 30% | Ejecución técnica: jerarquía, responsive, estados de interacción |
| **Accessibility** | 20% | Contrast, focus, alt text, touch targets |

**Score ponderado** = Σ(score × peso)

## Output format

```markdown
## 🔬 Design Review: {título o URL}

### Visual Coherence
- Color system: {coherent | mixed | broken}
- Typography hierarchy: {clear | inconsistent | absent}
- Spacing rhythm: {consistent | irregular | chaotic}
- Screenshot: [desktop baseline]

### Design Fidelity (si aplica)
- Match score: {X}% estimated
- Key deviations:
  1. {component/area}: {expected} → {actual} [screenshot]

### Interaction States
| State | Present | Quality | Evidence |
|-------|---------|---------|----------|
| Hover | yes/no | {notes} | [screenshot] |
| Focus | yes/no | {notes} | [screenshot] |
| Loading | yes/no | {notes} | [screenshot] |
| Error | yes/no | {notes} | [screenshot] |
| Empty | yes/no | {notes} | [screenshot] |

### Responsive
| Viewport | Layout | Issues | Screenshot |
|----------|--------|--------|------------|
| Desktop 1280px | {ok/broken} | {notes} | [screenshot] |
| Tablet 768px | {ok/broken} | {notes} | [screenshot] |
| Mobile 375px | {ok/broken} | {notes} | [screenshot] |

### Scores

| Criterion | Score | Notes |
|-----------|-------|-------|
| Design Quality | X/10 | ... |
| Originality | X/10 | ... |
| Craft | X/10 | ... |
| Accessibility | X/10 | ... |
| **Weighted** | **X.X/10** | |

### Verdict: PASS | ITERATE | FAIL

**Key improvements for generator:**
1. {specific, visual item — e.g., "reduce heading font-size from 48px to 36px on mobile"}
```

## Done criteria
- [ ] Full-page screenshot capturado en desktop
- [ ] Los 5 aspectos de visual coherence evaluados
- [ ] Si hay referencia Figma: al menos 3 puntos de comparación
- [ ] Interaction states auditados (hover, focus, loading, error, empty)
- [ ] Screenshots en 3 viewports
- [ ] Score asignado para los 4 criterios
- [ ] Veredicto emitido con action items concretos

## Errores comunes a evitar
- No dar score de Originality bajo solo porque usa un framework (Tailwind, shadcn). Evaluar las decisiones dentro del framework.
- No confundir "minimalista" con "incompleto". Un diseño limpio puede ser intencional.
- No penalizar por falta de dark mode a menos que el diseño lo especifique.
- No ignorar mobile — muchos bugs de diseño solo aparecen en viewports pequeños.
- No comparar contra estándares de apps de $100M. Calibrar contra el contexto del proyecto.
