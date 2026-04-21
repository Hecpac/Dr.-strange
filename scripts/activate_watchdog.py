import subprocess
subprocess.run(["launchctl", "load", "/Users/hector/Library/LaunchAgents/com.claw.vercel-watchdog.plist"])
print("Watchdog launchd agent loaded.")
subprocess.run(["launchctl", "list", "com.claw.vercel-watchdog"])
