---
name: NotebookLM Workflow
triggers:
  - notebooklm
  - notebook
  - cuaderno
  - podcast
  - deep research
priority: 10
---

# NotebookLM — Workflow de Creación de Cuadernos y Podcasts

## Prerequisitos
- Chrome CDP corriendo (puerto 9222 ó 9250 según launcher; verificar con `curl http://localhost:9250/json/version` o `lsof -i :9222`).
- Si no está corriendo: `bash scripts/launch-chrome-cdp.sh`.
- Google login activo en el perfil CDP (`~/.claw-chrome-cdp`).

## Conexión Playwright
```python
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://localhost:9222")
context = browser.contexts[0]
# Encontrar tab de NotebookLM o crear uno nuevo
page = context.new_page()
page.set_viewport_size({"width": 1280, "height": 900})
page.goto("https://notebooklm.google.com/", wait_until="domcontentloaded", timeout=30000)
```

## Crear Cuaderno Nuevo
1. En la home, click "Crear cuaderno nuevo" o botón "+ Crear cuaderno" en el header
2. Se abre modal de "Agregar fuentes"

## Activar Deep Research (OBLIGATORIO para contenido de calidad)

**Pre-requisito de selectores (verificado 2026-05-01):**
- El modal "Agregar fuentes" se identifica con `mat-dialog-container` (NO `[role="dialog"]`, que también matchea la paleta de emojis oculta).
- Si la URL del notebook está abierta sin el modal, forzar reapertura con `?addSource=true`.

1. Dentro del `mat-dialog-container`, click en chip `button:has-text("Fast Research")` (UI actual; el playbook viejo decía "Investigación rápida" pero ya no aparece — el commit `5cd95d8` corrigió el selector en `notebooklm_cdp.py`).
2. En el popover (fuera del dialog) click `div[role="menu"] >> text="Deep Research"` o fallback `[role="option"]:has-text("Deep Research")`.
3. **Después del switch a Deep Research el control cambia de input a textarea**:
   - Modo Fast Research → `input[placeholder*="Buscar fuentes"]`
   - Modo Deep Research → `textarea[placeholder*="investigar"]` (con placeholder "¿Sobre qué te gustaría investigar?")
4. **Angular forms**: `fill()` NO activa los botones — usar `click()` en el control + `keyboard.type()` con delay=5-25.
5. **Botón Enviar**: `button[aria-label="Enviar"]` scoped al `mat-dialog-container` (NO al chat principal). Fallback: `page.keyboard.press("Enter")`.
6. Deep Research tarda 3-8 minutos. Indicador de progreso visible en sidebar izquierdo: "⏳ Planificando... No salgas de esta página" → "Investigando..." → "Listo".
7. Al terminar, click botón "Importar" para cargar fuentes.

## Generar Podcast de Audio
1. Click "Resumen en audio" o primer botón "Resumen..." en panel Studio (derecha)
2. Podcast en **español** siempre (verificar configuración de idioma antes de generar)
3. Generación tarda 5-15 minutos según cantidad de fuentes
4. Monitorear con screenshots periódicos

## Auto-label de Fuentes (rollout 2026-04-24)

NotebookLM categoriza fuentes automáticamente cuando el cuaderno tiene 5+ fuentes.

- **Disparador**: ≥5 fuentes importadas
- **Espera**: 60s post-import antes de revisar
- **Set estándar de labels** (cross-notebook):
  - 📄 Primaria — papers, docs oficiales
  - 🗞️ Prensa — artículos, blogs
  - 📊 Data — datasets, benchmarks
  - 🧠 Análisis — opinión, ensayos
  - ⚠️ Contrapunto — crítica, red-team
  - 🎙️ Transcript — entrevistas, podcasts
  - 🧪 Research — preprints, RFCs
- **Reglas**: max 7 labels por cuaderno, nombres 1-2 palabras, multi-label sólo si ambas son frecuentes
- **Override manual cuando**: material confidencial cliente, <5 fuentes, contenido TC Insurance / SGC tenant
- **Outputs (audio/quiz/mindmap)**: organización manual con prefijo `YYYY-MM-DD-<tipo>` — auto-label aún no cubre outputs (roadmap)
- **Folders a nivel notebook**: pendientes (roadmap NotebookLM)

## Gotchas Críticos
- `DevBrowserService.chrome_navigate()` cierra la conexión al terminar — usar Playwright directo.
- `[role="dialog"]` matchea la paleta de emojis oculta — usar `mat-dialog-container` para el modal real.
- Hay DOS botones "Enviar" — uno para Deep Research, otro para Chat. Scope al `mat-dialog-container` para no caer en el del chat.
- Angular forms requieren `keyboard.type(text, delay=5-25)` para trigger change detection. `fill()` no funciona.
- El control del search input cambia de `<input>` (Fast Research) a `<textarea>` (Deep Research) tras el toggle.
- UI mixta: en algunas builds el chip dice "Fast Research" en inglés aunque el resto del UI esté en español. Tener selectores para ambas variantes.
- Título del notebook se auto-genera al importar fuentes.
- En Chrome CDP custom de Claw el puerto puede ser 9250 (no 9222). Verificar con `curl localhost:9250/json/version`.
