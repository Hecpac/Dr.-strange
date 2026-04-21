#!/usr/bin/env python3
"""Get status and summary from metacognition notebook."""
import sys, os
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.expanduser("~/Projects/Dr.-strange"))

from claw_v2.notebooklm import NotebookLMService

NB_ID = "18719f86-3077-495b-bddd-0fcadc7b5847"
svc = NotebookLMService()

print("=== STATUS ===")
try:
    status = svc.status(NB_ID)
    nb = status.get("notebook", {})
    print(f"Título: {nb.get('title')}")
    print(f"Fuentes: {nb.get('sources_count')}")
    for s in status.get("sources", []):
        print(f"  - {s.get('title')} ({s.get('kind')})")
except Exception as e:
    print(f"Status error: {e}")

print("\n=== RESUMEN ===")
try:
    summary = svc.chat(NB_ID, "Dame un resumen completo y detallado de todo el contenido de este cuaderno en español. Incluye los temas principales, hallazgos clave y conclusiones.")
    print(summary)
except Exception as e:
    print(f"Chat error: {e}")
