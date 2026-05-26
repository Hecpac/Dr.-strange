"""V4 — overlay BEHIND the speaker via mediapipe selfie segmentation.
Pipeline per frame:
  1. Get speaker mask (mediapipe selfie segmentation)
  2. Render overlay onto a copy of the frame (background with HyperFrames-style elements)
  3. Composite: out = overlay_frame * (1-mask) + original_frame * mask
     → overlays "behind" speaker
"""
import os, time, subprocess, json
import numpy as np
import cv2
import mediapipe as mp
import imageio_ffmpeg
from pathlib import Path

ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

BASE = "/Users/hector/Projects/Dr.-strange/renders/hector_clean_1779387047.mp4"
OUT_DIR = Path("/Users/hector/Projects/Dr.-strange/renders")
ts = int(time.time())
OUT_VIDEO_NOAUDIO = OUT_DIR / f"_tmp_v4_noaudio_{ts}.mp4"
OUT_FINAL = OUT_DIR / f"hector_overlay_behind_v4_{ts}.mp4"
THUMB = f"/tmp/overlay_v4_thumb_{ts}.jpg"

# Init mediapipe selfie segmentation
mp_selfie = mp.solutions.selfie_segmentation
seg = mp_selfie.SelfieSegmentation(model_selection=1)  # 1 = landscape, better for talking head

# Open video
cap = cv2.VideoCapture(BASE)
W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"Video: {W}x{H} @ {fps:.2f}fps, {total_frames} frames")

# Writer
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
writer = cv2.VideoWriter(str(OUT_VIDEO_NOAUDIO), fourcc, fps, (W, H))

# Font path
font_face = cv2.FONT_HERSHEY_DUPLEX
font_simplex = cv2.FONT_HERSHEY_SIMPLEX

# Colors BGR (cv2 uses BGR not RGB)
PINK = (204, 102, 255)        # #ff66cc
YELLOW = (107, 224, 251)      # #fbe06b
GREEN = (102, 255, 102)       # #66ff66
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

# Try to load Impact font as PIL since cv2.putText doesn't support TTF directly
from PIL import Image, ImageDraw, ImageFont
font_candidates = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Impact.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]
font_path = next((f for f in font_candidates if os.path.exists(f)), None)
print(f"Font: {font_path}")

def make_font(size):
    return ImageFont.truetype(font_path, size)

def draw_text_pil(img_bgr, text, x, y, size, color_rgb):
    """Draw text using PIL (TTF support) onto a BGR numpy array."""
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(img_rgb)
    d = ImageDraw.Draw(pil)
    d.text((x, y), text, font=make_font(size), fill=color_rgb)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

def draw_box(img, x, y, w, h, color_bgr, alpha=1.0, thickness=-1):
    """Draw filled or outlined box with optional alpha."""
    if thickness == -1:
        if alpha < 1.0:
            overlay = img.copy()
            cv2.rectangle(overlay, (x, y), (x+w, y+h), color_bgr, -1)
            cv2.addWeighted(overlay, alpha, img, 1-alpha, 0, img)
        else:
            cv2.rectangle(img, (x, y), (x+w, y+h), color_bgr, -1)
    else:
        cv2.rectangle(img, (x, y), (x+w, y+h), color_bgr, thickness)
    return img

def get_caption(t):
    """Return current caption text for time t."""
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
    """Return current 3-segment headline for time t."""
    beats = [
        (2.0, 8.0,  ("MARCAS", "QUE", "PIENSAN")),
        (10.0, 16.0, ("DR", "STRANGE", "24/7")),
        (18.0, 24.0, ("ENGINE", "ERING", "WINS")),
    ]
    for t0, t1, seg in beats:
        if t0 <= t < t1:
            return seg
    return None

frame_idx = 0
t0_render = time.time()
while True:
    ok, frame = cap.read()
    if not ok:
        break

    t = frame_idx / fps

    # 1. Get speaker mask
    res = seg.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    mask = res.segmentation_mask  # float32 0..1, shape (H, W)
    # Threshold and smooth
    mask_3 = np.stack([mask, mask, mask], axis=-1)  # (H, W, 3)

    # 2. Build background with overlays (will composite under speaker)
    bg = frame.copy()

    # --- Persistent top banners ---
    # Top-left: white box with pink icon + text "HYPERFRAMES BY HECTOR"
    bg = draw_box(bg, 20, 20, 560, 60, WHITE)
    bg = draw_box(bg, 30, 30, 40, 40, PINK)
    bg = draw_text_pil(bg, "HYPERFRAMES BY HECTOR", 85, 28, 30, (0, 0, 0))

    # Top-right: yellow box stats
    bg = draw_box(bg, W-620, 20, 600, 60, YELLOW)
    bg = draw_text_pil(bg, "1 FUNDADOR - DR STRANGE 24/7 - $0 ADS", W-605, 28, 26, (0, 0, 0))

    # --- Persistent editor chrome ---
    # Pink placeholder top-left
    bg = draw_box(bg, 30, 110, 130, 40, PINK)
    # Pink placeholder bottom-right
    bg = draw_box(bg, W-180, H-340, 140, 50, PINK)
    # Canvas border (thin grey)
    bg = draw_box(bg, 10, 100, W-20, H-280, (180, 180, 180), thickness=2)

    # --- Headline (3-segment colored block) ---
    headline = get_headline(t)
    if headline:
        seg1, seg2, seg3 = headline
        fontsize = 110
        char_w = int(fontsize * 0.50)
        pad = 25
        w1 = len(seg1) * char_w + pad * 2
        w2 = len(seg2) * char_w + pad * 2
        w3 = len(seg3) * char_w + pad * 2
        total = w1 + w2 + w3
        start_x = max(-40, (W - total) // 2)
        HEAD_Y = 640
        HEAD_H = 180

        x1 = start_x
        x2 = x1 + w1
        x3 = x2 + w2

        # Seg 1: yellow box, black text
        bg = draw_box(bg, x1, HEAD_Y, w1, HEAD_H, YELLOW)
        bg = draw_text_pil(bg, seg1, x1+pad, HEAD_Y+30, fontsize, (0, 0, 0))
        # Seg 2: black box, white text
        bg = draw_box(bg, x2, HEAD_Y, w2, HEAD_H, BLACK)
        bg = draw_text_pil(bg, seg2, x2+pad, HEAD_Y+30, fontsize, (255, 255, 255))
        # Seg 3: pink box, black text
        bg = draw_box(bg, x3, HEAD_Y, w3, HEAD_H, PINK)
        bg = draw_text_pil(bg, seg3, x3+pad, HEAD_Y+30, fontsize, (0, 0, 0))

        # Green selection outline
        offset = 14
        cv2.rectangle(bg, (x1-offset, HEAD_Y-offset),
                      (x1+total+offset, HEAD_Y+HEAD_H+offset), GREEN, 4)
        # Corner handles
        hs = 18
        for cx, cy in [
            (x1-offset, HEAD_Y-offset),
            (x1+total+offset, HEAD_Y-offset),
            (x1-offset, HEAD_Y+HEAD_H+offset),
            (x1+total+offset, HEAD_Y+HEAD_H+offset),
        ]:
            bg = draw_box(bg, cx-hs//2, cy-hs//2, hs, hs, GREEN)

    # 3. Composite: speaker (frame) on TOP of overlay'd background
    # mask values: closer to 1 = speaker, closer to 0 = background
    # out = frame * mask + bg * (1-mask)
    composited = (frame.astype(np.float32) * mask_3 +
                  bg.astype(np.float32) * (1 - mask_3)).astype(np.uint8)

    # 4. Add caption box ON TOP (bottom, should be in front of everything)
    cap_text = get_caption(t)
    if cap_text:
        box_w = max(360, len(cap_text) * 26)
        box_x = (W - box_w) // 2
        box_y = H - 200
        # Black border
        cv2.rectangle(composited, (box_x-3, box_y-3), (box_x+box_w+3, box_y+90+3), (0, 0, 0), -1)
        # White fill
        cv2.rectangle(composited, (box_x, box_y), (box_x+box_w, box_y+90), WHITE, -1)
        # Text
        composited = draw_text_pil(composited, cap_text, 0, box_y+20, 36, (0, 0, 0))
        # Need to recenter — quick recompute since draw_text_pil doesn't auto-center
        # Re-render with centering by measuring
        # Use PIL to measure text and place centered
        img_rgb = cv2.cvtColor(composited, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(img_rgb)
        d = ImageDraw.Draw(pil)
        # First erase the un-centered text we just drew
        # Easier: redo this section without the bad draw
        # Reset: redraw box+text fresh
        cv2.rectangle(composited, (box_x-3, box_y-3), (box_x+box_w+3, box_y+90+3), (0, 0, 0), -1)
        cv2.rectangle(composited, (box_x, box_y), (box_x+box_w, box_y+90), WHITE, -1)
        font_obj = make_font(36)
        try:
            bbox = font_obj.getbbox(cap_text)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(cap_text) * 20
        tx = (W - tw) // 2
        composited = draw_text_pil(composited, cap_text, tx, box_y+22, 36, (0, 0, 0))

    writer.write(composited)
    frame_idx += 1
    if frame_idx % 60 == 0:
        elapsed = time.time() - t0_render
        rate = frame_idx / elapsed
        eta = (total_frames - frame_idx) / rate if rate > 0 else 0
        print(f"  {frame_idx}/{total_frames} ({100*frame_idx/total_frames:.1f}%) @ {rate:.1f}fps ETA {eta:.1f}s")

cap.release()
writer.release()
seg.close()
elapsed = time.time() - t0_render
print(f"\nRender complete: {frame_idx} frames in {elapsed:.1f}s ({frame_idx/elapsed:.1f} fps)")

# Add audio from original
print("Muxing audio...")
mux_cmd = [ffmpeg, "-y", "-i", str(OUT_VIDEO_NOAUDIO), "-i", BASE,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:a", "copy", "-shortest", str(OUT_FINAL)]
r = subprocess.run(mux_cmd, capture_output=True, text=True, timeout=180)
if r.returncode != 0:
    print("Mux err:", r.stderr[-800:])
else:
    print(f"Final: {OUT_FINAL.name} ({OUT_FINAL.stat().st_size/1024/1024:.2f} MB)")

# Remove tmp
try: os.unlink(OUT_VIDEO_NOAUDIO)
except Exception: pass

# Thumbnail at t=5
subprocess.run([ffmpeg, "-y", "-ss", "5", "-i", str(OUT_FINAL), "-frames:v", "1",
                "-vf", "scale=360:-2", "-q:v", "5", THUMB], check=True, capture_output=True)

# Send to Telegram
import requests
token = ""
with open(os.path.expanduser("~/.claw/env")) as fp:
    for line in fp:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.strip().split("=", 1)[1]
chat_id = "574707975"

caption = ("TEST v4 — overlay BEHIND speaker via mediapipe selfie segmentation. "
           "Headline 3-color sigue mismo formato pero ahora pasa DETRAS de tu silueta. "
           "Caption box bottom va arriba (en front, no detras). "
           "Beats: MARCAS QUE PIENSAN t2-8 / DR STRANGE 24/7 t10-16 / ENGINEERING WINS t18-24.")

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
