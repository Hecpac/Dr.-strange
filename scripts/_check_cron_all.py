import sqlite3, datetime

conn = sqlite3.connect("data/claw.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT job_name, last_run_at, runs FROM cron_state ORDER BY last_run_at DESC"
).fetchall()
for r in rows:
    ts = datetime.datetime.fromtimestamp(r["last_run_at"]).strftime("%Y-%m-%d %H:%M") if r["last_run_at"] > 0 else "NEVER"
    print(f'{r["job_name"]:35s}  last_run={ts}  runs={r["runs"]}')
print(f"\nTotal jobs: {len(rows)}")
