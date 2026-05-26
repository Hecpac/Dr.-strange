"""V2: Discover the correct peekaboo CLI syntax via --help on each subcommand."""
import subprocess

PEEKABOO = "/opt/homebrew/bin/peekaboo"

def run(args, timeout=20):
    try:
        r = subprocess.run([PEEKABOO] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, '', str(e)

# Get help for each subcommand we want to use
for sub in ['list', 'inspect_ui', 'see', 'app', 'permissions']:
    print(f"\n=========== peekaboo {sub} --help ===========")
    rc, out, err = run([sub, '--help'])
    print(f"rc={rc}")
    print(out[:2000] if out else err[:1000])
