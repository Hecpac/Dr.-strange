import pathlib
from datetime import datetime

# Search wider - Telegram downloads, Photos, etc.
search_paths = [
    "/Users/hector/Downloads",
    "/Users/hector/Desktop",
    "/Users/hector/Documents",
    "/Users/hector/Pictures",
    "/Users/hector/Library/Group Containers",
]

for base in search_paths:
    p = pathlib.Path(base)
    if not p.exists():
        continue
    for ext in ["*.jpg", "*.jpeg", "*.png"]:
        for f in p.rglob(ext):
            if f.stat().st_size > 100000:  # >100KB (portrait-size)
                age_hours = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
                if age_hours < 48:
                    size_kb = f.stat().st_size // 1024
                    print(f"{f} ({size_kb}KB)")
