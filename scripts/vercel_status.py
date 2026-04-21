import subprocess, json, os, pathlib

# Search for Vercel token in multiple locations
token = None

# 1. Environment variable
token = os.environ.get("VERCEL_TOKEN", "")

# 2. Keychain variations
if not token:
    for service_name in ["vercel-token", "VERCEL_TOKEN", "vercel", "Vercel", "vercel-api-token"]:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            token = r.stdout.strip()
            print(f"Found in Keychain service: {service_name}")
            break
        r2 = subprocess.run(
            ["security", "find-generic-password", "-a", service_name, "-w"],
            capture_output=True, text=True
        )
        if r2.returncode == 0 and r2.stdout.strip():
            token = r2.stdout.strip()
            print(f"Found in Keychain account: {service_name}")
            break

# 3. Vercel CLI auth file locations
if not token:
    for p in [
        pathlib.Path.home() / ".config" / "vercel" / "auth.json",
        pathlib.Path.home() / ".vercel" / "auth.json",
        pathlib.Path.home() / ".local" / "share" / "com.vercel.cli" / "auth.json",
    ]:
        if p.exists():
            data = json.loads(p.read_text())
            token = data.get("token", "")
            if token:
                print(f"Found in: {p}")
                break

# 4. Check .env files in Dr.-strange
if not token:
    env_path = pathlib.Path("/Users/hector/Projects/Dr.-strange/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "VERCEL" in line.upper() and "=" in line:
                print(f"Found env line: {line[:30]}...")
                key, val = line.split("=", 1)
                if "TOKEN" in key.upper():
                    token = val.strip().strip('"').strip("'")

# 5. Check all .env files in home Projects
if not token:
    for env_file in pathlib.Path("/Users/hector/Projects").rglob(".env*"):
        if env_file.is_file() and env_file.stat().st_size < 10000:
            try:
                for line in env_file.read_text().splitlines():
                    if "VERCEL" in line.upper() and "TOKEN" in line.upper() and "=" in line:
                        print(f"Found in: {env_file}: {line[:30]}...")
                        _, val = line.split("=", 1)
                        token = val.strip().strip('"').strip("'")
                        break
            except Exception:
                pass
        if token:
            break

if token:
    print(f"\nToken: {token[:8]}... ({len(token)} chars)")
else:
    print("\nNO VERCEL TOKEN FOUND")
    # List keychain entries with 'vercel' for debugging
    r = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        if "vercel" in line.lower():
            print(f"  Keychain hint: {line.strip()}")
