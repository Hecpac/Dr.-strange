import subprocess
import os

os.chdir("/Users/hector/Projects/PHD")

subprocess.run(["git", "commit", "-m",
    "fix: replace default Vercel favicon with PHD brand logo\n\n"
    "Google was showing the Vercel triangle instead of the company logo\n"
    "in search results.\n\n"
    "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"])

subprocess.run(["git", "push", "origin", "main"])
