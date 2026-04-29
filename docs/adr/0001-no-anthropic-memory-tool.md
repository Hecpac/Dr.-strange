# ADR 0001 — No adoptamos `BetaAbstractMemoryTool` (`memory_20250818`)

- **Estado:** Rechazado
- **Fecha:** 2026-04-29
- **Decididores:** Hector Pachano, Claw
- **Contexto del repo:** `claw_v2/memory.py` (MemoryStore SQLite), Claude Agent SDK con preset `claude_code`, runtime local en `:8765`.

## Contexto

Anthropic publicó el tool builtin `memory_20250818` con la interfaz `BetaAbstractMemoryTool` (`view / create / str_replace / insert / delete / rename`) y una implementación de referencia `BetaLocalFilesystemMemoryTool`. El contrato deja que el modelo edite memoria como un filesystem virtual.

Se evaluó si valía la pena exponerlo en Claw vía un adapter (`ClawMemoryTool(BetaAbstractMemoryTool)` sobre `MemoryStore`). Se llegó a hacer un prototipo cableado en `_setup_core_state` con tests verdes (8/8), pero antes de commitear se aplicó el criterio: **¿qué problema concreto resuelve que no esté ya cubierto?**

## Decisión

**No adoptar.** El cableado fue revertido y la branch `feat/memory-tool-adapter` eliminada el 2026-04-29.

## Análisis comparativo

| Capacidad | MEMORY.md (Claude Code) | Unified `MemoryStore` | Episódica P3 (planeada) | `BetaAbstractMemoryTool` |
|---|---|---|---|---|
| Modelo edita memoria autónomamente | Sí (Edit/Read/Write) | — | — | Sí (CRUD plano) |
| Confidence / source_trust / conflict_flag | — | Sí | — | No |
| Embeddings + BM25 híbrido | — | Sí | — | No |
| `task_outcomes` + grafo de entidades | — | Sí | — | No |
| Active Recall pre-tarea | — | Parcial (`recent_failures`) | Sí (objetivo) | No |
| Snapshots episódicos | — | — | Sí | No |
| Compatibilidad Anthropic Managed Agents (cloud) | — | — | — | Sí |

El único valor exclusivo de `BetaAbstractMemoryTool` es la compatibilidad con Anthropic Managed Agents en la nube. Claw corre local con Claude Agent SDK + preset `claude_code` y no tiene roadmap de migración a Managed Agents cloud.

La auditoría Gemma 4 31B (2026-04-24) prioriza Tier Enforcement, Goal Stack, Active Recall, Verification Loop y migración a MCP. **Ninguna de esas necesidades es resuelta por `BetaAbstractMemoryTool`** — es solo un CRUD plano sobre archivos, más pobre que el schema actual de `MemoryStore`.

## Consecuencias

- Se mantiene `MemoryStore` como única fuente de verdad para facts/outcomes/sesión.
- Cualquier exposición futura de la memoria al modelo se hará por la ruta MCP (consistente con la dirección recomendada por la auditoría Gemma).
- No se introduce dependencia adicional con el tool builtin de Anthropic ni con su ciclo de versiones beta.

## Criterio de reapertura

Reconsiderar **solo si** se cumple alguna de:
1. Claw migra a Anthropic Managed Agents cloud para algún lane.
2. Anthropic deprecia los métodos actuales de memoria del SDK y `BetaAbstractMemoryTool` se convierte en la única vía soportada.
3. Aparece un caso de uso concreto que no resuelva ni `MemoryStore` ni la memoria episódica P3 ni un MCP server propio.

Si en el futuro se reabre, **no** reanimar la branch `feat/memory-tool-adapter` (5bb48c4) — crear branch nueva con commits frescos referenciando este ADR.

## Referencias

- `claw_v2/memory.py` — MemoryStore SQLite con confidence/trust/embeddings/task_outcomes
- Auditoría Gemma 4 31B: `claw-gemma-audit-2026-04-24` (wiki)
- Anthropic SDK: `anthropic.lib.tools._beta_builtin_memory_tool`
- Branch eliminada: `feat/memory-tool-adapter` @ `5bb48c4`
