import os as _os, sys as _sys; DESK_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "desk")  # cloud bundle path
"""2026-10-23 v3 Type B stop rule tests: stop beyond the nearest REAL unbroken 1H swing (pivot confirmed by >= 1 closed bar each
side) + 0.1 ATR, floored (outward) at max(0.5 ATR1h, 0.4%), capped at 3% (features.MAX_STOP_PCT). 4H struct must not point
against the trade; rebound 0.5-1.5 ATR. No network: synthetic swings + XPL fixture (closed bars 2026-10-23 08:05:30 Rome)."""
import sys, os, json, numpy as np
sys.dont_write_bytecode = True; sys.path.insert(0, DESK_DIR)
import setup_types as S, features
from features import build_ctx
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or sys.exit(1)
ok(S.B_MIN_STOP_ATR == 1.0 and S.B_MIN_STOP_PCT == 0.4, "B stop floor = max(1.0 ATR1h, 0.4%) (v4)")
ok(S.B_MAX_STOP_PCT == features.MAX_STOP_PCT == 3.0, "B max stop 3% = features.MAX_STOP_PCT (scanner A/B hard cap)")
ok(S.B_SWING_K == 1 and S.B_MIN_REBOUND_ATR == 0.5, "swing pivot confirmation 1 bar; rebound min 0.5 ATR")
ok(S.B_MIN_SCORE == 7.5 and S.B_RISK_PCT == 1.5 and S.MIN_RR == 1.5 and S.B_MAX_DIST == 1.5 and S.B_LEVEL_ATR == 0.5, "other Type B params unchanged (score 7.5, risk 1.5%, RR 1.5, rebound max 1.5 ATR, level 0.5 ATR)")
def mkH(h, l=None): h = np.array(h, float); return {"h": h, "l": np.array(l, float) if l is not None else h - 2.0}
base = [99.0] * 40
def peaks(pk, tail):
    h = list(base)
    for idx, v in pk: h[idx] = v
    return h + tail
E, A1 = 100.0, 1.0
# 1) XPL pattern: rebound high is the LIVE (last closed, unconfirmed) bar; older pivot 100.45 broken -> stop beyond intact 101.6
h = peaks([(20, 101.6), (32, 100.45)], [99.5, 99.8, 100.1, 100.3, 100.5]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is not None and inf["stop_swing"] == 101.6 and abs(st - 101.7) < 1e-9, f"XPL pattern: unconfirmed rebound high 100.5 not used -> stop beyond real swing 101.6 ({st:.2f})")
# 2) XRP pattern: rebound high confirmed by 1 lower bar after it; stop+buffer 0.25 ATR -> floored outward to 0.5 ATR, still beyond swing
h = peaks([(20, 102.0)], [99.6, 99.8, 100.15, 100.05]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is not None and inf["stop_swing"] == 100.15 and inf["stop_floored"] and abs(st - 101.0) < 1e-9 and st > 100.15, f"XRP pattern: 1-bar-confirmed rebound high 100.15 -> stop floored to 1.0 ATR = {st:.2f} (beyond swing)")
# 3) nearest unbroken swing used (no jump to a farther one); 1.2 ATR away -> stop 101.3
h = peaks([(15, 102.5), (33, 101.2)], [99.6, 99.7, 99.9, 99.8, 99.95]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is not None and inf["stop_swing"] == 101.2 and not inf["stop_floored"] and abs(st - 101.3) < 1e-9, f"nearest unbroken swing 101.2 -> stop {st:.2f}, not floored")
# 4) no unbroken swing within 48 bars -> rejected
h = [99.0] * 10 + [103.0] + [99.0] * 50 + [99.6, 99.7, 99.9, 99.95, 100.0]; st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is None and "no unbroken" in inf["stop_reject"], f"no real swing in 48h -> rejected ({inf['stop_reject']})")
# 5) 3% cap: nearest swing 103.5 -> stop 103.6 = 3.6% -> rejected
h = peaks([(30, 103.5)], [99.6, 99.7, 99.9, 99.8, 99.95]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is None and "> max 3.0%" in inf["stop_reject"], f"stop 3.6% -> rejected ({inf['stop_reject']})")
h = peaks([(30, 102.85)], [99.6, 99.7, 99.9, 99.8, 99.95]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, A1)
ok(st is not None and inf["stop_pct"] <= 3.0, f"stop 2.95% -> allowed ({st:.2f})")
# 6) long mirror
lows = [101.0] * 40; lows[33] = 98.7; lows += [100.4, 100.3, 100.1, 100.2, 100.05]
st, inf = S.b_structural_stop(mkH(np.array(lows) + 2.0, lows), len(lows) - 1, 1, E, A1)
ok(st is not None and abs(st - 98.6) < 1e-9, f"long mirror: swing low 98.7 -> stop {st:.2f}")
# 7) 0.4% floor when ATR tiny
h = peaks([(30, 100.2)], [99.6, 99.7, 99.9, 99.8, 99.95]); st, inf = S.b_structural_stop(mkH(h), len(h) - 1, -1, E, 0.1)
ok(st is not None and abs(st - 100.4) < 1e-9, f"0.4% floor (ATR 0.1): swing 100.2 -> stop {st:.2f}")
# ---- XPL 2026-10-23 08:05 fixture (real closed Kraken bars) ----
F = json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "xpl_2026-10-23_0805.json")))
ctx = build_ctx("PF_XPLUSD", *(np.array(F[tf], float) for tf in ("1h", "4h", "1d"))); i = len(ctx["H"]["t"]) - 1; a1 = float(ctx["H"]["atr"][i])
for label, e in (("scanner entry 0.07815", F["entry_scanner"]), ("Zone mark 0.07853", F["mark_zone"])):
    st, inf = S.b_structural_stop(ctx["H"], i, -1, e, a1)
    ok(st is not None and st > 0.0797 and inf["stop_swing"] == 0.07975, f"XPL {label}: stop {st:.5f} beyond real swing {inf['stop_swing']} (old rule 0.0787-0.0790 at the live rebound high {inf['rebound_extreme']})")
    cand, ck = S.type_b(ctx, i, -1, e)
    ok(cand is None and ck["4H_struct"] == 1 and ck["4H"] is False, f"XPL {label}: 4H struct +1 against the short -> REJECTED")
# 4H struct: 0 ok, against rejected
F4 = ctx["F"]; j4 = int(ctx["j4"][i]); saved = F4["st"][j4]
for v, exp in ((0, True), (1, False), (-1, True)):
    F4["st"][j4] = v; ck = S.type_b(ctx, i, -1, F["mark_zone"])[1]
    ok(ck["4H"] is (exp and ck["4H_ema"]) and ck["4H_struct"] == v, f"short: 4H struct {v:+d} -> 4H ok {ck['4H']} (EMA {ck['4H_ema']})")
F4["st"][j4] = saved
e20 = float(ctx["H"]["e20"][i])
for k, exp in ((0.08, False), (0.49, False), (0.5, True), (1.0, True), (1.6, False)):
    ok(S.type_b(ctx, i, -1, e20 + k * a1)[1]["rebound_ok"] is exp, f"rebound {k} ATR above 1H EMA20 -> rebound_ok {exp}")
# rejection paths after stop recompute
orig = S.b_structural_stop
S.b_structural_stop = lambda H, i, d, entry, a1, k=None: (None, {"rebound_extreme": 0.0786, "min_stop_dist": 0.5 * a1, "min_stop_atr": 0.5, "stop_reject": "test"})
c, ck = S.type_b(ctx, i, -1, F["mark_zone"]); ok(c is None and ck["stop_ok"] is False, "type_b: no valid stop -> rejected")
S.b_structural_stop = orig
print("ALL TYPE_B_STOP TESTS PASSED")
