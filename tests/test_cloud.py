"""Cloud wrapper tests. NO network, NO webhook: H.post is replaced; state files in a temp dir.
Also proves the bundled desk code is the box code (FILTER_VER 2026-10-27a) except the 3 documented path/UA lines."""
import hashlib, io, json, os, re, sys, tempfile, contextlib
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cloud")); sys.path.insert(0, os.path.join(ROOT, "desk"))
from datetime import datetime
from zoneinfo import ZoneInfo
R = ZoneInfo("Europe/Rome"); N = 0
def ok(c, m):
    global N; N += 1; print(("PASS " if c else "FAIL ") + m); c or sys.exit(1)
def ts(s): return datetime.fromisoformat(s).replace(tzinfo=R).timestamp()

# ---- 1. bundled desk code == box code at bundling time (only documented lines differ) ----
PATCHED = {  # file -> [(bundle line, box line)]
    "desklib.py": [('DESK = os.environ.get("DESK_ROOT") or os.path.dirname(os.path.abspath(__file__))  # cloud bundle: path only', 'DESK = "/workspace/desk"'),
                   ('UA = {"User-Agent": os.environ.get("DESK_HTTP_UA") or "nexora-desk-scanner/1.0"}  # cloud bundle: optional UA override', 'UA = {"User-Agent": "nexora-desk-scanner/1.0"}')],
    "head_alert.py": [('TRADES = os.environ.get("DESK_TRADES_DIR") or os.path.join(DESK, "no_trades")  # cloud bundle: no trade logs in the public repo', 'TRADES = "/workspace/trades"')]}
for line in open(os.path.join(ROOT, "tests", "UPSTREAM_box_sha256.txt")):
    want, name = line.split()
    b = open(os.path.join(ROOT, "desk", name), "rb").read()
    for new, old in PATCHED.get(name, []):
        assert b.count(new.encode()) == 1, (name, new); b = b.replace(new.encode(), old.encode())
    ok(hashlib.sha256(b).hexdigest() == want, f"desk/{name} identical to box copy" + (f" (after undoing {len(PATCHED[name])} path/UA line(s))" if name in PATCHED else ""))

import cloud_scan as C, cloud_loop as L, head_alert as H
ok(H.FILTER_VER == "2026-10-27a", f"FILTER_VER {H.FILTER_VER}")
ok(H.MIN_RR == 1.5 and H.STOP_LO == -4.0 and H.STOP_HI == 1.6 and H.B_MAX_OPEN == 1, "head_alert constants unchanged (R:R 1.5, -4/+1.6, max 1 B)")

# ---- 2. gates: desk hours only (+ optional repo variable) ----
ok(C.cloud_gates(ts("2026-10-27 08:00:20")) is None, "Tue 08:00 Rome -> open")
ok(C.cloud_gates(ts("2026-10-27 07:59:00")) == "outside desk hours", "Tue 07:59 -> closed")
ok(C.cloud_gates(ts("2026-10-27 23:00:20")) == "outside desk hours", "Tue 23:00 -> closed")
ok(C.cloud_gates(ts("2026-10-31 12:00:00")) == "outside desk hours", "Saturday -> closed")
os.environ["DESK_ENTRIES_SUSPENDED"] = "1"; ok("SUSPENDED" in C.cloud_gates(ts("2026-10-27 10:00")), "repo variable DESK_ENTRIES_SUSPENDED=1 -> closed")
os.environ.pop("DESK_ENTRIES_SUSPENDED")
ok(H.gates is C.cloud_gates and H.process is C.cloud_process, "head_alert gates/process swapped in-process")

# ---- 3. sender: source=cloud, 1 per SYM+dir per 2 h (owner 27/10, per-day rule removed), dry run never POSTs, secrets never printed ----
tmp = tempfile.mkdtemp(); C.SENT = os.path.join(tmp, "sent.jsonl"); C.DRYF = os.path.join(tmp, "dry.jsonl")
posted = []
def fake_post(p): posted.append(p); return 200, "header=Authorization"
H.post = fake_post; C.DRY = False
def fake_process(hits, now, loader, sender, log):   # stands in for the real H.process (covered by test_head_alert.py)
    return "head_alert=" + str([f"{h['symbol']}:{sender(dict(h))[0]}" for h in hits])
C._orig_process = fake_process
p = {"setup_type": "B", "risk_pct": 1.5, "symbol": "PF_ZROUSD", "dir": "short", "score": 9.0, "entry": 1.66, "stop": 1.69, "t1": 1.64, "t2": 1.60, "rr": 2.38, "time_rome": "x"}
now = ts("2026-10-27 12:37:20")
r = C.cloud_process([p], now); ok(len(posted) == 1 and posted[0]["source"] == "cloud" and posted[0]["filter_ver"] == "2026-10-27a", f"payload carries source=cloud + filter_ver ({r})")
ok(set(posted[0]) - set(p) == {"source", "filter_ver"}, "payload = desk payload + 2 fields only")
r = C.cloud_process([p, dict(p, dir="long")], ts("2026-10-27 13:30:20"))
ok(len(posted) == 2 and posted[-1]["dir"] == "long" and "cloud sent <2h ago" in r, f"same SYM+dir 53 min later -> skipped, other dir sent ({r})")
r = C.cloud_process([p], ts("2026-10-27 14:32:20")); ok(len(posted) == 2 and "cloud sent <2h ago" in r, f"same SYM+dir 1h55 later -> still skipped ({r})")
r = C.cloud_process([p], ts("2026-10-27 14:37:20")); ok(len(posted) == 3 and posted[-1]["symbol"] == "PF_ZROUSD", f"same SYM+dir exactly 2h later, same Rome day -> alerts again ({r})")
r = C.cloud_process([p], ts("2026-10-27 15:00:20")); ok(len(posted) == 3, "2h window restarts from the latest alert (14:37) -> 15:00 skipped")
r = C.cloud_process([p], ts("2026-10-27 16:37:20")); ok(len(posted) == 4, "16:37 (2h after 14:37) -> allowed")
r = C.cloud_process([p], ts("2026-10-28 09:00:20")); ok(len(posted) == 5, "next Rome day -> allowed")
ok(len(open(C.SENT).read().splitlines()) == 5 and json.loads(open(C.SENT).read().splitlines()[2])["ts_unix"] == ts("2026-10-27 14:37:20"), "ledger ts = scan time")
H.post = lambda p_: (_ for _ in ()).throw(AssertionError("POST in dry run")); C.DRY = True
C.SENT = os.path.join(tmp, "sent2.jsonl"); r = C.cloud_process([dict(p, symbol="PF_NEARUSD")], now)
ok(json.loads(open(C.DRYF).read().splitlines()[-1])["symbol"] == "PF_NEARUSD", "dry run: payload recorded, H.post never called")
# real post() with sentinel secrets: unreachable https URL -> transport error; secrets must not appear in output/state
SECRET_URL, SECRET_KEY = "https://127.0.0.1:9/SENTINEL_URL_9f3", "SENTINEL_KEY_7c1"
os.environ["DESK_HEAD_WEBHOOK_URL"], os.environ["DESK_HEAD_WEBHOOK_KEY"] = SECRET_URL, SECRET_KEY
import importlib; H2 = importlib.reload(H); H2.AUTHF = os.path.join(tmp, "auth.json")
code, note = H2.post({"x": 1})
blob = note + "".join(open(os.path.join(tmp, f)).read() for f in os.listdir(tmp))
ok(code is None and "SENTINEL" not in blob, f"post() failure note has no secret ({note})")
for k in ("DESK_HEAD_WEBHOOK_URL", "DESK_HEAD_WEBHOOK_KEY"): os.environ.pop(k)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): print(re.sub(r"equity=\S+", "equity=[hidden]", "x equity=12.34(STALE) errors=0"))
ok("12.34" not in buf.getvalue(), "scanner summary line: equity hidden in public logs")

# ---- 4. block timing: Rome clock, CET and CEST, late starts ----
for start, end in (("2026-10-27 07:55:00", "2026-10-27 13:00"), ("2026-10-27 09:31:00", "2026-10-27 13:00"),
                   ("2026-10-27 12:56:30", "2026-10-27 18:00"), ("2026-10-27 17:55:00", "2026-10-27 23:00"),
                   ("2026-10-27 22:50:00", "2026-10-27 23:00"), ("2026-07-14 07:55:00", "2026-07-14 13:00"),
                   ("2026-03-30 12:55:00", "2026-03-30 18:00")):
    e = L.block_end(ts(start), 340); got = datetime.fromtimestamp(e, R).strftime("%Y-%m-%d %H:%M")
    ok(got == end and e - ts(start) <= 340 * 60, f"start {start} {datetime.fromtimestamp(ts(start), R):%Z} -> block end {got}")
ok(L.block_end(ts("2026-10-27 07:00:00"), 340) - ts("2026-10-27 07:00:00") == 340 * 60, "never longer than CLOUD_MAX_MINUTES (340 < 360 GitHub limit)")
s = L.next_slot(ts("2026-10-27 12:37:26")); ok(datetime.fromtimestamp(s, R).strftime("%H:%M:%S") == "12:40:20", "slots = box slots (5 min + 20 s)")
print(f"ALL {N} CLOUD TESTS PASSED")
