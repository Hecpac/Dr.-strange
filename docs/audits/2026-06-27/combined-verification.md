# Resumen combinado — dos investigaciones verificadas (read-only, HEAD 610bfea)

Dos investigaciones independientes, verificadas cada una contra el código real:
- **Inv. A — Auditoría integral** (baseline `6eb6ab9`, 59 commits stale). 31 hallazgos, 15 agentes. → `audit-verification.md`
- **Inv. B — Browse falla + roadmap F0-F6** (HEAD actual). 15 claims, 8 agentes.

Conclusión central: **no son dos investigaciones separadas — son dos cortes del mismo sistema.** Convergen en tres ejes: **F2**, el **invariante de lanes**, y el **subsistema de verificación**. El código va por delante de toda capa narrativa (docs, memoria, claims de subagentes).

---

## El mapa de relaciones (lo que pediste contrastar)

### 1. F2 es la columna que une H1 (Inv. A) con la fila F2 del roadmap (Inv. B) — MISMO código
- **Inv. A / H1:** 3 tests rojos en `test_bot.py`, root cause = lógica F2-recovery fail-closed (commit `a89096e`, `task_handler.py:868-927`).
- **Inv. B / fila F2:** "BUILT, NOT DEPLOYED".
- **Verificado (CODE-VERIFIED):** es literalmente el mismo subsistema. El **mecanismo de recovery está LIVE** (cableado en el resume de tareas), pero el **store de durabilidad que consume está APAGADO por defecto** (`f2_durability_enabled=False`, `config.py:554`; `ensure_f2_durability_schema` NO se llama en startup; write-path doble-gated `if store is None: return`).
- **Por eso fallan los tests:** mockean un coordinator con `f2_durability_store` *truthy* → ejercen el path de recovery que producción (store=None) nunca toca. **"Los tests rojos" (Inv. A) y "F2 built-not-deployed" (Inv. B) son el mismo hecho** visto desde test-land vs roadmap-land. → "un mecanismo de recovery desplegado que depende de un store de durabilidad NO desplegado."

### 2. El invariante de lanes: Inv. B aterriza lo que Inv. A dio por sentado
- **Inv. A** clasificó el invariante "lanes advisory = sin tools" como **"aseverado por la auditoría, NO re-verificado"** (junto con triple-AND, redacción, cuarentena).
- **Inv. B / B2 (root cause del browse)** es ese invariante EN ACCIÓN: research+verifier son `NON_TOOL_LANES` (`llm.py:37`), enforced por `_validate_lane_input` (`llm.py:570-576`) → el coordinator nunca puede invocar browser. **Inv. B verifica conductualmente parte de lo que Inv. A tomó por fe.**
- Corrección menor (Inv. B se sobre-afirmó): no es "TODO en lane research" — research+synthesis usan `research`, verification usa `verifier`. Ambos son non-tool, así que la conclusión aguanta.

### 3. El subsistema de verificación: misma máquina, hallazgos de signo opuesto
- **Inv. A / D3:** la verificación de notebook es **solo key-presence** → débil ante contenido falso (una review bien-formada pero falsa pasa).
- **Inv. B / HF2:** el verifier **bloqueó correctamente** un resultado sin feed real → cero contenido inventado.
- **Unificado (CODE-VERIFIED en ambos):** el gate de verificación es **bueno detectando evidencia AUSENTE, ciego ante evidencia presente-pero-falsa.** Misma compuerta, dos caras. No es contradicción: es el límite de diseño actual (verifica presencia + igualdad de conteos, no veracidad).

### 4. Ambas confirman: el sistema "falla honestamente", no corrompe en silencio
- Inv. A: sin Critical; D1/D3/D5 cerrados (code-cited); subprocess/`shell=True` cubierto por el tripwire. **(Redacción-por-evento, triple-AND y cuarentena anti-injection = aseverados por la auditoría, NO re-verificados en esta pasada — y el único hallazgo de redacción, `L_codex_redact`, es un gap CONFIRMADO, no "ok".)**
- Inv. B: la tarea FALLÓ veraz (negativa honesta + bloqueo del verifier), sin feed fabricado.
- **Net: el modo de fallo del daemon es "fail-closed cuando falta capacidad" — la postura deseada. El gap es de CAPACIDAD (F5 ejecución browser), no de seguridad.**

### 5. El código va por delante de TODA narrativa (meta-hallazgo de ambas)
- Inv. A: docs stale → `AUDIT_CLOSURE.md` ("OPEN" cuando está cerrado), PRD (bot.py ~200 vs 11.575), runbook ("F2 design-only").
- Inv. B / HF1: **MEMORY lista PR #112 "abierto" pero está MERGED (2026-06-23)**; los 2 críticos no fueron "superseded" (claim del subagente = inexacto) sino **arreglados en el merge** (file:// bloqueado; gate operator parcial). Mismo patrón.

### 6. La feature más nueva (F4-B1) alimenta la tabla sin retención (M1)
- **Inv. A / M1:** `agent_jobs` / `agent_tasks` sin política de retención → crecimiento ilimitado.
- **Inv. B / F4-B1** (lo último desplegado): el `F4DelegationJobRunner` encola filas durables en `agent_jobs` vía `JobService`. **La feature recién enviada alimenta justo la tabla que la auditoría marcó como sin prune.** Convergencia real: M1 se vuelve más relevante con cada feature durable nueva.

---

## Veredictos clave Inv. B (browse + roadmap)

| Claim | Verdict | Clase evidencia | Nota |
|---|---|---|---|
| **B2** coordinator corre en lanes sin tools → nunca browser | PARTIAL (sustancia ✅) | CODE-VERIFIED | "entirely research" sobre-afirmado; es research+verifier, ambos non-tool. 0 browser tools en coordinator/agent_loop |
| **B3** evidence pack = solo metadata, sin contenido | CONFIRMED | CODE-VERIFIED | `attach_trace` solo mezcla TRACE_KEYS (trace_id/job_id/artifact_id) |
| **B1** Chrome/CDP sano 9250 (Chrome/149) | CONFIRMED | CODE-VERIFIED + RUNTIME | Chrome/149.0.7827.200 vivo; tools atómicas existen; "uso example.com" = smoke jun-23 (runtime) |
| **F5_caveat** sin sesión X autenticada | CONFIRMED | RUNTIME (snapshot ahora) | 7 tabs: solo google.com + internals. **Sin X.** 2º gate real |
| **B4** dos modos de fallo distintos | CONFIRMED (mecanismo) | MIXED | 60s timeout (`config.py:140`) + fallback anthropic→openai + circuit-open por rate-limit, todos CODE-VERIFIED; msg-IDs = runtime |
| **F0** quick wins | CONFIRMED | CODE + OPERATIONAL | observe prune+vacuum, atomic scratch |
| **F1** RuntimeDb single-writer + watchdog | CONFIRMED | CODE + OPERATIONAL | `sqlite_runtime.py:573` |
| **F2** built, not deployed | CONFIRMED | CODE-VERIFIED | ver eje #1 |
| **F3** parcial (resume+stale-reclaim sí, lease formal no) | PARTIAL ✅ | CODE-VERIFIED | sin `lease` en el código; solo stale por `updated_at` (300s) |
| **F4** A+B1 hechos, B2 sin construir | CONFIRMED | CODE + OPERATIONAL | F4-A 30s fail-closed; F4-B1 flag(default OFF)+runner; F4-B2 design-only |
| **F5** browser exec NO cableado al coordinator | CONFIRMED | CODE-VERIFIED | tools existen y los llama el brain directo, pero NO en el pipeline del coordinator |
| **F5_1** solo spec draft | CONFIRMED | CODE-VERIFIED | `2026-06-14-hermes-browser-tooling-adoption.md` (status: draft); `browser_tools.py` (847 líneas) sin integrar |
| **F6** fan-out no construido | CONFIRMED | CODE-VERIFIED | coordinator fixed 4-phase; `AgentBus.send` es mensajería, no F6 |
| **HF1** "2 críticos #112 superseded" | **REFUTED** | CODE-VERIFIED | **PR #112 MERGED 2026-06-23**; no superseded → arreglados en merge (file:// ✅, gate operator parcial). Memory stale |
| **HF2** cadena de honestidad funcionó | CONFIRMED (mecanismo) | CODE + RUNTIME | gate `passed_verification_missing_for_action` (`bot.py:6370-6385`) |

---

## Accionables (convergencia de ambas)

1. **El fix del browse = F5** (tu hipótesis correcta, verificada): cablear un paso de ejecución browser tool-capable (lane worker/worker_heavy + las tools CDP que YA funcionan en 9250) al pipeline del coordinator, para que el evidence pack lleve contenido real. Spec draft Hermes existe. **Necesario pero NO suficiente:** el perfil Chrome no tiene sesión X (verificado ahora) → auth/anti-bot de X es un 2º gate aparte.
2. **H1 = fix solo de tests** (mismatch de mock F2), y es el MISMO F2 "built not deployed". Decisión tuya: forzar `f2_durability_store=None` en el mock, o afirmar el nuevo contrato F2-recovery.
3. **Higiene de narrativa (ambas):** `AUDIT_CLOSURE.md`, PRD, runbook (Inv. A) + **MEMORY PR#112** (Inv. B, verificado merged). Residual a *confirmar* (relayed del agente, no auto-verificado): el gate operator de click/type — `requires_human=true` añadido pero `operator` seguiría en `allowed_contexts`.
4. **M1 retención** sube de prioridad: F4-B1 (lo último desplegado) encola en `agent_jobs`, la tabla sin prune. (Resto de accionables de la auditoría en `audit-verification.md`: L_subprocess keychain, D3 judge lane, etc.)

**Postura general (las dos coinciden):** lo que SÍ verifiqué del núcleo de seguridad (D1/D3/D5 + subprocess vía tripwire) está sólido y el fail-closed es honesto; el bloqueo del browse es **capacidad faltante (F5)**, no un bug de seguridad. **(Triple-AND, redacción y cuarentena siguen solo aseverados por la auditoría — pendiente una pasada que los ejercite.)** El trabajo restante es F5 + higiene de docs/tests/memoria.

---

## Nota sobre la evidencia cruda

Los JSON crudos de los 64 veredictos (con cada `file:line` y quote) vivían en `/private/tmp/.../tasks/*.output` (almacenamiento efímero, ya purgado por el OS). Los veredictos destilados con sus citas están embebidos en estos dos reportes y en las tablas de arriba. Los scripts de los workflows que los generaron están en `./workflows/` (reejecutables para regenerar la evidencia cruda).
