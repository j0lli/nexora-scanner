"""Runs every bundled test (no network except Kraken's public instruments list for the xStock set; no webhook)."""
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
env = {k: v for k, v in os.environ.items() if k not in ("DESK_HEAD_WEBHOOK_URL", "DESK_HEAD_WEBHOOK_KEY")}
bad = 0
for t in ("test_head_alert.py", "test_bounce_filter.py", "test_type_b_stop.py", "test_cloud.py"):
    r = subprocess.run([sys.executable, "-B", os.path.join(HERE, t)], env=env, capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(f"{'OK  ' if r.returncode == 0 else 'FAIL'} {t}: {last}")
    if r.returncode: bad += 1; print(r.stdout[-3000:], r.stderr[-3000:])
sys.exit(1 if bad else 0)
