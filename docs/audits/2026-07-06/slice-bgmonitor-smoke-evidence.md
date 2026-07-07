# Slice bgmonitor-phrase-widen — evidencia de smoke

Fecha: 2026-07-07 · PR #222 → main `d1c8aef` · deploy clone `~/srv/claw-daemon` pid 12639

## Contexto

Fase 0 (recon-dedup) DESCONFIRMÓ la premisa de un bug de dedup: el "ya está en
marcha" que el bot repitió 4× sobre la tarea de Calculadora (muerta desde las
19:48) fue confabulación del brain, no código. La causa próxima: el guard
`_enforce_background_monitor_contract` era ciego a las conjugaciones y salía
antes de su chequeo de evidencia. El slice amplía el reconocedor a frases de
referencia-a-tarea / notificación-futura (no estado desnudo, no finalización) y
corrige el falso claim con la verdad, nombrando la tarea fallida.

## Review (regla de verificación pre-merge)

Gemini 3 + CodeRabbit 1; todos aplicados:
- **CodeRabbit P2 (falso positivo que el slice introdujo)**: los patrones de
  estado desnudo ("ya está en marcha" / "está corriendo") colisionan con un
  launch verdadero ("La Calculadora ya está en marcha") → habrían nukeado una
  confirmación legítima. Estrechado a task-reference ("la que está corriendo"),
  left-running ("lo dejo/dejé corriendo") y notificación ("te aviso al
  cerrar/terminar/acabar"). El incidente sigue cazado por 3 vías. +test lock.
- Gemini: "te aviso al" sin redundancia con el "cuando" existente; estado
  traducido ("failed"→"fallida") vía `_public_operational_task_state`.
Ronda final: CodeRabbit pass, 0 findings.

## Deploy

Clone a `d1c8aef`, restart → pid 12639, 12/12 `startup_healthcheck_ok`, puerto
8765, watchdog quieto.

## Smoke — determinista sobre el código DESPLEGADO

Nota de método: el guard corre post-modelo sobre la respuesta del brain; forzar
al brain a producir la frase exacta es no-determinista (mismo límite que
F4-B2a). PERO el guard es una **función pura de (content, session_state)**, así
que se ejerció determinista contra el módulo desplegado del clone (no el
worktree), cubriendo los cuatro carriles:

| Carril | Entrada | Resultado |
|--------|---------|-----------|
| POS | tarea `failed` + "La que está corriendo… Te aviso al cerrar" | **corregido con la verdad**: removió la frase, dice "terminó fallida… reintento", nombra la Calculadora; evento `background_monitor_claim_rejected` |
| NEG1 | tarea genuinamente `running` + "La que está corriendo… te aviso al terminar" | **intacto** (backing-check protege); sin evento nuke |
| NEG2 | "La Calculadora ya está en marcha" (launch verdadero) | **NO matchea** — el falso positivo de CodeRabbit, bloqueado |
| NEG3 | "Listo, ya quedó. Hecho." (finalización) | **NO matchea** — la quemada previa de Hector, protegida |

## Veredicto

Los cuatro carriles PASS sobre el binario desplegado. El comportamiento que le
importa al owner — que no le mienta "ya está en marcha" sobre una tarea muerta,
sin romper confirmaciones legítimas — está probado determinista en el código
que corre en prod. La mitad brain-produce-la-frase es no-determinista (mismo
caveat que F4-B2a); su prueba orgánica será el próximo incidente real.
