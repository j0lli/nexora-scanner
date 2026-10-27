#!/usr/bin/env python3
"""ONE cloud scan (one 5-minute slot). Runs the UNCHANGED desk scanner (desk/scanner.py, desk/head_alert.py,
FILTER_VER 2026-10-27a) and only swaps the parts that need private box data:

  * gates(): the box gate needs desk/state/equity.json (live account) and the ENTRIES_SUSPENDED flag. The cloud has
    neither, so here the gate is desk hours only (Mon-Fri 08:00-23:00 Europe/Rome) plus an optional repository
    variable DESK_ENTRIES_SUSPENDED=1. The daily -4% / +1.6% gates, suspension and the "max 1 open Type B" check
    are re-applied on the box when the alert arrives (payload carries source="cloud").
  * sender: payload gets "source":"cloud" and "filter_ver"; one alert per SYMBOL+direction per Rome day
    (ledger desk/state/cloud_sent.jsonl, kept across blocks with actions/cache). CLOUD_DRY_RUN=1 never POSTs.
  * stdout: the scanner's own summary line is printed without the equity figure (public Actions logs).

Every score / R:R / Type A-B / filter function is the desk's own code, imported as-is. No Kraken keys, no orders.
Secrets DESK_HEAD_WEBHOOK_URL / DESK_HEAD_WEBHOOK_KEY are read only by head_alert.post() at send time; never printed."""
import contextlib, io, json, os, re, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK = os.environ.get("DESK_ROOT") or os.path.join(ROOT, "desk")
os.environ["DESK_ROOT"] = DESK
sys.path.insert(0, DESK)
ROME = ZoneInfo("Europe/Rome")
DRY = os.environ.get("CLOUD_DRY_RUN", "0") == "1"

import head_alert as H  # noqa: E402  (unchanged desk module)

SENT = os.path.join(H.STATE, "cloud_sent.jsonl")
DRYF = os.path.join(H.STATE, "cloud_dryrun_payloads.jsonl")
os.makedirs(H.STATE, exist_ok=True); os.makedirs(H.CACHE, exist_ok=True)


def rome_day(ts):
    return datetime.fromtimestamp(ts, ROME).strftime("%Y-%m-%d")


def cloud_gates(now, *_a, **_k):
    t = datetime.fromtimestamp(now, ROME)
    if t.isoweekday() > 5 or not (8 <= t.hour < 23): return "outside desk hours"
    if os.environ.get("DESK_ENTRIES_SUSPENDED", "").strip() == "1": return "ENTRIES_SUSPENDED (repo variable)"
    return None


def sent_today(now):
    day, out = rome_day(now), set()
    if os.path.exists(SENT):
        for line in open(SENT):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("day") == day: out.add((r["symbol"], r["dir"]))
    return out


def cloud_sender(p):
    payload = {**p, "source": "cloud", "filter_ver": H.FILTER_VER}
    if DRY:
        with open(DRYF, "a") as f: f.write(json.dumps(payload) + "\n")
        code, note = 200, "DRY-RUN (not sent)"
    else:
        code, note = H.post(payload)          # desk's own POST: https only, Bearer / X-Webhook-Key, 5 s timeout
    if code is not None and 200 <= code < 300:
        with open(SENT, "a") as f:
            f.write(json.dumps({"day": rome_day(time.time()), "ts_unix": int(time.time()), "symbol": p["symbol"],
                                "dir": p["dir"], "setup_type": p["setup_type"], "dry_run": DRY}) + "\n")
    return code, note


_orig_process = H.process


def cloud_process(hits, now=None, loader=H._candles, sender=None, log=print):
    """Same H.process (same qualify/B limits/2h dedup), with the cloud sender and the 1-per-day SYM+dir rule."""
    now = now or time.time(); done = sent_today(now)
    keep = [h for h in hits if (h.get("symbol"), h.get("dir")) not in done]
    dup = [f"{h['symbol'].replace('PF_', '')}:{h['dir'][0]}:skip[cloud already sent today]" for h in hits
           if (h.get("symbol"), h.get("dir")) in done and h.get("setup_type") in ("A", "B")]
    r = _orig_process(keep, now, loader, cloud_sender, log)
    return r + (f" {dup}" if dup else "")


H.gates = cloud_gates
H.process = cloud_process


def main():
    import fcntl
    lk = open(os.path.join(DESK, ".scanner_run.lock"), "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print("another scanner run in progress, skip"); return 0
    import scanner                        # unchanged desk scanner; head_alert above is already patched in sys.modules
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf): scanner.main()
    finally:
        out = re.sub(r"equity=\S+", "equity=[hidden]", buf.getvalue())
        print(("[DRY-RUN] " if DRY else "") + out.strip(), flush=True)
    return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception:
        import traceback; traceback.print_exc(); sys.exit(1)
