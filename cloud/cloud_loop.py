#!/usr/bin/env python3
"""One GitHub Actions block: scan every 5-minute wall-clock slot (+20 s, same slots as the box scanner_loop.sh)
until the end of the current desk block, then exit 0.

Blocks (Europe/Rome, Mon-Fri): 08:00-13:00, 13:00-18:00, 18:00-23:00. The block end is the first boundary in
BLOCK_ENDS that is more than START_GRACE_MIN after the ACTUAL start (GitHub cron can start late or early), and the run
never lasts more than CLOUD_MAX_MINUTES (default 340, under GitHub's 360-minute job limit). All times come from
zoneinfo Europe/Rome, so CET (UTC+1) and CEST (UTC+2) are both handled, whatever the runner's clock zone.
Outside Mon-Fri 08:00-23:00 Rome it exits at once (a late catch-up costs ~1 minute)."""
import os, subprocess, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
HERE = os.path.dirname(os.path.abspath(__file__))
SLOT, OFFSET = 300, int(os.environ.get("CLOUD_SLOT_OFFSET_S", "20"))
BLOCK_ENDS = (13, 18, 23)                      # Rome hours
START_GRACE_MIN = 15                           # a job starting 12:45+ belongs to the 13:00-18:00 block
SCAN_TIMEOUT = 240


def in_desk_hours(ts):
    t = datetime.fromtimestamp(ts, ROME)
    return t.isoweekday() <= 5 and 8 <= t.hour < 23


def block_end(start, max_minutes):
    t = datetime.fromtimestamp(start + START_GRACE_MIN * 60, ROME)
    end = None
    for h in BLOCK_ENDS:
        cand = t.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > t: end = cand; break
    if end is None:                              # 22:45-23:00 Rome: finish the last slots of the day
        end = datetime.fromtimestamp(start, ROME).replace(hour=23, minute=0, second=0, microsecond=0)
        if end.timestamp() <= start: return start
    return min(end.timestamp(), start + max_minutes * 60)


def next_slot(now):
    return now - now % SLOT + SLOT + OFFSET


def main():
    start = float(os.environ.get("CLOUD_JOB_START_UNIX") or time.time())   # job start, set by the workflow's first step
    max_min = float(os.environ.get("CLOUD_MAX_MINUTES", "340"))
    max_scans = int(os.environ.get("CLOUD_MAX_SCANS") or 0)
    now = time.time()
    t = datetime.fromtimestamp(now, ROME)
    if t.isoweekday() > 5 or t.hour >= 23 or t.hour < 7:
        print(f"{t:%F %T %Z} outside desk days/hours, nothing to do"); return 0
    end = block_end(start, max_min)
    print(f"block start {datetime.fromtimestamp(start, ROME):%F %T %Z}, end {datetime.fromtimestamp(end, ROME):%F %T %Z}, "
          f"slots +{OFFSET}s, dry_run={os.environ.get('CLOUD_DRY_RUN', '0')}", flush=True)
    now = time.time(); slot = now - now % SLOT + OFFSET          # this slot if its +20 s mark is still ahead
    if slot <= now: slot += SLOT
    scans = 0
    while slot < end:
        while time.time() < slot: time.sleep(min(5, max(0.2, slot - time.time())))
        if in_desk_hours(time.time()):
            try:
                r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "cloud_scan.py")], timeout=SCAN_TIMEOUT)
                if r.returncode: print(f"{datetime.now(ROME):%F %T %Z} scanner exit {r.returncode}", flush=True)
            except subprocess.TimeoutExpired:
                print(f"{datetime.now(ROME):%F %T %Z} scanner timeout {SCAN_TIMEOUT}s", flush=True)
            scans += 1
            if max_scans and scans >= max_scans: break
        slot = next_slot(time.time())
    print(f"{datetime.now(ROME):%F %T %Z} block done: {scans} scans", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
