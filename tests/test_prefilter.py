"""Unit tests for prefilter.py (synthetic candles, no network). Run: python3 -B test_prefilter.py"""
import sys, os, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desk"))
import numpy as np, prefilter as P
from datetime import datetime
from zoneinfo import ZoneInfo
R = ZoneInfo("Europe/Rome"); ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else: fail += 1; print("FAIL", name)
NOW = datetime(2026, 10, 27, 15, 10, tzinfo=R).timestamp()
def bars(tf, highs, lows=None, end=NOW):
    sec = P.TF[tf]; n = len(highs); t0 = (end // sec) * sec - n * sec   # all closed before `end`
    lows = lows if lows is not None else [h - 1 for h in highs]
    return np.array([[t0 + k * sec, (h + l) / 2, h, l, (h + l) / 2, 1] for k, (h, l) in enumerate(zip(highs, lows))], float)
def loader(c4=None, c1=None):
    return lambda s, tf: c4 if tf == "4h" else c1
flat1 = bars("1h", [100] * 20)
# ---- F1 structural stop (short)
c4 = bars("4h", [100, 100, 102, 100, 100, 100, 100, 100])     # pivot high 102 (k=2) above entry 100.5
h = {"symbol": "X", "dir": "short", "entry": 100.5, "stop": 101.5, "setup_type": "B", "last": 100.5}
check("F1 short stop inside pivot -> drop", not P.check_struct(h, NOW, loader(c4, flat1))[0])
h2 = dict(h, stop=102.3); check("F1 short stop beyond pivot -> keep", P.check_struct(h2, NOW, loader(c4, flat1))[0])
c4far = bars("4h", [100, 100, 110, 100, 100, 100, 100, 100])   # 9.5% away > 3% bound -> ignored
check("F1 swing beyond 3% ignored", P.check_struct(h, NOW, loader(c4far, flat1))[0])
c4tail = bars("4h", [100, 100, 100, 100, 100, 100, 100, 101.8])  # fresh unconfirmed high in last k bars
check("F1 fresh unconfirmed 4H high above stop -> drop", not P.check_struct(h, NOW, loader(c4tail, flat1))[0])
c4old = bars("4h", [100, 100, 102] + [100] * 40)                # pivot older than 30 bars -> ignored
check("F1 pivot outside 30-bar lookback ignored", P.check_struct(h, NOW, loader(c4old, flat1))[0])
c4none = bars("4h", [100] * 10, [99] * 10)
check("F1 no swing on stop side -> keep", P.check_struct(dict(h, entry=100.5), NOW, loader(bars("4h", [100.2] * 10), flat1))[0])
# open (forming) 4H bar must be ignored
c4f = np.vstack([bars("4h", [100] * 8), [NOW // 14400 * 14400, 100, 105, 99, 100, 1]])
check("F1 forming 4H bar ignored", P.check_struct(h, NOW, loader(c4f, flat1))[0])
# ---- F1 long mirror
c4L = bars("4h", [101] * 8, [100, 100, 98, 100, 100, 100, 100, 100])
hl = {"symbol": "X", "dir": "long", "entry": 99.5, "stop": 98.5, "setup_type": "A", "last": 99.5}
check("F1 long stop above pivot low -> drop", not P.check_struct(hl, NOW, loader(c4L, flat1))[0])
check("F1 long stop below pivot low -> keep", P.check_struct(dict(hl, stop=97.8), NOW, loader(c4L, flat1))[0])
# ---- F2 counter-move
c1 = bars("1h", [100, 100, 100, 100, 100, 100, 100, 101])       # last closed bar new high > prev 6
check("F2 short last closed new high -> drop", not P.check_counter(h, NOW, loader(c4, c1))[0])
c1b = bars("1h", [100, 101, 100, 100, 100, 100, 100, 100.5])     # last high 100.5 < 101 in prev 6
check("F2 short no new high -> keep", P.check_counter(h, NOW, loader(c4, c1b))[0])
c1c = bars("1h", [101, 100, 100, 100, 100, 100, 100, 100.5])     # 101 is 7 bars back -> outside window -> new high
check("F2 window is exactly 6 bars", not P.check_counter(h, NOW, loader(c4, c1c))[0])
c1f = np.vstack([c1b, [NOW // 3600 * 3600, 100, 101.2, 100, 100.5, 1]])   # forming bar breaks last 6 highs
check("F2 forming bar new high -> drop", not P.check_counter(h, NOW, loader(c4, c1f))[0])
c1L = bars("1h", [101] * 8, [100, 100, 100, 100, 100, 100, 100, 99])
check("F2 long last closed new low -> drop", not P.check_counter(hl, NOW, loader(c4L, c1L))[0])
check("F2 long no new low -> keep", P.check_counter(hl, NOW, loader(c4L, bars("1h", [101] * 8, [100] * 8)))[0])
check("F2 short mark adverse > 0.1R -> drop", not P.check_counter(dict(h, last=100.7), NOW, loader(c4, c1b))[0])
check("F2 short mark adverse within 0.1R -> keep", P.check_counter(dict(h, last=100.55), NOW, loader(c4, c1b))[0])
check("F2 short mark below entry (favourable) -> keep", P.check_counter(dict(h, last=100.0), NOW, loader(c4, c1b))[0])
check("F2 long mark adverse -> drop", not P.check_counter(dict(hl, last=99.3), NOW, loader(c4L, bars("1h", [101] * 8, [100] * 8)))[0])
check("F2 explicit mark arg wins", not P.check_counter(h, NOW, loader(c4, c1b), mark=101.0)[0])
# ---- F3 time
ts = lambda hh, mm: datetime(2026, 10, 27, hh, mm, tzinfo=R).timestamp()
check("F3 22:29 ok", P.check_time(ts(22, 29))[0]); check("F3 22:30 drop", not P.check_time(ts(22, 30))[0])
check("F3 22:55 drop", not P.check_time(ts(22, 55))[0]); check("F3 08:00 ok", P.check_time(ts(8, 0))[0])
check("F3 uses Rome time across DST (CEST 22:31 drop)", not P.check_time(datetime(2026, 10, 20, 22, 31, tzinfo=R).timestamp())[0])
# ---- judge / filter_hits / install
good = dict(h, stop=102.3)
j = P.judge(good, NOW, loader(c4, c1b)); check("judge keep", j["keep"] and j["fail"] == [])
j = P.judge(h, ts(22, 40), loader(c4, c1)); check("judge all three fail", set(j["fail"]) == {"F1_struct", "F2_counter", "F3_time"})
j = P.judge(h, NOW, lambda s, tf: 1 / 0); check("judge fail-open on loader error", j["keep"] and "errors" in j["info"])
untyped = {"symbol": "X", "dir": "short", "entry": 1, "stop": 2, "setup_type": None}
import tempfile; lg = os.path.join(tempfile.mkdtemp(), "pf.jsonl")
out = P.filter_hits([h, good, untyped], NOW, loader(c4, c1b), "on", lg)
check("filter on drops bad, keeps good + untyped", out == [good, untyped])
check("log written 2 rows (untyped not judged)", len(open(lg).read().splitlines()) == 2)
out = P.filter_hits([h, good], NOW, loader(c4, c1b), "shadow", None); check("shadow keeps all", out == [h, good])
out = P.filter_hits([h, good], NOW, loader(c4, c1b), "off", None); check("off keeps all", out == [h, good])
# ---- per-check modes (owner t64u: F3 on, F1/F2 shadow)
import json as _j
for k in ("DESK_PREFILTER", "DESK_PREFILTER_F3"): os.environ.pop(k, None)
check("default env modes = (shadow, on)", P.env_modes() == ("shadow", "on"))
os.environ["DESK_PREFILTER"] = "bogus"; os.environ["DESK_PREFILTER_F3"] = "ON "
check("unknown mode -> default, case/space tolerant", P.env_modes() == ("shadow", "on"))
for k in ("DESK_PREFILTER", "DESK_PREFILTER_F3"): os.environ.pop(k, None)
lg2 = os.path.join(tempfile.mkdtemp(), "state", "prefilter_shadow.jsonl")
late = ts(22, 40)
out = P.filter_hits([h, good, untyped], late, loader(c4, c1), log_path=lg2)       # env defaults
check("defaults: F3 drops every A/B hit after 22:30", out == [untyped])
rows = [_j.loads(x) for x in open(lg2)]
check("shadow log has one row per judged hit", len(rows) == 2)
r0 = rows[0]
check("row has per-check verdicts", r0["verdict"] == {"F1_struct": "FAIL", "F2_counter": "FAIL", "F3_time": "FAIL"})
check("row dropped_by only F3 (F1/F2 shadow)", r0["dropped"] and r0["dropped_by"] == ["F3_time"] and r0["mode_f12"] == "shadow" and r0["mode_f3"] == "on")
check("row would_drop_if_all_on", r0["would_drop_if_all_on"] is True)
lg3 = os.path.join(tempfile.mkdtemp(), "s.jsonl")
out = P.filter_hits([h, good], NOW, loader(c4, c1), log_path=lg3)               # 15:10, h fails F1+F2 only
check("defaults before 22:30: F1/F2 failures are NOT dropped", out == [h, good])
rr = [_j.loads(x) for x in open(lg3)]
check("shadow verdict F1/F2 FAIL logged, not dropped", rr[0]["verdict"]["F1_struct"] == "FAIL" and not rr[0]["dropped"] and rr[0]["verdict"]["F3_time"] == "pass")
check("good hit: F2 FAIL logged (c1 new high), kept", rr[1]["verdict"]["F2_counter"] == "FAIL" and not rr[1]["dropped"])
out = P.filter_hits([h], late, loader(c4, c1), "shadow", None, "shadow"); check("F3 shadow -> nothing dropped", out == [h])
out = P.filter_hits([h], late, loader(c4, c1), "shadow", None, "off"); check("F3 off -> nothing dropped", out == [h])
out = P.filter_hits([h], NOW, loader(c4, c1), "on", None, "off"); check("F1/F2 on still drops", out == [])
out = P.filter_hits([h], late, loader(c4, c1b), "off", None, "on"); check("F1/F2 off + F3 on drops late", out == [])
msgs = []; P.filter_hits([h], late, loader(c4, c1), log_path=None, log=msgs.append)
check("log callback line (cloud output)", len(msgs) == 1 and "F3=FAIL DROPPED" in msgs[0] and "F1=FAIL" in msgs[0])
os.environ["DESK_PREFILTER_F3"] = "shadow"; out = P.filter_hits([h], late, loader(c4, c1), log_path=None)
check("env DESK_PREFILTER_F3=shadow respected", out == [h]); os.environ.pop("DESK_PREFILTER_F3")
seen = {}
H = types.SimpleNamespace(_candles=loader(c4, c1b))
def orig(hits, now=None, loader=None, sender=None, log=print): seen["hits"] = hits; seen["sender"] = sender; return "orig"
H.process = orig
P.install(H, "on", log_path=lg); P.install(H, "on")
r = H.process([h, good], NOW, None, "SENDER")
check("install wraps process, passes sender through", r == "orig" and seen["hits"] == [good] and seen["sender"] == "SENDER")
check("install idempotent", getattr(H.process, "_prefilter", False) and H.process.__closure__ is not None)
H2 = types.SimpleNamespace(_candles=loader(c4, c1b), process=orig); P.install(H2, "shadow", log_path=lg); H2.process([h, good], NOW)
check("install shadow passes all", seen["hits"] == [h, good])
H3 = types.SimpleNamespace(_candles=loader(c4, c1), process=orig); P.install(H3, log_path=lg)
check("install defaults = shadow/on", H3.process._mode == "shadow" and H3.process._mode_f3 == "on")
H3.process([h, good, untyped], NOW); check("install defaults 15:10 keep all", seen["hits"] == [h, good, untyped])
H3.process([h, good, untyped], late); check("install defaults 22:40 drop A/B", seen["hits"] == [untyped])
print(f"prefilter tests: {ok} passed, {fail} failed"); sys.exit(1 if fail else 0)
