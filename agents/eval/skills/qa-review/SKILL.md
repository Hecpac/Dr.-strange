---
name: qa-review
description: >
  Evaluación funcional de un deploy live usando browser automation. Usa este
  skill cuando el bus emita `pr_ready` o `deploy_complete`, cuando se pida
  verificar un deploy de preview, o cuando se necesite validar que una feature
  funciona correctamente en producción. También aplica para cualquier solicitud
  de "prueba este deploy", "verifica que funcione", "revisa el preview".
---

# QA Review — Evaluación Funcional de Deploy

Navega un deploy live, ejecuta flujos de usuario, y produce un reporte estructurado con evidencia visual y veredicto.

## Inputs requeridos

| Input                    | Fuente                                    | Requerido |
|--------------------------|-------------------------------------------|-----------|
| URL del deploy           | Vercel preview URL, PR link, o URL directa | Sí        |
| Flujos a probar          | PR description, ticket, o instrucción      | Recomendado |
| Criterios especiales     | Instrucción del usuario o contexto del PR   | Opcional  |

## Proceso

### 1. Setup & Smoke Test
- Navegar a la URL del deploy
- Esperar hydration completo (wait for networkidle o selector clave)
- Capturar screenshot inicial como baseline
- Verificar: ¿carga sin errores? ¿Hay console errors? ¿Assets rotos (404)?
- Si el smoke test falla → reportar FAIL inmediatamente con evidencia

### 2. Functional Flows
Para cada flujo definido (o inferido del PR):
- Ejecutar la secuencia de acciones (click, fill, submit, navigate)
- En cada paso: capturar screenshot + verificar estado esperado
- Probar el happy path primero, luego edge cases:
  - Inputs vacíos
  - Inputs con caracteres especiales
  - Doble submit
  - Navegación back/forward
  - Refresh durante operación

### 3. API Verification (si aplica)
- Si el deploy expone endpoints API, verificar con fetch directo
- Comprobar: status codes, estructura del response, tiempos de respuesta
- Probar con inputs inválidos para verificar error handling

### 4. Responsive Check
- Capturar screenshots en 3 viewports:
  - Desktop (1280×800)
  - Tablet (768×1024)
  - Mobile (375×667)
- Verificar que el layout no se rompe en ningún viewport

### 5. Performance Signals
- Medir tiempo de carga inicial (First Contentful Paint aproximado)
- Detectar layout shifts visibles
- Identificar assets pesados o requests innecesarias

### 6. Grading

Asignar score 0-10 para cada criterio:

| Criterio | Peso | Qué evaluar |
|----------|------|-------------|
| **Functionality** | 40% | ¿Completa los flujos sin errores? ¿Maneja edge cases? |
| **Craft** | 25% | Ejecución técnica: loading states, transiciones, error messages |
| **Responsiveness** | 20% | ¿Funciona en los 3 viewports? |
| **Performance** | 15% | Tiempo de carga, layout shifts, assets |

**Score ponderado** = Σ(score × peso)

## Output format

```markdown
## 🔬 QA Review: {título del PR o URL}

### Smoke Test
- Status: PASS | FAIL
- Console errors: {count}
- Broken assets: {count}
- Screenshot: [baseline]

### Functional Flows

| # | Flow | Steps | Result | Evidence |
|---|------|-------|--------|----------|
| 1 | {flow name} | {N steps} | PASS/FAIL | [screenshot] |

### Findings

| # | Severity | Finding | Expected | Actual | Evidence |
|---|----------|---------|----------|--------|----------|
| 1 | BLOCKER/MAJOR/MINOR | {description} | {expected behavior} | {actual behavior} | [screenshot] |

### Scores

| Criterion | Score | Notes |
|-----------|-------|-------|
| Functionality | X/10 | ... |
| Craft | X/10 | ... |
| Responsiveness | X/10 | ... |
| Performance | X/10 | ... |
| **Weighted** | **X.X/10** | |

### Verdict: PASS | ITERATE | FAIL

**Blockers:** {count}
**Action items for generator:**
1. {specific, actionable item with file/component reference}
```

## Done criteria
- [ ] Smoke test ejecutado con screenshot baseline
- [ ] Al menos 1 flujo funcional completo probado
- [ ] Screenshots en 3 viewports capturados
- [ ] Cada finding tiene evidencia visual
- [ ] Score asignado para los 4 criterios
- [ ] Veredicto emitido basado en thresholds (PASS ≥7, ITERATE ≥5, FAIL <5)

## Errores comunes a evitar
- No reportar PASS sin haber probado al menos un flujo funcional completo
- No inventar findings sin screenshot — si no puedes capturar evidencia, no es un finding
- No probar solo el happy path — los edge cases son donde viven los bugs
- No dar scores altos por default — calibrar contra el rubric, no contra "se ve bien"
