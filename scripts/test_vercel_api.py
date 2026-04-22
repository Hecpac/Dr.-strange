"""Test the fixed watchdog API calls."""
import sys
sys.path.insert(0, "/Users/hector/Projects/Dr.-strange/scripts")
from vercel_watchdog import get_vercel_token, get_latest_deployment

token = get_vercel_token()
print(f"Token: {'yes' if token else 'NO'} ({len(token)} chars)")

for project in ["hector-services", "phd", "sinpetca", "tc-insurance"]:
    uid, url = get_latest_deployment(project)
    print(f"{project}: uid={uid[:20]}... url={url[:50]}..." if uid else f"{project}: FAILED")
