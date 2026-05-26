"""Extract video from Bin Liu's HyperFrames tweet using yt-dlp."""
import subprocess, os, json, time

TWEET_URL = "https://x.com/liu8in/status/2058104983770546526"
OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/binliu_video"
os.makedirs(OUT_DIR, exist_ok=True)
ts = int(time.time())
out_tmpl = f"{OUT_DIR}/binliu_hyperframes_{ts}.%(ext)s"

# yt-dlp from venv
yt_dlp = "/Users/hector/Projects/Dr.-strange/.venv/bin/yt-dlp"

# Step 1: list available formats (info only, no download)
print("=== Available formats ===")
r1 = subprocess.run([yt_dlp, "-F", TWEET_URL], capture_output=True, text=True, timeout=60)
print(r1.stdout[:3000])
if r1.stderr:
    print("STDERR:", r1.stderr[:800])

# Step 2: download best video
print("\n=== Downloading best video ===")
r2 = subprocess.run([yt_dlp, "-f", "best", "-o", out_tmpl, "--no-playlist", TWEET_URL],
                    capture_output=True, text=True, timeout=180)
print("RC:", r2.returncode)
print(r2.stdout[:1500])
if r2.stderr:
    print("STDERR:", r2.stderr[:1500])

# Step 3: list downloaded files
print("\n=== Files downloaded ===")
for f in sorted(os.listdir(OUT_DIR)):
    p = os.path.join(OUT_DIR, f)
    print(f"  {f}  ({os.path.getsize(p)} bytes)")
