"""CLI: dispatch the Echo social media agent and capture its evidence.

Usage:
    python -m claw_v2.cli.echo_smoke [--skill SKILL] [--prompt-file PATH]
                                     [--lane research|worker] [--out-dir DIR]

Defaults to the @pachanodesign IG audit prompt and the `research` lane.
Wires the production-style Anthropic adapter so Echo runs on its configured
provider (claude-opus-4-7), and surfaces silent fallbacks via the
dispatch_typed failures channel.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.agents import FileAgentStore, SubAgentService
from claw_v2.config import AppConfig
from claw_v2.llm import LLMRouter

DEFAULT_PROFILE_SNAPSHOT = """
Account: @pachanodesign
Type: Digital Creator (switched today)
Followers: 0 | Following: 0 | Posts: 0
Display name: Hector Pachano
Bio (v2 live, 110/150 chars):
  Mi AI no me dice 'gran idea'. Me corrige.
  AI agents + brand para founders.
  Dallas - EN/ES
  > como lo construi
Profile photo: dashboard render (Hector frente a Google Ads/Analytics dashboard)
Website link: MISSING (mobile-only, blocked from desktop)
Contact buttons: MISSING (mobile-only)
Category visible on profile: NO (checkbox unchecked)
Story highlights: 0
Threads connected: NO

Available content in disk (not posted yet):
  - sycophancy reel (60s, 9:16, with v3 gold-serif captions, no overlays on face)
  - 3am agent reel (45s, 9:16)
  - dashboard agent reel (33s, 9:16, Pachano Design brand)
  - LinkedIn intro reel (33s, 9:16, executive neutral set)

Constraints durables:
  - No flag emojis in copy.
  - Spanish neutral LATAM only (no voseo).
  - Tier 3 needed before any submit/publish.
  - Warmup phase: cuenta nueva, no postear hasta dia 3-4.
"""

DEFAULT_INSTRUCTION = f"""Echo, audita @pachanodesign con el snapshot live abajo y entrega:

1. Estado actual en 3 lineas (que esta bien / que falta / riesgo principal).
2. Top 5 acciones priorizadas para esta semana (formato tabla: # | accion | quien | cuando).
3. Recomendacion sobre cual reel postear primero (sycophancy/3am/dashboard/LinkedIn intro) y por que.
4. Si detectas algo que viola tus hard rules (flags, voseo, captions sobre cara, etc.), levantalo.

Respeta tu voice: directo, sin "great question", sin emoji-spam, sin promesas de reach especificas.
No inventes metricas. No propongas acciones Tier 3 sin marcarlas como tales.

--- LIVE SNAPSHOT ---
{DEFAULT_PROFILE_SNAPSHOT}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", default=None,
                        help="Optional skill name to invoke instead of free dispatch")
    parser.add_argument("--prompt-file", default=None,
                        help="Path to a prompt file; overrides the default IG-audit prompt")
    parser.add_argument("--lane", default="research", choices=["research", "worker"],
                        help="Dispatch lane (default: research)")
    parser.add_argument("--out-dir", default="artifacts/content",
                        help="Directory for the .md output and meta JSON")
    args = parser.parse_args(argv)

    if args.prompt_file:
        instruction = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        instruction = DEFAULT_INSTRUCTION

    config = AppConfig.from_env()
    anthropic_executor = create_claude_sdk_executor(config)
    router = LLMRouter.default(config, anthropic_executor=anthropic_executor)
    store = FileAgentStore(tempfile.mkdtemp())
    svc = SubAgentService(
        definitions_root=config.agent_definitions_root,
        router=router,
        store=store,
    )
    agents = svc.discover()
    if "echo" not in agents:
        print(f"FAIL: echo not in discovered agents: {agents}", file=sys.stderr)
        return 1

    echo = svc.get_agent("echo")
    print(f"Echo loaded: {echo.display_name} / {echo.provider}:{echo.model}")
    print(f"Skills: {sorted(echo.skills.keys())}")
    print(f"Dispatching {'skill=' + args.skill if args.skill else 'free instruction'} via lane={args.lane}...")

    t0 = time.time()
    if args.skill:
        summary = svc.run_skill("echo", args.skill, instruction, lane=args.lane)
        status = "succeeded"
        failures: tuple = ()
        evidence: list = []
    else:
        result = svc.dispatch_typed("echo", instruction, lane=args.lane)
        summary = result.summary
        status = result.status
        failures = result.failures
        evidence = list(result.evidence)
    elapsed = time.time() - t0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    suffix = args.skill or "dispatch"
    txt_path = out_dir / f"echo_{suffix}_{ts}.md"
    txt_path.write_text(summary, encoding="utf-8")

    meta_path = out_dir / f"echo_{suffix}_{ts}_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "status": status,
                "elapsed_sec": round(elapsed, 2),
                "failures": list(failures),
                "evidence": [
                    {"kind": e.kind, "ref": e.ref, "summary": e.summary}
                    for e in evidence
                ],
                "chars": len(summary),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Status: {status}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Chars: {len(summary)}")
    print(f"Failures: {failures}")
    print(f"Provider used: {evidence[0].ref if evidence else 'n/a'}")
    print(f"Output: {txt_path}")
    print(f"Meta: {meta_path}")
    return 0 if status == "succeeded" and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
