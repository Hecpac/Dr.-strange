#!/usr/bin/env python3
"""Import sources and generate podcast for metacognition notebook."""
import sys, os, asyncio, time
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.expanduser("~/Projects/Dr.-strange"))

NOTEBOOK_ID = "18719f86-3077-495b-bddd-0fcadc7b5847"
TASK_ID = "59f1effd-6a34-473d-a0c3-219bb6aaeb52"
SOURCES = [
    {'url': 'https://rewire.it/blog/building-metacognitive-ai-agents-complete-guide/', 'title': 'Building Metacognitive AI Agents: A Complete Guide from Theory to Production', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://openreview.net/forum?id=4KhDd0Ozqe', 'title': 'Position: Truly Self-Improving Agents Require Intrinsic Metacognitive Learning', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://openreview.net/pdf/13e1b3445638eb34bf18995f66ea7e684dc0359f.pdf', 'title': 'AGENTIC CONFIDENCE CALIBRATION', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://openreview.net/pdf?id=UTXtCOWdOM', 'title': 'CONFIDENCE CALIBRATION AND RATIONALIZATION FOR LLMS VIA MULTI-AGENT DELIBERATION', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://www.cs.cmu.edu/~sherryw/assets/pubs/2024-far.pdf', 'title': 'Fact-and-Reflection (FaR) Improves Confidence Calibration of LLMs', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://ojs.aaai.org/index.php/AAAI/article/view/41465/45426', 'title': 'A Metacognitive Architecture for Correcting LLM Errors in AI Agents', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://cicl.stanford.edu/papers/johnson2024wise.pdf', 'title': 'Imagining and building wise machines: The centrality of AI metacognition', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://arxiv.org/html/2602.19837v1', 'title': "Meta-Learning and Meta-Reinforcement Learning - DeepMind's Adaptive Agent", 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://arxiv.org/pdf/2603.06333', 'title': 'SAHOO: Safeguarded Alignment for Recursive Self-Improvement', 'result_type': 1, 'research_task_id': TASK_ID},
    {'url': 'https://arxiv.org/pdf/2505.02888?', 'title': 'Noise-to-Meaning Recursive Self-Improvement', 'result_type': 1, 'research_task_id': TASK_ID},
]

async def main():
    from notebooklm import NotebookLMClient
    async with await NotebookLMClient.from_storage(timeout=120) as client:
        nb_id = NOTEBOOK_ID

        # Step 1: Import research sources
        print("Importing 10 research sources...")
        try:
            imported = await client.research.import_sources(nb_id, TASK_ID, SOURCES)
            print(f"Imported: {len(imported)} sources")
        except Exception as e:
            print(f"Import error: {e}")
            print("Trying to add sources as URLs directly...")
            for s in SOURCES[:5]:
                try:
                    r = await client.sources.add_url(nb_id, s['url'])
                    print(f"  Added: {r.title}")
                except Exception as e2:
                    print(f"  Failed {s['url'][:50]}: {e2}")

        # Step 2: Generate podcast in Spanish
        print("\nGenerating podcast (audio) in Spanish...")
        try:
            audio = await client.artifacts.generate_audio(nb_id, language="es")
            print(f"Audio started: {audio}")

            if hasattr(audio, 'status') and audio.status == 'failed':
                print("Generation failed immediately. Waiting 30s and retrying...")
                await asyncio.sleep(30)
                audio = await client.artifacts.generate_audio(nb_id, language="es")
                print(f"Retry result: {audio}")

            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                try:
                    poll = await client.artifacts.poll_status(nb_id)
                    elapsed = int(time.monotonic() - (deadline - 900))
                    print(f"  [{elapsed}s] Podcast: {poll}")
                    p = str(poll).lower()
                    if any(w in p for w in ('completed', 'done', 'ready')):
                        break
                    if 'failed' in p:
                        print("  Podcast generation failed.")
                        break
                except Exception as pe:
                    print(f"  Poll error: {pe}")
                await asyncio.sleep(20)

            print(f"\nDone! https://notebooklm.google.com/notebook/{nb_id}")
        except Exception as e:
            print(f"Audio error: {e}")
            print(f"Notebook: https://notebooklm.google.com/notebook/{nb_id}")

asyncio.run(main())
