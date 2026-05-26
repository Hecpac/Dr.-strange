#!/bin/bash
# Detached watcher: polls a HeyGen render and delivers to Telegram when complete.
# Invoked via launchd LaunchAgent so it survives independently of Claude Code sandbox.
set -e
cd /Users/hector/Projects/Dr.-strange
exec ./.venv/bin/python -m claw_v2.cli.heygen_deliver "$1" \
  --caption "Anti-Sycophancy reel ES neutro · agent that corrects, not applauds · v3 multimodal (Gmail Drafts + correction log cutaways)" \
  --slug anti_sycophancy_es
