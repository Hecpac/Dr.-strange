import sqlite3, datetime

conn = sqlite3.connect("data/claw.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT job_name, last_run_at, runs FROM cron_state ORDER BY last_run_at DESC LIMIT 15"
).fetchall()
for r in rows:
    ts = datetime.datetime.fromtimestamp(r["last_run_at"]).strftime("%Y-%m-%d %H:%M")
    print(f'{r["job_name"]:30s}  last_run={ts}  runs={r["runs"]}')
conn.close()
