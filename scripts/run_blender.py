import subprocess
r = subprocess.run(
    ["/opt/homebrew/bin/blender", "--background", "--python",
     "/Users/hector/Projects/Dr.-strange/scripts/blender_portrait.py"],
    capture_output=True, text=True, timeout=300
)
print(r.stdout[-2000:] if len(r.stdout) > 2000 else r.stdout)
if r.returncode != 0:
    print(f"STDERR: {r.stderr[-1000:]}")
