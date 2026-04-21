#!/usr/bin/env python3
"""Embed hero image as base64 into landing HTML. Uses sips for compression."""
import base64, subprocess, os
from pathlib import Path

base = Path("/Users/hector/Projects/Dr.-strange/captures")
hero_png = base / "hero-desert-house.png"
hero_jpg = Path("/tmp/hero-compressed.jpg")
html_path = base / "pachano-design" / "landing-prototype.html"

# Convert PNG to JPEG using macOS sips
subprocess.run([
    "sips", "-s", "format", "jpeg",
    "-s", "formatOptions", "75",
    str(hero_png), "--out", str(hero_jpg)
], capture_output=True)

print(f"Original PNG: {hero_png.stat().st_size/1024:.0f} KB")
print(f"Compressed JPEG: {hero_jpg.stat().st_size/1024:.0f} KB")

b64 = base64.b64encode(hero_jpg.read_bytes()).decode("ascii")
data_uri = f"data:image/jpeg;base64,{b64}"

html = html_path.read_text()
old = "url('../hero-desert-house.png')"
new = f"url('{data_uri}')"
html = html.replace(old, new)

html_path.write_text(html)
print(f"HTML updated: {len(html)/1024:.0f} KB total")
