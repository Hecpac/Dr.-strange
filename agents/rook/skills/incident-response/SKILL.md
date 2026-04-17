---
name: incident-response
description: >
  Respuesta estructurada ante incidentes criticos detectados en el sistema.
  Usa este skill cuando se detecte una alerta urgente, un hallazgo CRITICAL
  en health-audit, un mensaje de bus con priority=urgent, o cualquier situacion
  que requiera investigacion y mitigacion inmediata.
  Tambien aplica cuando se mencionen "servicio caido", "data loss", "incidente",
  "produccion rota", "alerta critica", "SEV1", "SEV2", "respuesta a incidente",
  o cualquier escenario que requiera recopilar evidencia, clasificar severidad,
  y ejecutar mitigaciones automaticas.
---

# Incident Response

Ejecuta una respuesta estructurada ante incidentes criticos: recopila evidencia, clasifica severidad, formula hipotesis de causa raiz, y ejecuta mitigaciones Tier 1 de forma automatica.

## Inputs requeridos

| Input                              | Fuente                                                        | Requerido     |
|------------------------------------|---------------------------------------------------------------|---------------|
| Fuente de la alerta                | health-audit finding, bus escalation, o reporte manual        | Si            |
| Log sources disponibles y sus paths | Configuracion de servicios, CRON.md, rutas conocidas         | Si            |
| Historial reciente de deploys      | `git log` de las ultimas 24h                                  | Si            |
| Estado actual de servicios         | Endpoints de salud, status de procesos                        | Recomendado   |

## Proceso

### 1. Recopilacion de evidencia (paralelo)
Ejecuta estas 4 tareas simultaneamente para minimizar tiempo de respuesta:
- **Logs recientes:** ultimos 30 minutos de logs relevantes al servicio afectado.
- **Health de servicios:** estado actual de todos los endpoints (status code, latencia, disponibilidad).
- **Ultimos 3 deploys/commits:** `git log --oneline -3` para identificar cambios recientes que puedan correlacionar.
- **Cron jobs activos:** estado de tareas programadas y su ultimo resultado de ejecucion.

### 2. Construccion de timeline
Ordena cronologicamente todos los eventos recopilados que condujeron al incidente:
- Cada entrada del timeline debe tener timestamp exacto y fuente verificable.
- Incluye: cambios de estado, errores en logs, deploys, ejecuciones de cron, mensajes de bus.
- Minimo 3 data points reales (de logs/eventos, no hipoteticos).

### 3. Clasificacion de severidad

| Severidad | Criterio                                                        | Notificacion                        |
|-----------|-----------------------------------------------------------------|-------------------------------------|
| **SEV1**  | Servicio user-facing caido, riesgo de data loss                 | Telegram a Hector inmediatamente    |
| **SEV2**  | Servicio degradado, funcionalidad parcial perdida               | Notificar dentro de 15 minutos      |
| **SEV3**  | Componente no critico fallando, sin impacto al usuario          | Incluir en el proximo heartbeat     |

Reglas:
- Si hay duda entre SEV1 y SEV2, clasificar como SEV1.
- Data loss confirmado o potencial siempre es SEV1.
- Degradacion que afecta revenue es SEV1, no SEV2.

### 4. Hipotesis de causa raiz
Formula al menos 1 hipotesis de causa raiz con:
- Descripcion clara de la causa propuesta.
- Evidencia que la soporta (log lines especificos, timestamps, correlaciones).
- Nivel de confianza: low, medium, o high.

Si hay multiples hipotesis, listarlas ordenadas por confianza descendente.

### 5. Auto-ejecucion de mitigaciones Tier 1
Ejecutar automaticamente (sin aprobacion) acciones de bajo riesgo:
- Reiniciar cron jobs fallidos.
- Limpiar locks stale (`~/.claw/locks/`).
- Reintentar requests fallidos (maximo 1 reintento).
- Registrar cada accion y su resultado.

Limite de tiempo: todas las mitigaciones Tier 1 deben intentarse dentro de **2 minutos** de la deteccion.

### 6. Runbook para acciones Tier 2/3
Para acciones que requieren aprobacion, generar un runbook con:
- Descripcion de la accion.
- Justificacion de por que es necesaria.
- Nivel de riesgo (medium o high).
- Pasos exactos para ejecutar.
- Como verificar que funciono.

## Output format

## Incident Report

**Severity:** SEV{1|2|3}
**Status:** {investigating|mitigating|resolved}
**Detected:** {timestamp}
**Duration:** {ongoing o resolved at timestamp}

### Timeline
- {timestamp}: {evento} — fuente: {log/deploy/cron/bus}
- {timestamp}: {evento} — fuente: {log/deploy/cron/bus}
- {timestamp}: {evento} — fuente: {log/deploy/cron/bus}

### Root Cause
**Hypothesis:** {descripcion}
**Evidence:** {datos que la soportan — log lines, timestamps, correlaciones}
**Confidence:** {low|medium|high}

### Actions Taken (Tier 1 — auto-executed)
- {accion}: {resultado}

### Actions Pending (require approval)
- {accion}: {por que es necesaria} — Risk: {low|medium|high}

### Notification
{a quien se notifico y cuando}

## Done criteria
- [ ] El timeline tiene al menos 3 data points de logs/eventos reales (no hipoteticos).
- [ ] Hay al menos 1 hipotesis de causa raiz con evidencia concreta.
- [ ] Todas las mitigaciones Tier 1 se intentaron dentro de 2 minutos de la deteccion.
- [ ] SEV1 disparo notificacion inmediata via Telegram a Hector (via bus a Alma).
- [ ] Las acciones pendientes tienen labels de riesgo claros.
- [ ] Cada entrada del timeline tiene timestamp exacto y fuente identificada.

## Errores comunes a evitar
- No inventes datos para el timeline: si no tienes evidencia, no la incluyas. "Posiblemente fallo a las 14:00" no es un data point.
- No subestimes severidad: si un servicio esta degradado y afecta usuarios, es SEV1 o SEV2, no SEV3.
- No ejecutes acciones Tier 2/3 sin aprobacion: reiniciar un servicio es Tier 1, pero hacer rollback de un deploy es Tier 2.
- No ignores la correlacion temporal: si hubo un deploy 5 minutos antes del incidente, es evidencia relevante aunque no sea la causa.
- No olvides notificar: SEV1 sin notificacion es una falla del proceso, no solo del sistema.
