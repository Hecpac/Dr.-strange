---
name: code-review
description: >
  Revisa PRs y diffs buscando errores de seguridad, lógica, rendimiento y estilo.
  Usa este skill cuando haya un PR listo para revisión, cuando el bus emita un
  mensaje con topic `pr_ready`, o cuando se pida revisar cambios de código.
  También aplica cuando se mencionen revisiones de código, análisis de diffs,
  merge requests, o cualquier solicitud de "revisar estos cambios", "¿está bien
  este PR?", o "qué problemas tiene este diff".
---

# Code Review

Revisa PRs y diffs para encontrar problemas de seguridad, lógica, rendimiento y estilo, entregando hallazgos concretos con ubicación exacta y fix sugerido.

## Inputs requeridos

Antes de ejecutar, recopila esta información:

| Input                          | Fuente                                           | Requerido      |
|-------------------------------|--------------------------------------------------|----------------|
| Diff o referencia al PR        | GitHub PR (repo + número), diff pegado, o branch  | ✅ Sí         |
| Convenciones del proyecto      | CLAUDE.md, .editorconfig, linter configs           | Recomendado   |
| Datos de test coverage         | Coverage report, CI output                         | Si disponible |

Si no tienes acceso directo a las fuentes, pide al usuario que pegue o describa la información relevante.

## Proceso

### 1. Parse del diff
- Descompón el diff en change sets por archivo.
- Para cada archivo registra: nombre, líneas añadidas, líneas eliminadas, tipo de cambio (nuevo, modificado, eliminado).

### 2. Security scan
- Revisa patrones del OWASP top 10: injection, XSS, auth bypass, secrets en código, deserialización insegura.
- Verifica cada patrón contra el contexto del código antes de reportar — 0 falsos positivos permitidos en SECURITY.

### 3. Análisis de lógica
- Detecta: null refs potenciales, errores no manejados, race conditions, off-by-one, problemas de boundary.
- Valida que los nuevos code paths manejen todos los edge cases identificables.

### 4. Performance
- Marca: N+1 queries, loops sin límite, índices faltantes, allocaciones innecesarias.
- Si hay acceso a la base de datos, verifica que las queries nuevas tengan índices correspondientes.

### 5. Estilo
- Compara contra las convenciones del proyecto (naming, imports, estructura de archivos).
- Usa CLAUDE.md o .editorconfig como referencia si existen.

### 6. Test coverage
- Identifica paths de lógica modificados que no tienen cambios de tests correspondientes.
- Marca como hallazgo si un branch nuevo o condición alterada no está cubierta por tests.

## Output format

## 🔍 Code Review: {título del PR o resumen del diff}

### Findings

| # | File:Line | Category | Severity | Finding | Suggested Fix |
|---|-----------|----------|----------|---------|---------------|

Categories: SECURITY, LOGIC, PERFORMANCE, STYLE, TESTS
Severity: MUST-FIX (bloquea merge), SHOULD-FIX (debe atenderse), NIT (opcional)

### Summary
- MUST-FIX: {count}
- SHOULD-FIX: {count}
- NIT: {count}
- Recommendation: {APPROVE | REQUEST_CHANGES | BLOCK}

## Done criteria
- [ ] Cada hallazgo tiene referencia exacta file:line.
- [ ] Cada hallazgo tiene un fix sugerido concreto (no solo "arregla esto").
- [ ] 0 falsos positivos en categoría SECURITY (cada patrón verificado contra contexto).
- [ ] El conteo de MUST-FIX determina directamente la recomendación:
  - 0 MUST-FIX → APPROVE
  - ≥1 MUST-FIX → REQUEST_CHANGES o BLOCK

## Errores comunes a evitar
- No reportes un hallazgo de seguridad sin verificar contra el contexto real del código.
  "Posible SQL injection" no basta — confirma que el input llega sin sanitizar.
- No seas vago: "Mejorar manejo de errores" no es un fix sugerido.
  "Agregar catch para TimeoutError en api/client.ts:42 con retry y fallback a cache" sí lo es.
- No ignores test coverage: si un nuevo if/else no tiene test, repórtalo aunque el resto del archivo tenga cobertura.
