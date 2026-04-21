"""Quick test: verify the Vercel REST API calls work from the watchdog."""
import sys
sys.path.insert(0, "/Users/hector/Projects/Dr.-strange/scripts")
from vercel_watchdog import get_vercel_token, get_latest_deployment

token = get_vercel_token()
print(f"Token found: {'yes' if token else 'NO'} ({len(token)} chars)")

for project in ["hector-services", "phd", "sinpetca", "tc-insurance"]:
    url = get_latest_deployment(project)
    print(f"{project}: {url or 'FAILED'}")
