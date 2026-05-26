"""Test: apply Bin Liu / HyperFrames-style overlays to an existing render.
Style: stat blocks on SIDES (not over face) + bottom caption box, no burned-in face text.
Base video: hector_clean_1779387047.mp4 (33s Pachano Design pitch, vertical 9:16).
"""
import subprocess, os, time, json, re
import imageio_ffmpeg
import requests

ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
BASE = "/Users/hector/Projects/Dr.-strange/renders/hector_clean_1779387047.mp4"
OUT_DIR = "/Users/hector/Projects/Dr.-strange/renders"
ts = int(time.time())
OUT = f"{OUT_DIR}/hector_overlay_test_v1_{ts}.mp4"

# Inspect base
p0 = subprocess.run([ffmpeg, "-i", BASE, "-hide_banner"], capture_output=True, text=True)
print("=== Base video info ===")
for line in p0.stderr.splitlines():
    if "Duration" in line or "Stream" in line:
        print(" ", line.strip())

# Pick a font that's reliably available on macOS
font = "/System/Library/Fonts/Helvetica.ttc"
if not os.path.exists(font):
    font = "/System/Library/Fonts/HelveticaNeue.ttc"
if not os.path.exists(font):
    font = "/System/Library/Fonts/SFNSDisplay.ttf"
# Bold for caption box impact
font_bold = "/System/Library/Fonts/Helvetica.ttc"

# We'll build a complex filter chain emulating Bin Liu's style:
#   1) Top-left persistent small banner ("PACHANO DESIGN BY HECTOR")
#   2) Top-right persistent small stat banner ("1 FUNDADOR · DR. STRANGE 24/7 · 0 ADS")
#   3) Big stat block right-mid at t=2-8s: "1 FUNDADOR"
#   4) Big stat block right-mid at t=10-18s: "DR. STRANGE 24/7"
#   5) Bottom caption sync box throughout, changing text per beat
#
# Style: pink #ff66cc box + yellow text + thin black border for the big stat blocks
# Caption: white box + black bold text

# Colors
PINK = "0xff66cc"
YELLOW = "0xffee00"
BLACK = "0x000000"
WHITE = "0xffffff"
BORDER = "0x000000"

# Assume vertical 9:16 — most likely 1080x1920 from HeyGen Video Agent v3
W, H = 1080, 1920

# Build filter chain
filters = []

# 1) Top-left persistent banner
# Box around it
filters.append(
    f"drawbox=x=20:y=20:w=600:h=70:color={PINK}@0.92:t=fill"
)
filters.append(
    f"drawtext=fontfile='{font}':text='HYPERFRAMES BY HECTOR':"
    f"x=40:y=35:fontsize=36:fontcolor=black:enable='between(t,0,33)'"
)

# 2) Top-right persistent stats banner
filters.append(
    f"drawbox=x=W-720:y=20:w=700:h=70:color={YELLOW}@0.92:t=fill"
)
filters.append(
    f"drawtext=fontfile='{font}':text='1 FUNDADOR  ·  DR. STRANGE 24/7  ·  $0 ADS':"
    f"x=W-700:y=35:fontsize=30:fontcolor=black:enable='between(t,0,33)'"
)

# 3) Big stat block right-side mid at t=2-8s: "1 FUNDADOR"
# Pink box with yellow text, large
filters.append(
    f"drawbox=x=W-450:y=H/2-100:w=420:h=200:color={PINK}@0.95:t=fill:"
    f"enable='between(t,2,8)'"
)
filters.append(
    f"drawtext=fontfile='{font}':text='1 FUNDADOR':"
    f"x=W-430:y=H/2-50:fontsize=72:fontcolor={YELLOW}:"
    f"enable='between(t,2,8)'"
)

# 4) Big stat block left-side mid at t=10-17s: "DR. STRANGE 24/7"
filters.append(
    f"drawbox=x=30:y=H/2-100:w=450:h=200:color={PINK}@0.95:t=fill:"
    f"enable='between(t,10,17)'"
)
filters.append(
    f"drawtext=fontfile='{font}':text='DR. STRANGE':"
    f"x=50:y=H/2-90:fontsize=64:fontcolor={YELLOW}:"
    f"enable='between(t,10,17)'"
)
filters.append(
    f"drawtext=fontfile='{font}':text='24/7':"
    f"x=50:y=H/2:fontsize=64:fontcolor={YELLOW}:"
    f"enable='between(t,10,17)'"
)

# 5) Big stat block right-side mid at t=20-27s: "12KB SYSTEM"
filters.append(
    f"drawbox=x=W-470:y=H/2-100:w=440:h=200:color={PINK}@0.95:t=fill:"
    f"enable='between(t,20,27)'"
)
filters.append(
    f"drawtext=fontfile='{font}':text='12KB':"
    f"x=W-450:y=H/2-90:fontsize=80:fontcolor={YELLOW}:"
    f"enable='between(t,20,27)'"
)
filters.append(
    f"drawtext=fontfile='{font}':text='SYSTEM':"
    f"x=W-450:y=H/2+10:fontsize=64:fontcolor={YELLOW}:"
    f"enable='between(t,20,27)'"
)

# 6) Bottom caption box — sync to spoken word beats (in Spanish)
# t=0-5: "SOY HECTOR PACHANO"
# t=5-10: "PACHANO DESIGN"
# t=10-15: "DR. STRANGE AGENT"
# t=15-22: "MARCAS QUE PIENSAN COMO SISTEMA"
# t=22-28: "ENGINEERING > PROMPTING"
# t=28-33: "HABLEMOS"
captions = [
    (0, 5, "SOY HECTOR PACHANO"),
    (5, 10, "PACHANO DESIGN"),
    (10, 15, "DR. STRANGE AGENT"),
    (15, 22, "MARCAS PENSADAS COMO SISTEMA"),
    (22, 28, "ENGINEERING > PROMPTING"),
    (28, 33, "HABLEMOS"),
]
for t0, t1, txt in captions:
    # Caption box: white bg, centered horizontally at y=H-180
    box_w = max(200, len(txt) * 28)
    filters.append(
        f"drawbox=x=(W-{box_w})/2:y=H-200:w={box_w}:h=90:color={WHITE}@0.95:t=fill:"
        f"enable='between(t,{t0},{t1})'"
    )
    filters.append(
        f"drawtext=fontfile='{font}':text='{txt}':"
        f"x=(W-tw)/2:y=H-170:fontsize=36:fontcolor=black:"
        f"enable='between(t,{t0},{t1})'"
    )

vf = ",".join(filters)

cmd = [
    ffmpeg, "-y", "-i", BASE,
    "-vf", vf,
    "-c:v", "libx264", "-preset", "medium",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    "-c:a", "copy",
    OUT
]
print(f"\n=== Rendering test overlay ===\nOutput: {OUT}")
print(f"Filter chain length: {len(vf)} chars, {len(filters)} ops")

t0 = time.time()
r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
elapsed = round(time.time() - t0, 1)
print(f"RC: {r.returncode} elapsed: {elapsed}s")
if r.returncode != 0:
    print("STDERR tail:", r.stderr[-1500:])
    raise SystemExit(1)

size_mb = os.path.getsize(OUT) / 1024 / 1024
print(f"Output: {os.path.basename(OUT)} ({size_mb:.2f} MB)")

# Thumbnail at the 5s mark (where "1 FUNDADOR" should be visible)
thumb = f"/tmp/overlay_test_thumb_{ts}.jpg"
subprocess.run([ffmpeg, "-y", "-ss", "5", "-i", OUT, "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", thumb], check=True, capture_output=True)

# Send to Telegram
token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

caption = ("TEST overlay HyperFrames-style v1 — base: hector_clean Pachano Design pitch + "
           "stat blocks laterales (no sobre cara) + caption box sync bottom. "
           "Ojo: contenido de stats es placeholder, lo importante es validar el formato.")

url = f"https://api.telegram.org/bot{token}/sendVideo"
with open(OUT, "rb") as v, open(thumb, "rb") as th:
    r4 = requests.post(url,
        data={"chat_id": chat_id, "caption": caption,
              "width": W, "height": H, "duration": 33,
              "supports_streaming": "true"},
        files={"video": (os.path.basename(OUT), v, "video/mp4"),
               "thumbnail": ("thumb.jpg", th, "image/jpeg")},
        timeout=300)
print(f"\nTelegram status: {r4.status_code}")
j = r4.json()
if j.get('ok'):
    print(f"  msg={j['result'].get('message_id')}")
else:
    print(f"  err: {j.get('description')}")
