---
name: health-audit
description: >
  Audita salud operativa de servicios y reporta hallazgos como critical/warn/info.
  Usa este skill cuando el agente de operaciones necesite verificar estado de sistemas,
  detectar problemas, revisar servicios, o responder a “cómo están los servicios”,
  “algo está fallando”, “health check”, “status report”, “auditoría de sistemas”,
  o cualquier revisión de infraestructura y procesos operativos.
  También aplica para revisiones preventivas, post-incidente, o monitoreo periódico.
---

# Health Audit

Ejecuta una auditoría operativa rápida y entrega un reporte clasificado por severidad con acciones concretas.

## Inputs requeridos

| Input                                | Fuente                                                       | Requerido      |
|-------------------------------------|--------------------------------------------------------------|----------------|
| Lista de servicios/sistemas a auditar | Inventario de infraestructura, docker-compose, servicios cloud | ✅ Sí         |
| Logs de errores recientes             | Logs de aplicación, CI/CD, monitoring                        | ✅ Sí         |
| Estado de tareas programadas          | Cron jobs, schedulers, automations, pipelines               | Recomendado   |
| Últimas métricas de performance       | Uptime, latencia, uso de recursos                            | Si disponible |
| Último health audit (si existe)       | Reporte previo para comparar tendencias                      | Si disponible |

## Proceso

### 1. Inventario de servicios
Lista cada servicio/componente con su estado observado:

| Servicio | Estado esperado | Estado actual | Última actividad |
|----------|------------------|---------------|------------------|
| [nombre] | Running 24/7     | [observado]   | [timestamp]      |

### 2. Checklist de verificación
Para cada servicio, verifica:
- [ ] Disponibilidad: ¿Responde? ¿Status code correcto?
- [ ] Errores: ¿Hay errores en los últimos 24h? ¿Son nuevos o recurrentes?
- [ ] Performance: ¿Latencia/uso de recursos dentro de rangos normales?
- [ ] Datos: ¿Los datos se están procesando/actualizando correctamente?
- [ ] Dependencias: ¿APIs externas, bases de datos, third-party services OK?
- [ ] Scheduled tasks: ¿Cron jobs/automations ejecutándose en tiempo?
- [ ] Seguridad: ¿Certificados vigentes? ¿Secrets rotados? ¿Accesos actualizados?

### 3. Clasificación de hallazgos

| Nivel          | Criterio                                                                | Acción requerida              | Ejemplo                                       |
|----------------|-------------------------------------------------------------------------|-------------------------------|-----------------------------------------------|
| 🔴 **CRITICAL** | Servicio caído, data loss, seguridad comprometida, revenue impactado    | Acción inmediata (< 1 hora)   | “API de pagos retorna 500 desde hace 2h”      |
| 🟡 WARN         | Degradación, errores intermitentes, recurso cerca de límite, task fallida | Investigar hoy                | “Disco al 85%, crece 2%/día”                  |
| 🟢 INFO         | Observación, mejora sugerida, mantenimiento preventivo                  | Planificar                    | “Dependencia X tiene major update disponible” |

Reglas de clasificación:
- Si dudas entre WARN y CRITICAL → clasifica como CRITICAL.
- Un WARN que lleva más de 3 días sin atender → escala a CRITICAL.
- Hallazgos de auditorías previas no resueltos → suben un nivel automáticamente.

### 4. Recomendaciones
Solo para hallazgos CRITICAL y WARN, propón:
- Acción concreta: Qué hacer (comando, config change, o investigación específica).
- Quién: Qué rol/persona debería ejecutarlo.
- Cuándo: SLA basado en severidad.
- Verificación: Cómo confirmar que se resolvió.

## Output format

## 🏥 Health Audit Report — [fecha y hora]

### Resumen ejecutivo
| Nivel | Cantidad |
|-------|----------|
| 🔴 CRITICAL | N |
| 🟡 WARN | N |
| 🟢 INFO | N |

**Estado general**: [🔴 Requiere atención inmediata / 🟡 Estable con alertas / 🟢 Saludable]
**Comparación vs último audit**: [Mejoró / Igual / Empeoró] — [detalle breve si aplica]

---
### 🔴 Critical
#### C1: [Título del hallazgo]
- **Servicio**: [nombre]
- **Evidencia**: [dato concreto: error log, métrica, status code]
- **Desde cuándo**: [timestamp o período]
- **Impacto**: [qué está afectando]
- **Acción**: [paso concreto]
- **Verificación**: [cómo confirmar que se resolvió]

---
### 🟡 Warn
#### W1: [Título del hallazgo]
- **Servicio**: [nombre]
- **Evidencia**: [dato]
- **Riesgo si no se atiende**: [qué puede pasar]
- **Acción sugerida**: [paso concreto]

---
### 🟢 Info
- [Observación 1]: [detalle breve]
- [Observación 2]: [detalle breve]

---
### ⏭️ Siguiente acción inmediata
[La acción más urgente que se puede ejecutar en < 5 minutos]

## Done criteria
- [ ] Cada hallazgo tiene evidencia concreta (no suposiciones).
- [ ] Los conteos de critical/warn/info son correctos y verificables.
- [ ] Los hallazgos CRITICAL tienen acción con SLA definido.
- [ ] Hay un “estado general” claro al inicio del reporte.
- [ ] Si hay audit previo, se compara la tendencia.
- [ ] El reporte es escaneable en < 2 minutos.

## Errores comunes a evitar
- No reportes sin evidencia: “Podría haber un problema con X” no es un hallazgo.
  “X retornó error 503 a las 14:32 UTC” sí.
- No infles severidades: Si todo es CRITICAL, nada es CRITICAL. Reserva el rojo para lo que realmente necesita acción inmediata.
- No olvides las dependencias externas: El servicio puede estar “up” pero si la API de terceros que consume está caída, hay un problema.
- No ignores tendencias: Un disco al 80% no es CRITICAL hoy, pero si creció de 60% a 80% en una semana, es WARN.
