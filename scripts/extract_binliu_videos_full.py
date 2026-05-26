"""Download both videos from Bin Liu's tweet at best quality + send to Telegram."""
import subprocess, os, time, json, re, requests

TWEET_URL = "https://x.com/liu8in/status/2058104983770546526"
OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/binliu_video"
os.makedirs(OUT_DIR, exist_ok=True)
ts = int(time.time())

yt_dlp = "/Users/hector/Projects/Dr.-strange/.venv/bin/yt-dlp"

# Download both videos at best quality (let yt-dlp pick + merge)
print("=== Downloading both videos at best quality ===")
out_tmpl = f"{OUT_DIR}/binliu_thread_%(playlist_index)s_{ts}.%(ext)s"
r = subprocess.run([yt_dlp, "-o", out_tmpl, TWEET_URL], capture_output=True, text=True, timeout=300)
print("RC:", r.returncode)
print(r.stdout[-2500:])
if r.stderr:
    print("STDERR:", r.stderr[-800:])

# List all files
print("\n=== Files in dir ===")
files = []
for f in sorted(os.listdir(OUT_DIR)):
    p = os.path.join(OUT_DIR, f)
    sz = os.path.getsize(p)
    print(f"  {f}  ({sz} bytes, {sz/1024/1024:.2f} MB)")
    if f.endswith('.mp4'):
        files.append(p)

# Inspect dimensions of each
import imageio_ffmpeg
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

video_info = {}
for f in files:
    p = subprocess.run([ffmpeg, "-i", f, "-hide_banner"], capture_output=True, text=True)
    info = {}
    for line in p.stderr.splitlines():
        if "Duration" in line:
            m = re.search(r"Duration: (\d+:\d+:\d+\.\d+)", line)
            if m: info['duration'] = m.group(1)
        if "Stream #" in line and "Video:" in line:
            m = re.search(r"(\d{3,4})x(\d{3,4})", line)
            if m: info['res'] = f"{m.group(1)}x{m.group(2)}"
    info['size_mb'] = round(os.path.getsize(f) / 1024 / 1024, 2)
    video_info[f] = info

print("\n=== Video info ===")
print(json.dumps(video_info, indent=2))

# Send each to Telegram with caption explaining what it is
token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

# Find the two files (index 1 and 2)
files_sorted = sorted(files)
captions = {}
for f in files_sorted:
    if '_1_' in f or 'index=1' in f:
        captions[f] = "Bin Liu — 'literally 10x-ed my efficiency' (HyperFrames demo, video principal del tweet, ver 5to segundo)"
    elif '_2_' in f or 'index=2' in f:
        captions[f] = "HeyGen quoted (20h antes) — drag elements en studio, fix placement sin prompting + component catalog"
    else:
        captions[f] = "Bin Liu thread video"

for f in files_sorted:
    size_mb = os.path.getsize(f) / 1024 / 1024
    if size_mb > 49:
        # Compress
        compressed = f.replace('.mp4', '_tg.mp4')
        print(f"Compressing {f} ({size_mb:.1f} MB)...")
        subprocess.run([ffmpeg, "-y", "-i", f,
                        "-c:v", "libx264", "-preset", "medium",
                        "-b:v", "2700k", "-maxrate", "3500k", "-bufsize", "5400k",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        "-c:a", "aac", "-b:a", "128k", compressed],
                       check=True, capture_output=True)
        send_path = compressed
    else:
        send_path = f

    # Thumbnail
    thumb = f"/tmp/thumb_{os.path.basename(send_path)}.jpg"
    subprocess.run([ffmpeg, "-y", "-ss", "2", "-i", send_path, "-frames:v", "1",
                    "-vf", "scale=360:-2", "-q:v", "5", thumb], check=True, capture_output=True)

    # Parse dims
    info = video_info.get(f, {})
    res = info.get('res', '1280x720')
    w, h = (int(x) for x in res.split('x')) if 'x' in res else (1280, 720)
    dur_str = info.get('duration', '00:00:30')
    dh, dm, ds = dur_str.split(':')
    duration = int(float(dh))*3600 + int(dm)*60 + int(float(ds))

    print(f"\nSending {os.path.basename(send_path)} to Telegram...")
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(send_path, "rb") as v, open(thumb, "rb") as th:
        r4 = requests.post(url,
            data={"chat_id": chat_id,
                  "caption": captions.get(f, "Video del tweet"),
                  "width": w, "height": h, "duration": duration,
                  "supports_streaming": "true"},
            files={"video": (os.path.basename(send_path), v, "video/mp4"),
                   "thumbnail": ("thumb.jpg", th, "image/jpeg")},
            timeout=300)
    print(f"  status={r4.status_code}")
    j = r4.json()
    if j.get('ok'):
        print(f"  msg={j['result'].get('message_id')}")
    else:
        print(f"  err: {j.get('description')}")
