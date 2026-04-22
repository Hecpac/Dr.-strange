import urllib.request
import ssl

# Upscaled image ID from Midjourney CDN
img_id = "9b1d0685-5c2c-48f6-a3c4-86c260e0040d"

# Try full resolution PNG first, then webp
urls = [
    f"https://cdn.midjourney.com/{img_id}/0_0.png",
    f"https://cdn.midjourney.com/{img_id}/0_0.webp",
    f"https://cdn.midjourney.com/{img_id}/0_0_3072_N.webp",
    f"https://cdn.midjourney.com/{img_id}/0_0_2048_N.webp",
    f"https://cdn.midjourney.com/{img_id}/0_0_1024_N.webp",
]

output_path = "/Users/hector/Projects/Dr.-strange/captures/pachano-design/assets/hero-midjourney.png"

ctx = ssl.create_default_context()

for url in urls:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        data = resp.read()
        if len(data) > 10000:
            # Determine extension
            ext = ".webp" if "webp" in url else ".png"
            out = output_path.replace(".png", ext)
            with open(out, "wb") as f:
                f.write(data)
            print(f"Downloaded: {url}")
            print(f"Size: {len(data)} bytes ({len(data)/1024:.0f} KB)")
            print(f"Saved to: {out}")
            break
        else:
            print(f"Too small ({len(data)} bytes): {url}")
    except Exception as e:
        print(f"Failed: {url} — {e}")
else:
    print("All download attempts failed")
