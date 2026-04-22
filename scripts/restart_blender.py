import subprocess
import time

subprocess.run(["pkill", "-f", "Blender"], timeout=5)
time.sleep(2)
subprocess.Popen(["/opt/homebrew/bin/blender"])
print("Blender restarted — MCP server will auto-start in ~5 seconds")
