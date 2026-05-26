"""Check if peekaboo has a 'browser' subcommand for Chrome CDP integration."""
import subprocess

PEEKABOO = "/opt/homebrew/bin/peekaboo"

def run(args, timeout=10):
    r = subprocess.run([PEEKABOO] + args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

# Check browser subcommand
print("=== peekaboo browser --help ===")
rc, out, err = run(['browser', '--help'])
print(f"rc={rc}")
print(out[:3000] if out else err[:1000])

# Check tools list
print("\n\n=== peekaboo tools --help ===")
rc, out, err = run(['tools', '--help'])
print(f"rc={rc}")
print(out[:1500] if out else err[:500])

# Get permissions status
print("\n\n=== peekaboo permissions status ===")
rc, out, err = run(['permissions', 'status'])
print(f"rc={rc}")
print(out[:1500] if out else err[:500])
