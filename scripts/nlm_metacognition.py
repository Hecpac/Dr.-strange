#!/usr/bin/env python3
"""Create notebook, Deep Research, and podcast via SDK."""
import sys, os, asyncio, time
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.expanduser("~/Projects/Dr.-strange"))

NOTEBOOK_ID = "18719f86-3077-495b-bddd-0fcadc7b5847"
QUERY = (
    "How to build metacognition in autonomous AI agents: self-monitoring, "
    "self-evaluation, confidence calibration, strategy selection, learning from "
    "mistakes, introspective reasoning, cognitive architecture for self-aware "
    "agents. Include research on: chain-of-thought monitoring, agent reflection "
    "loops, epistemic uncertainty, meta-learning, self-improvement cycles, "
    "and recursive self-improvement safety considerations."
)

async def main():
    from notebooklm import NotebookLMClient
    async with await NotebookLMClient.from_storage(timeout=120) as client:
        nb_id = NOTEBOOK_ID
        print(f"Using notebook: {nb_id}")

        # Rename it
        await client.notebooks.rename(nb_id, "Metacognición de Agentes Autónomos")
        print("Renamed notebook")

        # Start Deep Research
        print("\nStarting Deep Research...")
        result = await client.research.start(nb_id, QUERY)
        print(f"Research started: {result}")

        # Poll for completion
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = await client.research.poll(nb_id)
            elapsed = int(time.monotonic() - (deadline - 600))
            print(f"  [{elapsed}s] Status: {status}")
            if status is None:
                print("  Research completed (poll returned None)")
                break
            s = str(status).lower()
            if any(w in s for w in ('completed', 'done', 'finished', 'complete')):
                break
            await asyncio.sleep(15)

        # Import sources
        print("\nImporting sources...")
        try:
            imp = await client.research.import_sources(nb_id)
            print(f"Imported: {imp}")
        except Exception as e:
            print(f"Import: {e}")

        # Generate podcast in Spanish
        print("\nGenerating podcast (audio)...")
        try:
            audio = await client.artifacts.generate_audio(nb_id, language="es")
            print(f"Audio generation started: {audio}")

            # Wait for completion
            deadline2 = time.monotonic() + 900
            while time.monotonic() < deadline2:
                try:
                    poll = await client.artifacts.poll_status(nb_id)
                    elapsed = int(time.monotonic() - (deadline2 - 900))
                    print(f"  [{elapsed}s] Podcast: {poll}")
                    p = str(poll).lower()
                    if any(w in p for w in ('completed', 'done', 'ready')):
                        break
                except Exception:
                    pass
                await asyncio.sleep(20)

            print(f"\nDone! Notebook: https://notebooklm.google.com/notebook/{nb_id}")
        except Exception as e:
            print(f"Audio error: {e}")
            print(f"Notebook ready for manual podcast: https://notebooklm.google.com/notebook/{nb_id}")

asyncio.run(main())
