"""V3 — match Bin Liu's exact visual style:
- Headline GIGANTE horizontal con 3 segmentos de color (yellow + black + pink)
- Editor chrome: corner brackets verdes + 2 pink placeholder boxes
- Font Impact (condensed display bold)
- Caption box rounded bottom
"""
import subprocess, os, time, requests
import imageio_ffmpeg

ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
BASE = "/Users/hector/Projects/Dr.-strange/renders/hector_clean_1779387047.mp4"
OUT_DIR = "/Users/hector/Projects/Dr.-strange/renders"
ts = int(time.time())
OUT = f"{OUT_DIR}/hector_overlay_test_v3_{ts}.mp4"

# Try Impact first; fallback to Helvetica Neue Bold Condensed
font_candidates = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]
font = next((f for f in font_candidates if os.path.exists(f)), font_candidates[-1])
print(f"Using font: {font}")

W, H = 1080, 1920
DUR = 28.5

# Colors (Bin Liu palette)
PINK = "0xff66cc"
YELLOW = "0xfbe06b"
GREEN = "0x66ff66"  # selection brackets
BLACK = "0x000000"
WHITE = "0xffffff"

filters = []

# ============================================================
# PERSISTENT TOP BANNERS (full duration)
# ============================================================

# Top-left: small pink icon + "HYPERFRAMES BY HECTOR" in black on white
filters.append(f"drawbox=x=20:y=20:w=560:h=60:color={WHITE}@0.95:t=fill:enable='between(t,0,{DUR})'")
# Pink icon (small box)
filters.append(f"drawbox=x=30:y=30:w=40:h=40:color={PINK}:t=fill:enable='between(t,0,{DUR})'")
filters.append(f"drawtext=fontfile='{font}':text='HYPERFRAMES BY HECTOR':"
               f"x=85:y=32:fontsize=30:fontcolor=black:enable='between(t,0,{DUR})'")

# Top-right: yellow box with stats
filters.append(f"drawbox=x={W-620}:y=20:w=600:h=60:color={YELLOW}:t=fill:enable='between(t,0,{DUR})'")
filters.append(f"drawtext=fontfile='{font}':text='1 FUNDADOR - DR STRANGE 24/7 - $0 ADS':"
               f"x={W-605}:y=32:fontsize=28:fontcolor=black:enable='between(t,0,{DUR})'")

# ============================================================
# PERSISTENT EDITOR CHROME (mimics the meta-look of editing in HyperFrames)
# ============================================================

# Pink placeholder block top-left (below banner)
filters.append(f"drawbox=x=30:y=110:w=130:h=40:color={PINK}:t=fill:enable='between(t,0,{DUR})'")
# Pink placeholder block bottom-right (above caption)
filters.append(f"drawbox=x={W-180}:y={H-340}:w=140:h=50:color={PINK}:t=fill:enable='between(t,0,{DUR})'")

# Canvas border (light grey rounded look — approximate with thin stroke)
filters.append(f"drawbox=x=10:y=100:w={W-20}:h={H-280}:color={WHITE}@0.4:t=2:enable='between(t,0,{DUR})'")

# ============================================================
# HEADLINE BLOCKS — 3 segments per beat
# Each beat: 3 colored boxes side by side spanning full width
# Y position: ~H*0.35 = ~670 (chest level, doesn't tap face which is ~H*0.18)
# ============================================================

HEADLINE_Y = 640
HEADLINE_H = 180
GREEN_OUTLINE_OFFSET = 14  # green selection bracket offset

def headline_block(t0, t1, seg1_text, seg2_text, seg3_text,
                   seg1_color=YELLOW, seg2_color=BLACK, seg3_color=PINK,
                   seg1_text_color=BLACK, seg2_text_color=WHITE, seg3_text_color=BLACK,
                   fontsize=110):
    """Build a 3-segment headline block."""
    # Estimate segment widths based on char count + padding
    # Impact at fontsize=110: ~45-55px per char (condensed)
    char_w = int(fontsize * 0.50)
    pad = 25
    w1 = len(seg1_text) * char_w + pad * 2
    w2 = len(seg2_text) * char_w + pad * 2
    w3 = len(seg3_text) * char_w + pad * 2
    total = w1 + w2 + w3
    # Start a little left of center; let it overflow slightly right (Bin Liu's look)
    start_x = max(-40, (W - total) // 2)
    x1 = start_x
    x2 = x1 + w1
    x3 = x2 + w2

    ops = []
    # Seg 1 (yellow box, black text)
    ops.append(f"drawbox=x={x1}:y={HEADLINE_Y}:w={w1}:h={HEADLINE_H}:color={seg1_color}:t=fill:"
               f"enable='between(t,{t0},{t1})'")
    ops.append(f"drawtext=fontfile='{font}':text='{seg1_text}':"
               f"x={x1+pad}:y={HEADLINE_Y+40}:fontsize={fontsize}:fontcolor={seg1_text_color}:"
               f"enable='between(t,{t0},{t1})'")
    # Seg 2 (black box, white text)
    ops.append(f"drawbox=x={x2}:y={HEADLINE_Y}:w={w2}:h={HEADLINE_H}:color={seg2_color}:t=fill:"
               f"enable='between(t,{t0},{t1})'")
    ops.append(f"drawtext=fontfile='{font}':text='{seg2_text}':"
               f"x={x2+pad}:y={HEADLINE_Y+40}:fontsize={fontsize}:fontcolor={seg2_text_color}:"
               f"enable='between(t,{t0},{t1})'")
    # Seg 3 (pink box, black text)
    ops.append(f"drawbox=x={x3}:y={HEADLINE_Y}:w={w3}:h={HEADLINE_H}:color={seg3_color}:t=fill:"
               f"enable='between(t,{t0},{t1})'")
    ops.append(f"drawtext=fontfile='{font}':text='{seg3_text}':"
               f"x={x3+pad}:y={HEADLINE_Y+40}:fontsize={fontsize}:fontcolor={seg3_text_color}:"
               f"enable='between(t,{t0},{t1})'")

    # Green selection outline around the entire headline block
    outline_x = x1 - GREEN_OUTLINE_OFFSET
    outline_y = HEADLINE_Y - GREEN_OUTLINE_OFFSET
    outline_w = total + GREEN_OUTLINE_OFFSET * 2
    outline_h = HEADLINE_H + GREEN_OUTLINE_OFFSET * 2
    ops.append(f"drawbox=x={outline_x}:y={outline_y}:w={outline_w}:h={outline_h}:color={GREEN}:t=4:"
               f"enable='between(t,{t0},{t1})'")
    # Corner handle squares (small green dots at 4 corners)
    handle_size = 18
    corners = [
        (outline_x - handle_size // 2, outline_y - handle_size // 2),
        (outline_x + outline_w - handle_size // 2, outline_y - handle_size // 2),
        (outline_x - handle_size // 2, outline_y + outline_h - handle_size // 2),
        (outline_x + outline_w - handle_size // 2, outline_y + outline_h - handle_size // 2),
    ]
    for cx, cy in corners:
        ops.append(f"drawbox=x={cx}:y={cy}:w={handle_size}:h={handle_size}:color={GREEN}:t=fill:"
                   f"enable='between(t,{t0},{t1})'")
    return ops

# Beat 1 (t=2-8): "MARCAS QUE PIENSAN"
filters.extend(headline_block(2, 8, "MARCAS", "QUE", "PIENSAN"))

# Beat 2 (t=10-16): "DR STRANGE 24/7" — use 3 segments
filters.extend(headline_block(10, 16, "DR", "STRANGE", "24/7"))

# Beat 3 (t=18-24): "ENGINEERING > PROMPTING"
filters.extend(headline_block(18, 24, "ENGINE", "ERING", "WINS"))

# ============================================================
# CAPTION BOX BOTTOM (rounded look via wider box + smaller inner)
# ============================================================
captions = [
    (0.0, 4.5, "SOY HECTOR PACHANO"),
    (4.5, 9.0, "PACHANO DESIGN"),
    (9.0, 14.0, "DR STRANGE AGENT"),
    (14.0, 20.0, "MARCAS PENSADAS COMO SISTEMA"),
    (20.0, 25.0, "ENGINEERING > PROMPTING"),
    (25.0, 28.4, "HABLEMOS"),
]
for t0, t1, txt in captions:
    box_w = max(360, len(txt) * 26)
    box_x = (W - box_w) // 2
    # Outer black thin border (rounded approximation)
    filters.append(f"drawbox=x={box_x-3}:y={H-200-3}:w={box_w+6}:h=96:color=black@0.8:t=fill:"
                   f"enable='between(t,{t0},{t1})'")
    # Inner white fill
    filters.append(f"drawbox=x={box_x}:y={H-200}:w={box_w}:h=90:color={WHITE}@0.96:t=fill:"
                   f"enable='between(t,{t0},{t1})'")
    filters.append(f"drawtext=fontfile='{font}':text='{txt}':"
                   f"x=(w-tw)/2:y={H-178}:fontsize=38:fontcolor=black:"
                   f"enable='between(t,{t0},{t1})'")

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

# Thumbnail at t=5 (during first headline)
thumb = f"/tmp/overlay_v3_thumb_{ts}.jpg"
subprocess.run([ffmpeg, "-y", "-ss", "5", "-i", OUT, "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", thumb], check=True, capture_output=True)

# Send to Telegram
token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

caption = ("TEST v3 — match Bin Liu look: headline gigante 3-color (yellow+black+pink) + green selection brackets "
           "+ editor chrome (pink placeholders + canvas border) + caption box rounded bottom. "
           "Beats: MARCAS QUE PIENSAN (t2-8) / DR STRANGE 24/7 (t10-16) / ENGINEERING WINS (t18-24).")

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
