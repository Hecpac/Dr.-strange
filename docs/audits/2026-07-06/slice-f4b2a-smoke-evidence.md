# Slice F4-B2a — draft retenido ejecutable: evidencia de smoke en vivo

Fecha: 2026-07-07 · PR #221 → main `3d001be` · deploy clone `~/srv/claw-daemon` pid 95373

## Contexto

Dolor #1 del diagnóstico de ruptura 2026-07-06: cuando el evidence gate retenía
una respuesta del brain, «ejecútalo» re-derivaba desde cero (el draft se
truncaba a 500 chars en un artifact que nadie releía; la memoria de
conversación quedaba con el mensaje enlatado). El slice preserva el draft
completo como `pending_action` ejecutable con TTL 30min; el resolver de
continuación existente lo levanta.

## Review (regla de verificación pre-merge)

Gemini + Codex + CodeRabbit; los 3 findings accionables aplicados antes del merge:
- **Gemini HIGH (seguridad)**: `_is_secret_shaped_token` retorna False ante
  whitespace → un secreto embebido en draft multi-palabra se colaba; fix
  per-token + test con token de alta entropía.
- **CodeRabbit**: la coherencia ahora puntúa contra el ask original (no el
  boilerplate de la directiva); el guard de message-delta es la protección de
  drift; +test.
- **Codex P2**: el path de continuación de Telegram (que no pasa por el
  resolver) chequea frescura inline para no ejecutar un draft vencido.
Ronda final: CodeRabbit pass, 0 findings; Fast gate + secret-scan verdes.

## Deploy

Clone a `3d001be`, restart → pid 95373, 12/12 `startup_healthcheck_ok`, puerto
8765, watchdog quieto, clone limpio.

## Smoke por la superficie real (web chat API viva)

**Nota de alcance honesta:** el disparo del gate (F4-B1 anti-confabulación) es
probabilístico y se rehusó a los 4 intentos sintéticos — el gate funciona: el
brain se negó a fabricar, delegó tareas reales, corrió tools reales, y en un
caso el framing "es planificación" lo descalificó como action-request. Todo
comportamiento sano. Por eso la *creación* de la retención se sembró
controladamente (escribiendo `session_state` con la misma clase `MemoryStore`
que usa el daemon, formato de directiva idéntico al que produce
`_build_retained_draft_directive`), y lo que se ejerció EN VIVO es la mitad
novedosa y de riesgo del slice: **preservar el draft real → «ejecútalo» →
ejecutar el draft, no re-derivar**.

Sesión `web-smoke-f4b2-seed`: sembrada con `pending_action` = directiva +
draft completo, meta `evidence_gate_retained_draft` TTL 1800.

| Paso | Evidencia (event-id, daemon vivo) |
|------|-----------------------------------|
| «ejecútalo» al web chat | turno arranca 00:31:57 |
| Coherencia (fix del review) corre y pasa | **587367 `pending_action_coherence_checked`** |
| El resolver detecta el draft retenido REAL | **587369 `pending_action_detected`** preview = "Ejecuta AHORA, con tools reales o delegate_task, lo que este borrador retenido..." |
| Ejecución arranca con el draft | **587370 `pending_action_execution_started`** (mismo preview) |
| Brain EJECUTA con tools reales | 3 `sdk_post_tool_use` en el turno (no re-narración) |
| Arco cierra | **587482 `pending_action_execution_completed`** |
| Reply | "Hecho... Crucé `approval.py` contra `test_approval.py` (grep directo)... reissue + expiración ya están cubiertos" — ejecutó el plan real y corrigió su premisa con evidencia, en vez de re-derivar desde el mensaje enlatado |

Contraste con el comportamiento viejo (medido hoy antes del deploy, sonda 3 del
diagnóstico): «ejecútalo» sembraba el brain con el mensaje ENLATADO → re-derivaba.
Ahora siembra el draft verbatim → ejecuta.

## Veredicto

La mitad load-bearing del slice (resume del draft real vía «ejecútalo») está
**PASS en vivo** sobre el daemon real. La mitad de preservación (gate → state
write) está test-locked (9 tests) y su formato de salida se replicó exacto en
el seed. Lo único no ejercido en un flujo continuo es la unión gate-fire →
preserve, que requiere una retención orgánica (F4-B1, ya probado por separado)
— su prueba viva completa será la próxima retención orgánica de Hector, que
ahora ejecutará el draft real.

Limpieza: sesión `web-smoke-f4b2-seed` y `web-smoke-f4b2b` son de prueba
(inertes); scratch borrado.
