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
- Chrome CDP corriendo en puerto 9222 (verificar con `lsof -i :9222`)
- Si no está corriendo: `bash scripts/launch-chrome-cdp.sh`
- Google login activo en el perfil CDP (`~/.claw-chrome-cdp`)

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
1. Click dropdown "Investigación rápida" para abrir menú
2. Click "Deep Research" para seleccionarlo
3. El input de Deep Research es un `textarea` con placeholder "¿Sobre qué te gustaría investigar?"
4. **Angular forms**: `fill()` NO activa los botones — usar `mouse.click()` en el input + `keyboard.type()` con delay=25
5. **Botón Enviar**: Buscar `button[aria-label="Enviar"]` más cercano al textarea de Deep Research (NO el del chat)
6. Deep Research tarda 3-8 minutos. Monitorear buscando "finalizó" en `page.content()`
7. Al terminar, click botón "Importar" para cargar fuentes

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
- `DevBrowserService.chrome_navigate()` cierra la conexión al terminar — usar Playwright directo
- Hay DOS botones "Enviar" — uno para Deep Research, otro para Chat. Seleccionar el correcto por proximidad al textarea
- Angular forms requieren `keyboard.type(text, delay=25)` para trigger change detection
- UI en español: "Crear cuaderno nuevo", "Agregar fuentes", "Investigación rápida", "Importar", "Enviar"
- Título del notebook se auto-genera al importar fuentes
