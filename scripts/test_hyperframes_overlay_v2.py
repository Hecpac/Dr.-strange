"""V2 — fix: drawbox doesn't support W/H expressions. Use numeric (1080x1920)."""
import subprocess, os, time, requests
import imageio_ffmpeg

ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
BASE = "/Users/hector/Projects/Dr.-strange/renders/hector_clean_1779387047.mp4"
OUT_DIR = "/Users/hector/Projects/Dr.-strange/renders"
ts = int(time.time())
OUT = f"{OUT_DIR}/hector_overlay_test_v2_{ts}.mp4"

font = "/System/Library/Fonts/Helvetica.ttc"

W, H = 1080, 1920
DUR = 28.5  # actual base duration

PINK = "0xff66cc"
YELLOW = "0xffee00"
WHITE = "0xffffff"

filters = []

# 1) Top-left persistent: "HYPERFRAMES BY HECTOR" (pink box)
filters.append(f"drawbox=x=20:y=20:w=620:h=70:color={PINK}@0.92:t=fill:enable='between(t,0,{DUR})'")
filters.append(f"drawtext=fontfile='{font}':text='HYPERFRAMES BY HECTOR':"
               f"x=40:y=35:fontsize=36:fontcolor=black:enable='between(t,0,{DUR})'")

# 2) Top-right persistent: stats banner (yellow box)
filters.append(f"drawbox=x={W-720}:y=20:w=700:h=70:color={YELLOW}@0.92:t=fill:enable='between(t,0,{DUR})'")
filters.append(f"drawtext=fontfile='{font}':text='1 FUNDADOR  ·  DR STRANGE 24/7  ·  $0 ADS':"
               f"x={W-700}:y=35:fontsize=28:fontcolor=black:enable='between(t,0,{DUR})'")

# 3) Big stat — right side mid: "1 FUNDADOR" t=2-7
filters.append(f"drawbox=x={W-450}:y={H//2-100}:w=420:h=200:color={PINK}@0.95:t=fill:enable='between(t,2,7)'")
filters.append(f"drawtext=fontfile='{font}':text='1 FUNDADOR':"
               f"x={W-430}:y={H//2-50}:fontsize=68:fontcolor={YELLOW}:enable='between(t,2,7)'")

# 4) Big stat — left side mid: "DR STRANGE" t=8-14
filters.append(f"drawbox=x=30:y={H//2-100}:w=460:h=210:color={PINK}@0.95:t=fill:enable='between(t,8,14)'")
filters.append(f"drawtext=fontfile='{font}':text='DR STRANGE':"
               f"x=50:y={H//2-85}:fontsize=64:fontcolor={YELLOW}:enable='between(t,8,14)'")
filters.append(f"drawtext=fontfile='{font}':text='24/7 AGENT':"
               f"x=50:y={H//2+5}:fontsize=58:fontcolor={YELLOW}:enable='between(t,8,14)'")

# 5) Big stat — right side: "12KB SYSTEM" t=16-22
filters.append(f"drawbox=x={W-470}:y={H//2-100}:w=440:h=210:color={PINK}@0.95:t=fill:enable='between(t,16,22)'")
filters.append(f"drawtext=fontfile='{font}':text='12KB':"
               f"x={W-450}:y={H//2-85}:fontsize=84:fontcolor={YELLOW}:enable='between(t,16,22)'")
filters.append(f"drawtext=fontfile='{font}':text='SYSTEM':"
               f"x={W-450}:y={H//2+15}:fontsize=58:fontcolor={YELLOW}:enable='between(t,16,22)'")

# 6) Bottom caption box — sync per beat (durations adjusted to 28.5s base)
captions = [
    (0.0, 4.5, "SOY HECTOR PACHANO"),
    (4.5, 9.0, "PACHANO DESIGN"),
    (9.0, 14.0, "DR STRANGE AGENT"),
    (14.0, 20.0, "MARCAS PENSADAS COMO SISTEMA"),
    (20.0, 25.0, "ENGINEERING > PROMPTING"),
    (25.0, 28.4, "HABLEMOS"),
]
for t0, t1, txt in captions:
    box_w = max(280, len(txt) * 28)
    box_x = (W - box_w) // 2
    filters.append(f"drawbox=x={box_x}:y={H-200}:w={box_w}:h=90:color={WHITE}@0.95:t=fill:enable='between(t,{t0},{t1})'")
    filters.append(f"drawtext=fontfile='{font}':text='{txt}':"
                   f"x=(w-tw)/2:y={H-175}:fontsize=36:fontcolor=black:enable='between(t,{t0},{t1})'")

vf = ",".join(filters)
print(f"Filter ops: {len(filters)}, total chars: {len(vf)}")

cmd = [ffmpeg, "-y", "-i", BASE,
       "-vf", vf,
       "-c:v", "libx264", "-preset", "medium", "-crf", "20",
       "-pix_fmt", "yuv420p", "-movflags", "+faststart",
       "-c:a", "copy", OUT]

t_start = time.time()
r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
elapsed = round(time.time() - t_start, 1)
print(f"RC: {r.returncode} elapsed: {elapsed}s")
if r.returncode != 0:
    print("STDERR tail:", r.stderr[-1500:])
    raise SystemExit(1)

size_mb = os.path.getsize(OUT) / 1024 / 1024
print(f"Output: {os.path.basename(OUT)} ({size_mb:.2f} MB)")

# Thumbnail at 5s
thumb = f"/tmp/overlay_v2_thumb_{ts}.jpg"
subprocess.run([ffmpeg, "-y", "-ss", "5", "-i", OUT, "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", thumb], check=True, capture_output=True)

# Send to Telegram
token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

caption = ("TEST overlay v2 — base: hector_clean Pachano Design + stats laterales (HyperFrames-style). "
           "Validar: legibilidad, no obstruccion de cara, timing de caption box. "
           "Stats placeholder, contenido se itera despues.")

url = f"https://api.telegram.org/bot{token}/sendVideo"
with open(OUT, "rb") as v, open(thumb, "rb") as th:
    r4 = requests.post(url,
        data={"chat_id": chat_id, "caption": caption,
              "width": W, "height": H, "duration": int(DUR),
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
