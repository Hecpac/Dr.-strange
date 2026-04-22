#!/usr/bin/env python3
"""Check NotebookLM SDK availability and auth state."""
import os
import sys

# Check SDK
try:
    import notebooklm
    print(f"SDK installed: {getattr(notebooklm, '__version__', 'unknown version')}")
except ImportError:
    print("SDK NOT installed (notebooklm package missing)")

# Check auth state
state_path = os.path.expanduser("~/.notebooklm/storage_state.json")
if os.path.exists(state_path):
    size = os.path.getsize(state_path)
    print(f"Auth state exists: {state_path} ({size} bytes)")
else:
    print(f"Auth state MISSING: {state_path}")

# Check if the service initializes with SDK
sys.path.insert(0, os.path.expanduser("~/Projects/Dr.-strange"))
try:
    from claw_v2.notebooklm import NotebookLMService
    svc = NotebookLMService()
    print(f"Service _use_sdk: {svc._use_sdk}")
    print(f"Service _sdk_available: {svc._sdk_available}")
except Exception as e:
    print(f"Service init error: {e}")
