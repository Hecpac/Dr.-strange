---
name: cron-doctor
description: >
  Diagnostica fallas en cron jobs y propone fixes especificos.
  Usa este skill cuando un heartbeat detecte un cron en estado de error,
  un cron que no se ejecuto en su schedule, o cualquier problema con tareas programadas.
  Tambien aplica cuando se mencione "cron fallo", "cron no corrio", "tarea programada rota",
  "schedule missed", "cron error", "cron timeout", "por que fallo el cron",
  "diagnosticar cron", o cualquier investigacion sobre por que una tarea programada
  no funciona correctamente.
---

# Cron Doctor

Diagnostica por que un cron job esta fallando o fue missed, detecta patrones de falla, verifica dependencias y conflictos, y propone un fix especifico.

## Inputs requeridos

| Input                                  | Fuente                                                    | Requerido     |
|----------------------------------------|-----------------------------------------------------------|---------------|
| Nombre del cron job y su schedule      | CRON.md                                                   | Si            |
| Historial de ejecucion (ultimas 10)    | Timestamps, exit codes, duraciones                        | Si            |
| Configuracion actual de CRON.md        | Archivo CRON.md completo                                  | Si            |
| Estado de recursos del sistema         | CPU, memoria, disco al momento de las fallas              | Si disponible |

## Proceso

### 1. Analisis de historial
Revisar las ultimas 10 ejecuciones del cron y construir una tabla de resultados:
- Para cada ejecucion: timestamp, exit code, duracion, status (success/failure/timeout).
- Calcular tasa de exito y tendencia (mejorando, estable, empeorando).
- Identificar la primera falla en la serie para establecer el inicio del problema.

### 2. Deteccion de patron de falla
Analizar las fallas para determinar cual de los 4 patrones aplica:

| Patron                    | Descripcion                                               | Evidencia necesaria                                  |
|---------------------------|-----------------------------------------------------------|------------------------------------------------------|
| **Time-correlated**       | Siempre falla a la misma hora                             | >= 2 fallas en el mismo horario o rango horario      |
| **Sequence-correlated**   | Falla despues de la ejecucion de otro cron especifico     | >= 2 fallas precedidas por el mismo cron             |
| **Duration-correlated**   | Falla cuando el runtime excede un threshold               | >= 2 fallas donde duracion > promedio de exitos      |
| **Random**                | Sin patron detectado                                      | Fallas sin correlacion temporal, secuencial, o de duracion |

Reglas:
- Un patron requiere evidencia de **al menos 2 ocurrencias** (no se diagnostica patron con 1 sola falla).
- Si hay evidencia para multiples patrones, reportar el de mayor confianza primero.
- Si es random, investigar causas transientes: network, API rate limits, recursos temporales.

### 3. Dependency check
Verificar si el cron depende de recursos externos:
- Servicios/APIs que deben estar disponibles.
- Archivos o directorios que deben existir.
- Otros procesos que deben haber completado.
- Conexiones de red o bases de datos.

Si alguna dependencia no esta disponible al momento de la falla, registrarlo como evidencia.

### 4. Conflict check
Buscar conflictos con otros crons programados:
- Dos crons ejecutandose al mismo tiempo que compiten por el mismo recurso (archivo, lock, API, CPU).
- Overlap de ventanas de ejecucion (cron A todavia corriendo cuando cron B inicia).
- **Si se detecta conflicto, nombrar ambos crons explicitamente.**

Revisar CRON.md completo para identificar schedules que se solapan.

### 5. Propuesta de fix
Generar un fix especifico que incluya:
- **Que cambiar:** la modificacion exacta.
- **Donde:** archivo y linea, o entrada especifica en CRON.md.
- **Riesgo:** low o medium (nunca proponer cambios high-risk sin supervision).
- **Verificacion:** un comando o check ejecutable para confirmar que el fix funciona.

El fix debe ser concreto: "Cambiar schedule de `*/5 * * * *` a `*/10 * * * *` en CRON.md linea 12" — no "investigar mas" o "monitorear".

## Output format

## Cron Doctor: {nombre del cron}

**Schedule:** {cron expression}
**Status:** {failing|missed|intermittent}
**Pattern:** {time|sequence|duration|random}

### History
| Run | Timestamp           | Exit Code | Duration | Status  |
|-----|---------------------|-----------|----------|---------|
| 1   | {timestamp}         | {code}    | {time}   | {ok/fail} |
| 2   | {timestamp}         | {code}    | {time}   | {ok/fail} |
| ... | ...                 | ...       | ...      | ...     |

### Diagnosis
**Pattern:** {descripcion con evidencia de >= 2 ocurrencias}
**Root cause:** {causa especifica identificada}
**Confidence:** {low|medium|high}

### Recommended Fix
- **Change:** {que cambiar exactamente}
- **Where:** {archivo y linea, o entrada en CRON.md}
- **Risk:** {low|medium}
- **Verification:** {comando o check ejecutable para confirmar}

## Done criteria
- [ ] El patron se identifico con evidencia de al menos 2 ocurrencias de falla.
- [ ] El fix es especifico (no "investigar mas" o "monitorear").
- [ ] Si se detecto conflicto, ambos crons en conflicto estan nombrados explicitamente.
- [ ] El paso de verificacion es ejecutable (un comando o check concreto, no "monitorear por unos dias").
- [ ] La tabla de historial tiene datos reales de las ejecuciones.

## Errores comunes a evitar
- No diagnostiques con 1 sola falla: un patron requiere minimo 2 ocurrencias. Si solo hay 1 falla, el patron es "insufficient data" y hay que esperar o buscar logs anteriores.
- No propongas fixes vagos: "ajustar el timeout" no es un fix. "Cambiar TIMEOUT de 30s a 120s en CRON.md entry `fetch-analytics`" si lo es.
- No ignores conflictos de schedule: si dos crons corren al mismo tiempo y uno falla, verificar si comparten recursos antes de buscar otras causas.
- No olvides nombrar ambos crons en un conflicto: "hay un conflicto de schedule" sin decir con cual cron es inutil.
- No asumas que random = no hay problema: fallas random recurrentes indican un problema intermitente real (network flapping, rate limits, memory pressure).
