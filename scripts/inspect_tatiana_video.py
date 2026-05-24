import json
import os
import subprocess

path = "/Users/hector/Downloads/Tatiana Avatar.mov"
out = {"path": path, "exists": os.path.exists(path)}
if out["exists"]:
    st = os.stat(path)
    out["size_mb"] = round(st.st_size / 1048576, 2)

# Try AVFoundation via PyObjC (preinstalled on macOS python)
try:
    import objc  # noqa: F401
    from AVFoundation import AVAsset
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    asset = AVAsset.assetWithURL_(url)
    dur = asset.duration()
    seconds = dur.value / dur.timescale if dur.timescale else None
    out["duration_sec"] = round(seconds, 2) if seconds is not None else None

    tracks = asset.tracksWithMediaType_("vide")
    if tracks:
        t = tracks[0]
        size = t.naturalSize()
        out["width"] = int(size.width)
        out["height"] = int(size.height)
        out["video_fps"] = float(t.nominalFrameRate())
    audio = asset.tracksWithMediaType_("soun")
    out["audio_tracks"] = len(audio)
except Exception as e:
    out["avf_error"] = str(e)

print(json.dumps(out, indent=2))
