---
name: dependency-audit
description: >
  Audita dependencias del proyecto buscando CVEs, paquetes obsoletos, problemas
  de licencia y paquetes sin uso. Usa este skill en el cron semanal, cuando el bus
  emita un mensaje con topic `security_alert`, o cuando se pida revisar dependencias.
  También aplica cuando se mencionen vulnerabilidades, auditorías de seguridad,
  paquetes desactualizados, o cualquier solicitud de "revisar dependencias",
  "¿hay CVEs?", "¿qué paquetes están obsoletos?", o "auditoría de seguridad".
---

# Dependency Audit

Audita las dependencias del proyecto buscando vulnerabilidades de seguridad, obsolescencia, problemas de licencia y paquetes sin uso, entregando un plan de acción por paquete.

## Inputs requeridos

Antes de ejecutar, recopila esta información:

| Input                          | Fuente                                           | Requerido      |
|-------------------------------|--------------------------------------------------|----------------|
| Archivos de dependencias       | requirements.txt, pyproject.toml, package.json, package-lock.json | ✅ Sí |
| Archivos fuente del proyecto   | Código fuente (para detección de imports no usados) | ✅ Sí         |
| Tipo de proyecto (licencia)    | LICENSE, README, o contexto del usuario             | Recomendado   |

Si no tienes acceso directo a las fuentes, pide al usuario que pegue o describa la información relevante.

## Proceso

### 1. Parse de dependencias
- Parsea todos los archivos de dependencias existentes en una lista unificada: `{name, version, source_file}`.
- Incluye tanto dependencias de producción como de desarrollo, marcando cuáles son dev.

### 2. CVE check
- Cruza cada dependencia contra bases de datos de vulnerabilidades conocidas (pip-audit, npm audit, o OSV.dev API).
- Para cada vulnerabilidad encontrada, registra: CVE ID, severidad, rango de versiones afectadas.

### 3. Freshness
- Compara la versión instalada vs la última estable.
- Marca como STALE si está >2 versiones major atrás o >1 año sin actualización.

### 4. License scan
- Identifica la licencia de cada dependencia.
- Marca GPL/AGPL en proyectos propietarios, o cualquier licencia desconocida.

### 5. Detección de paquetes sin uso
- Busca con grep en el código fuente los imports/requires de cada dependencia.
- Marca como UNUSED las dependencias con 0 coincidencias de import (excluye dev dependencies de esta verificación).
- Registra el comando grep exacto y los 0 resultados como evidencia.

### 6. Pin analysis
- Marca dependencias sin pin (`>=` sin upper bound) que podrían romper en un upgrade.
- Recomienda pin a rango específico para cada caso.

## Output format

## 🔒 Dependency Audit: {nombre del proyecto}

### Summary
- Total dependencies: {count}
- Vulnerable: {count} ({critical}/{high}/{medium}/{low})
- Stale (>1yr): {count}
- Unused: {count}
- License issues: {count}

### Findings

| Package | Version | Status | Detail | Action | Effort |
|---------|---------|--------|--------|--------|--------|

Status: VULN-CRITICAL, VULN-HIGH, VULN-MEDIUM, STALE, UNUSED, LICENSE, PIN, OK
Effort: trivial (version bump), moderate (API changes), significant (major rewrite)

## Done criteria
- [ ] 0 VULN-CRITICAL o VULN-HIGH sin flag explícito.
- [ ] Cada hallazgo UNUSED verificado con evidencia de grep (comando real y 0 resultados).
- [ ] Cada VULN tiene CVE ID y rango de versiones afectadas.
- [ ] Ruta de upgrade especificada para cada dependencia no-OK.

## Errores comunes a evitar
- No reportes UNUSED sin evidencia de grep: ejecuta la búsqueda real y muestra los 0 resultados.
  "Parece que no se usa" no basta — necesitas `grep -r "import package" src/` con 0 matches.
- No ignores dev dependencies en el CVE check: una herramienta de build vulnerable también es un riesgo.
- No asumas licencias: si no puedes determinar la licencia, márcala como LICENSE (unknown) — no la dejes como OK.
- No reportes STALE sin verificar la fecha real de la última release del paquete.
