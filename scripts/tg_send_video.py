import os, sys, urllib.request, json
from pathlib import Path

dotenv = Path(__file__).resolve().parent.parent / ".env"
env = {}
for line in dotenv.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

token = env.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env.get("TELEGRAM_ALLOWED_USER_ID", "")
video_path = sys.argv[1]
caption = sys.argv[2] if len(sys.argv) > 2 else ""

if not token or not chat_id:
    print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID in .env")
    sys.exit(1)

boundary = "----PythonFormBoundary7MA4YWxkTrZu0gW"
video_data = Path(video_path).read_bytes()
filename = Path(video_path).name

body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'
    f"Content-Type: video/mp4\r\n\r\n"
).encode() + video_data + f"\r\n--{boundary}--\r\n".encode()

url = f"https://api.telegram.org/bot{token}/sendVideo"
req = urllib.request.Request(url, data=body, method="POST")
req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

resp = urllib.request.urlopen(req, timeout=30)
result = json.loads(resp.read())
if result.get("ok"):
    print(f"Video sent to chat {chat_id}")
else:
    print(f"Error: {result}")
