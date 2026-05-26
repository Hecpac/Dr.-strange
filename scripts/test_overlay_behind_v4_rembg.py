"""V4 — overlay BEHIND speaker using rembg (U2-Net) for segmentation.
rembg is heavier than mediapipe but more universal — uses ONNX U2-Net model.
"""
import os, time, subprocess
import numpy as np
import cv2
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import requests

# rembg
from rembg import new_session, remove

import imageio_ffmpeg
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

BASE = "/Users/hector/Projects/Dr.-strange/renders/hector_clean_1779387047.mp4"
OUT_DIR = Path("/Users/hector/Projects/Dr.-strange/renders")
ts = int(time.time())
OUT_VIDEO_NOAUDIO = OUT_DIR / f"_tmp_v4_noaudio_{ts}.mp4"
OUT_FINAL = OUT_DIR / f"hector_overlay_behind_v4_{ts}.mp4"
THUMB = f"/tmp/overlay_v4_thumb_{ts}.jpg"

# Use a fast model: u2netp is the smaller/lighter version
print("Init rembg session (u2netp)...")
session = new_session("u2netp")
print("Session ready.")

# Open video
cap = cv2.VideoCapture(BASE)
W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"Video: {W}x{H} @ {fps:.2f}fps, {total_frames} frames")

# Downsample for segmentation (speed up): process at 512px wide
SEG_W = 512
SEG_H = int(H * SEG_W / W)

fourcc = cv2.VideoWriter_fourcc(*'mp4v')
writer = cv2.VideoWriter(str(OUT_VIDEO_NOAUDIO), fourcc, fps, (W, H))

PINK_BGR = (204, 102, 255)
YELLOW_BGR = (107, 224, 251)
GREEN_BGR = (102, 255, 102)
WHITE_BGR = (255, 255, 255)

font_path = "/System/Library/Fonts/Supplemental/Impact.ttf"
if not os.path.exists(font_path):
    font_path = "/System/Library/Fonts/Helvetica.ttc"

def make_font(size):
    return ImageFont.truetype(font_path, size)

def text_size(text, size):
    try:
        bbox = make_font(size).getbbox(text)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return len(text) * size // 2, size

def draw_text_pil(img_bgr, text, x, y, size, color_rgb):
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(img_rgb)
    d = ImageDraw.Draw(pil)
    d.text((x, y), text, font=make_font(size), fill=color_rgb)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

def get_caption(t):
    captions = [
        (0.0, 4.5, "SOY HECTOR PACHANO"),
        (4.5, 9.0, "PACHANO DESIGN"),
        (9.0, 14.0, "DR STRANGE AGENT"),
        (14.0, 20.0, "MARCAS PENSADAS COMO SISTEMA"),
        (20.0, 25.0, "ENGINEERING > PROMPTING"),
        (25.0, 28.4, "HABLEMOS"),
    ]
    for t0, t1, txt in captions:
        if t0 <= t < t1:
            return txt
    return None

def get_headline(t):
    beats = [
        (2.0, 8.0, ("MARCAS", "QUE", "PIENSAN")),
        (10.0, 16.0, ("DR", "STRANGE", "24/7")),
        (18.0, 24.0, ("ENGINE", "ERING", "WINS")),
    ]
    for t0, t1, seg in beats:
        if t0 <= t < t1:
            return seg
    return None

def build_overlay_bg(frame, t):
    """Build background-only version of frame with all overlay elements drawn."""
    bg = frame.copy()

    # Top-left banner
    cv2.rectangle(bg, (20, 20), (580, 80), WHITE_BGR, -1)
    cv2.rectangle(bg, (30, 30), (70, 70), PINK_BGR, -1)
    bg = draw_text_pil(bg, "HYPERFRAMES BY HECTOR", 85, 28, 30, (0, 0, 0))

    # Top-right banner
    cv2.rectangle(bg, (W-620, 20), (W-20, 80), YELLOW_BGR, -1)
    bg = draw_text_pil(bg, "1 FUNDADOR - DR STRANGE 24/7 - $0 ADS", W-605, 28, 26, (0, 0, 0))

    # Pink placeholders
    cv2.rectangle(bg, (30, 110), (160, 150), PINK_BGR, -1)
    cv2.rectangle(bg, (W-180, H-340), (W-40, H-290), PINK_BGR, -1)

    # Canvas border
    cv2.rectangle(bg, (10, 100), (W-10, H-180), (180, 180, 180), 2)

    # Headline
    headline = get_headline(t)
    if headline:
        s1, s2, s3 = headline
        fontsize = 110
        char_w = int(fontsize * 0.50)
        pad = 25
        w1 = len(s1) * char_w + pad * 2
        w2 = len(s2) * char_w + pad * 2
        w3 = len(s3) * char_w + pad * 2
        total = w1 + w2 + w3
        x1 = max(-40, (W - total) // 2)
        x2 = x1 + w1
        x3 = x2 + w2
        HEAD_Y = 640
        HEAD_H = 180

        # 3 colored boxes
        cv2.rectangle(bg, (x1, HEAD_Y), (x1+w1, HEAD_Y+HEAD_H), YELLOW_BGR, -1)
        cv2.rectangle(bg, (x2, HEAD_Y), (x2+w2, HEAD_Y+HEAD_H), (0, 0, 0), -1)
        cv2.rectangle(bg, (x3, HEAD_Y), (x3+w3, HEAD_Y+HEAD_H), PINK_BGR, -1)

        # Texts
        bg = draw_text_pil(bg, s1, x1+pad, HEAD_Y+30, fontsize, (0, 0, 0))
        bg = draw_text_pil(bg, s2, x2+pad, HEAD_Y+30, fontsize, (255, 255, 255))
        bg = draw_text_pil(bg, s3, x3+pad, HEAD_Y+30, fontsize, (0, 0, 0))

        # Green selection outline + corner handles
        off = 14
        cv2.rectangle(bg, (x1-off, HEAD_Y-off),
                      (x1+total+off, HEAD_Y+HEAD_H+off), GREEN_BGR, 4)
        hs = 18
        for cx, cy in [
            (x1-off, HEAD_Y-off),
            (x1+total+off, HEAD_Y-off),
            (x1-off, HEAD_Y+HEAD_H+off),
            (x1+total+off, HEAD_Y+HEAD_H+off),
        ]:
            cv2.rectangle(bg, (cx-hs//2, cy-hs//2), (cx+hs//2, cy+hs//2), GREEN_BGR, -1)

    return bg

def add_caption_on_top(img, t):
    cap_text = get_caption(t)
    if not cap_text:
        return img
    tw, _ = text_size(cap_text, 36)
    box_w = max(360, tw + 60)
    box_x = (W - box_w) // 2
    box_y = H - 200
    cv2.rectangle(img, (box_x-3, box_y-3), (box_x+box_w+3, box_y+90+3), (0, 0, 0), -1)
    cv2.rectangle(img, (box_x, box_y), (box_x+box_w, box_y+90), WHITE_BGR, -1)
    tx = (W - tw) // 2
    img = draw_text_pil(img, cap_text, tx, box_y+22, 36, (0, 0, 0))
    return img

frame_idx = 0
t0_render = time.time()
# Process every Nth frame for segmentation (interpolate masks) to speed up
SEG_EVERY = 2  # segment every 2nd frame, reuse mask for the other
last_mask_full = None

while True:
    ok, frame = cap.read()
    if not ok:
        break
    t = frame_idx / fps

    # Segmentation — only every N frames
    if frame_idx % SEG_EVERY == 0 or last_mask_full is None:
        # Downsample → segment → upscale mask
        small = cv2.resize(frame, (SEG_W, SEG_H), interpolation=cv2.INTER_LINEAR)
        small_rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        # rembg.remove with alpha_matting=False for speed; only_mask=True
        out_pil = remove(Image.fromarray(small_rgb), session=session, only_mask=True)
        mask_small = np.array(out_pil)
        mask_full = cv2.resize(mask_small, (W, H), interpolation=cv2.INTER_LINEAR)
        last_mask_full = mask_full
    else:
        mask_full = last_mask_full

    # Normalize mask to 0..1 float, smooth edges
    m = mask_full.astype(np.float32) / 255.0
    # Slight gaussian blur on mask to feather edges
    m = cv2.GaussianBlur(m, (5, 5), 1.5)
    m3 = np.stack([m, m, m], axis=-1)

    # Build overlay'd background
    bg = build_overlay_bg(frame, t)

    # Composite: speaker (frame) ON TOP of bg
    composited = (frame.astype(np.float32) * m3 +
                  bg.astype(np.float32) * (1 - m3)).astype(np.uint8)

    # Caption box ON TOP of composited (front-most)
    composited = add_caption_on_top(composited, t)

    writer.write(composited)
    frame_idx += 1
    if frame_idx % 30 == 0:
        elapsed = time.time() - t0_render
        rate = frame_idx / elapsed if elapsed > 0 else 0
        eta = (total_frames - frame_idx) / rate if rate > 0 else 0
        print(f"  {frame_idx}/{total_frames} ({100*frame_idx/total_frames:.1f}%) {rate:.2f}fps ETA {eta:.0f}s", flush=True)

cap.release()
writer.release()
elapsed = time.time() - t0_render
print(f"\nRender complete: {frame_idx} frames in {elapsed:.1f}s ({frame_idx/elapsed:.2f} fps)")

# Mux audio
print("Muxing audio...")
mux_cmd = [ffmpeg, "-y", "-i", str(OUT_VIDEO_NOAUDIO), "-i", BASE,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:a", "copy", "-shortest", str(OUT_FINAL)]
r = subprocess.run(mux_cmd, capture_output=True, text=True, timeout=180)
if r.returncode != 0:
    print("Mux err:", r.stderr[-800:])
    raise SystemExit(1)

size_mb = OUT_FINAL.stat().st_size / 1024 / 1024
print(f"Final: {OUT_FINAL.name} ({size_mb:.2f} MB)")

try: os.unlink(OUT_VIDEO_NOAUDIO)
except Exception: pass

subprocess.run([ffmpeg, "-y", "-ss", "5", "-i", str(OUT_FINAL), "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", THUMB], check=True, capture_output=True)

token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

caption = ("TEST v4 — overlay BEHIND speaker via rembg (U2-Net segmentation). "
           "Headline 3-color ahora pasa DETRAS de tu silueta. Cara y manos quedan al frente. "
           "Caption box bottom va en front (todo). Beats igual que v3.")

url = f"https://api.telegram.org/bot{token}/sendVideo"
with open(OUT_FINAL, "rb") as v, open(THUMB, "rb") as th:
    r4 = requests.post(url,
        data={"chat_id": chat_id, "caption": caption,
              "width": W, "height": H, "duration": 28,
              "supports_streaming": "true"},
        files={"video": (OUT_FINAL.name, v, "video/mp4"),
               "thumbnail": ("thumb.jpg", th, "image/jpeg")},
        timeout=300)
print(f"\nTelegram status: {r4.status_code}")
j = r4.json()
if j.get('ok'):
    print(f"  msg={j['result'].get('message_id')}")
else:
    print(f"  err: {j.get('description')}")
