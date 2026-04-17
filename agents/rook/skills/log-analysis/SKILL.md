---
name: log-analysis
description: >
  Analisis profundo de logs para identificar patrones anomalos y correlacionarlos con eventos del sistema.
  Usa este skill cuando se necesite investigar logs en detalle, detectar anomalias,
  encontrar patrones de error, o correlacionar eventos entre multiples fuentes de logs.
  Tambien aplica cuando incident-response necesite un deep dive, cuando se mencione
  "revisar logs", "que paso en los logs", "errores raros", "patrones de error",
  "anomalias en logs", "analisis de logs", o cualquier investigacion basada en logs
  que requiera clustering de patrones y deteccion de anomalias.
---

# Log Analysis

Ejecuta un analisis profundo de logs: ingesta con cap de 10k lineas, clustering de patrones, deteccion de 4 tipos de anomalias, correlacion con eventos del sistema, y ranking por impacto.

## Inputs requeridos

| Input                              | Fuente                                                       | Requerido     |
|------------------------------------|--------------------------------------------------------------|---------------|
| Paths de archivos de log           | Configuracion de servicios, CRON.md, rutas conocidas         | Si            |
| Ventana de tiempo a analizar       | Especificado por el caller (default: ultimas 6 horas)        | Recomendado   |
| Contexto del analisis              | Incidente, check de rutina, o pregunta especifica            | Recomendado   |

## Proceso

### 1. Ingesta
- Leer las ultimas N lineas dentro de la ventana de tiempo especificada.
- **Cap estricto de 10,000 lineas** para mantenerse dentro del contexto disponible.
- Si hay mas de 10k lineas, priorizar las mas recientes.
- Registrar cuantas lineas se leyeron vs cuantas habia disponibles.

### 2. Pattern clustering
- Agrupar lineas de log por patron regex: normalizar timestamps, IDs, UUIDs, IPs, y datos variables.
- Contar ocurrencias por patron.
- Ejemplo: `2026-03-31 14:32:01 ERROR [service-x] Connection refused to db:5432` y `2026-03-31 14:35:12 ERROR [service-x] Connection refused to db:5432` son el mismo patron con frecuencia 2.
- Registrar los patrones unicos encontrados.

### 3. Anomaly detection
Detectar 4 tipos de anomalias:

| Tipo                    | Descripcion                                                                | Criterio                                      |
|-------------------------|----------------------------------------------------------------------------|-----------------------------------------------|
| **new_pattern**         | Patrones que aparecen por primera vez en esta ventana                     | No existian en la ventana anterior             |
| **frequency_spike**     | Patrones con frecuencia >3x su baseline normal                            | Comparar con frecuencia promedio historica     |
| **error_escalation**    | Progresion warning -> error -> critical en el mismo componente             | Secuencia de escalacion en el mismo servicio   |
| **gap**                 | Patrones periodicos esperados que dejaron de aparecer                     | Patron con frecuencia regular que se interrumpe|

Para cada anomalia, registrar tipo, frecuencia, primera aparicion, y linea de log de ejemplo.

### 4. Correlacion
Para cada anomalia detectada, buscar correlacion con eventos recientes:
- Deploys (git log).
- Ejecuciones de cron (CRON.md, logs de cron).
- Cambios de configuracion.
- Mensajes de bus (especialmente escalations y urgentes).
- Si no se encuentra correlacion, registrar "none found".

### 5. Ranking por impacto
Ordenar las anomalias por impacto descendente:
- Anomalias que afectan mas componentes o usuarios van primero.
- Error escalation y frequency_spike generalmente tienen mayor impacto que new_pattern.
- Reportar maximo **5 anomalias** (top 5 por impacto, no listado exhaustivo).

## Output format

## Log Analysis: {source} ({time window})

### Summary
- Lines analyzed: {count}
- Unique patterns: {count}
- Anomalies found: {count}

### Top Anomalies

#### 1. {descripcion del patron}
- **Type:** new_pattern | frequency_spike | error_escalation | gap
- **Frequency:** {count} occurrences (baseline: {count})
- **First seen:** {timestamp}
- **Example:** `{linea de log real, copy-paste exacto}`
- **Correlation:** {evento relacionado o "none found"}
- **Impact:** {que afecta y a cuantos componentes/usuarios}

#### 2. {descripcion del patron}
- **Type:** {tipo}
- **Frequency:** {count} occurrences (baseline: {count})
- **First seen:** {timestamp}
- **Example:** `{linea de log real}`
- **Correlation:** {evento o "none found"}
- **Impact:** {que afecta}

[Repetir hasta maximo 5 anomalias]

## Done criteria
- [ ] Cada anomalia tiene una linea de log real como ejemplo (copy-paste, no sintetizada).
- [ ] Los conteos de frecuencia son exactos (de grep/count real, no estimados).
- [ ] Los timestamps de "first seen" son precisos y verificables.
- [ ] Se reportan maximo 5 anomalias (top 5 por impacto).
- [ ] El conteo de lineas analizadas es exacto y no excede el cap de 10,000.

## Errores comunes a evitar
- No sintetices lineas de log: el ejemplo debe ser una linea real copy-pasted, no una reconstruccion.
- No reportes mas de 5 anomalias: el valor esta en priorizar, no en ser exhaustivo.
- No estimes frecuencias: "aproximadamente 50 veces" no es valido. Cuenta exactamente.
- No confundas correlacion con causacion: "deploy ocurrio 10 min antes" es correlacion, no causa confirmada.
- No ignores los gaps: un cron que dejo de logear es tan anomalo como un spike de errores.
