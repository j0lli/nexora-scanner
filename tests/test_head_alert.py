import os as _os, sys as _sys; DESK_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "desk")  # cloud bundle path
"""Local test of head_alert filters + dedup. NO network: sender is a fake; state files in a temp dir."""
import sys, os, json, tempfile, numpy as np
sys.dont_write_bytecode = True; sys.path.insert(0, DESK_DIR)
import head_alert as H
tmp = tempfile.mkdtemp(); H.ALERTS = os.path.join(tmp, "a.jsonl"); H.AUTHF = os.path.join(tmp, "auth.json")
TRD = os.path.join(tmp, "trades"); os.makedirs(TRD); H.TRADES = TRD
eq = os.path.join(tmp, "eq.json"); sus = os.path.join(tmp, "SUS")
NOW = 1792688400  # 2026-10-22 18:20 Rome (Thu)
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or sys.exit(1)
def mk(d, n=300, tf=3600, slope=0.001, last_pull=0.0, wave=None):
    t = np.arange(NOW - n * tf - tf, NOW - tf, tf)[:n]; c = 100 * (1 + d * slope) ** np.arange(n); c[-1] *= (1 + last_pull)
    # 2026-10-23: 4H gets a small zigzag (period 8, ends on a trend-side bar) so swing structure is HH/HL or LH/LL, not 0
    if (wave if wave is not None else tf == 14400): c = c * (1 + d * 0.01 * np.abs(((np.arange(n) - n + 1) % 8) - 4) / 4)
    return np.c_[t, c, c * 1.01, c * 0.99, c, np.ones(n)]
def loader_for(d, flat_tf=None):
    def L(sym, tf):
        sec = H.TFSEC[tf]; C = mk(d, tf=sec)
        if tf == flat_tf: C[:, 1:5] = 100.0
        return C
    return L
def put_eq(p, age=0): json.dump({"ts_unix": NOW - age, "daily_pnl_pct": p}, open(eq, "w"))
H.EQ_STALE = 10**9  # freshness tested separately below
put_eq(0.0)
H_gates = H.gates; H.gates = lambda now: H_gates(now, eq, sus)
C1h = mk(1); e20 = H._ema(C1h[:, 4], 20)[-1]; a = H._atr(C1h)[-1]; last = C1h[-1, 4]
good = {"symbol": "PF_TEST", "setup_type": "A", "dir": "long", "score": 7.5, "entry": last, "stop": last - 1.0, "t1": last + 0.8, "t2": last + 2.0, "sizing": {"feasible": True}}
calls = []
def fake(p): calls.append(p); return 200, "header=Authorization"
r = H.process([good], NOW, loader_for(1), fake)
ok(len(calls) == 1 and set(calls[0]) == {"setup_type","risk_pct","symbol","dir","score","entry","stop","t1","t2","rr","time_rome"} and calls[0]["setup_type"] == "A" and calls[0]["risk_pct"] == 2.5, f"good aligned long -> 1 send, short payload ({r}); dist_1h={(last-e20)/a:.2f} ATR")
r = H.process([good], NOW + 3600, loader_for(1), fake); ok(len(calls) == 1, "same symbol/dir within 2h -> dedup, no send")
r = H.process([good], NOW + 2 * 3600 + 60, loader_for(1), fake); ok(len(calls) == 2, "after 2h -> sent again")
short = dict(good, dir="short", entry=mk(-1)[-1, 4]); short.update(stop=short["entry"] + 1, t1=short["entry"] - 0.8, t2=short["entry"] - 2)
H.process([short], NOW + 2 * 3600 + 60, loader_for(-1), fake); ok(len(calls) == 3, "short on same symbol = different key -> sent")
n = len(calls)
for name, hit, L in [("score 6.9", dict(good, symbol="S1", score=6.9), loader_for(1)),
                     ("RR 1.4", dict(good, symbol="S2", t2=good["entry"] + 1.4), loader_for(1)),
                     ("infeasible", dict(good, symbol="S3", sizing={"feasible": False}), loader_for(1)),
                     ("friday hint", dict(good, symbol="S4", source="friday"), loader_for(1)),
                     ("4H not aligned", dict(good, symbol="S5"), loader_for(1, "4h")),
                     ("1D not aligned", dict(good, symbol="S6"), loader_for(1, "1d")),
                     ("extended >1 ATR", dict(good, symbol="S7", entry=good["entry"] + 5 * a, stop=good["entry"] + 5 * a - 1, t2=good["entry"] + 5 * a + 2), loader_for(1)),
                     ("wrong side of EMA20", dict(good, symbol="S8", entry=e20 - 0.5 * a, stop=e20 - 0.5 * a - 1, t2=e20 - 0.5 * a + 2), loader_for(1))]:
    H.process([hit], NOW + 5 * 3600, L, fake); ok(len(calls) == n, f"filtered: {name}")
g = dict(good, symbol="G")
for name, setup in [("ENTRIES_SUSPENDED", lambda: open(sus, "w").write("x")), ("daily -4%", lambda: (os.remove(sus), put_eq(-4.0))),
                    ("daily +1.6%", lambda: put_eq(1.6)), ("equity stale", lambda: (setattr(H, "EQ_STALE", 900), put_eq(0, 3600)))]:
    setup(); r = H.process([g], NOW + 1800, loader_for(1), fake)
    key = {"ENTRIES_SUSPENDED": "ENTRIES_SUSPENDED", "daily -4%": "daily stop", "daily +1.6%": "daily target", "equity stale": "stale"}[name]
    ok(len(calls) == n and key in r, f"gate {name}: {r}")
H.EQ_STALE = 10**9; put_eq(0.0); sat = 1792911600  # Sat 2026-10-24 ~10:00 Rome... recompute equity ts
json.dump({"ts_unix": sat, "daily_pnl_pct": 0}, open(eq, "w")); r = H.process([g], sat, loader_for(1), fake); ok("outside desk hours" in r, f"weekend gate: {r}")
late = NOW + 5 * 3600 - 20 * 60  # 23:00 Rome
json.dump({"ts_unix": late, "daily_pnl_pct": 0}, open(eq, "w")); r = H.process([g], late, loader_for(1), fake); ok("outside" in r, f"23:00 gate: {r}")
def boom(p): return None, "transport TimeoutError"
put_eq(0.0); r = H.process([dict(good, symbol="F")], NOW, loader_for(1), boom, log=lambda m: None)
ok("F:l:None" in r, f"failed send -> logged, loop not blocked ({r})")
rec = [json.loads(l) for l in open(H.ALERTS)]; ok(rec[-1]["delivered"] is False, "failed send recorded delivered=false (retried next scan)")
def crash(p): raise RuntimeError("x")
ok("error" in H.process([dict(good, symbol="Z")], NOW, loader_for(1), crash), "exception inside sender -> process() still returns, never raises")
# auth fallback with fake opener
import urllib.error, io
os.environ["DESK_HEAD_WEBHOOK_URL"] = "https://example.invalid/x"; os.environ["DESK_HEAD_WEBHOOK_KEY"] = "k"
seen = []
class R:
    status = 200
    def __enter__(s): return s
    def __exit__(s, *a): pass
def opener(req, timeout):
    hk = [h for h in req.headers if h.lower() in ("authorization", "x-webhook-key")][0]; seen.append(hk)
    if hk.lower() == "authorization": raise urllib.error.HTTPError(req.full_url, 401, "u", {}, io.BytesIO())
    return R()
code, note = H.post({"t": 1}, opener); ok(code == 200 and len(seen) == 2 and "X-Webhook-Key" in note, f"401 on Bearer -> retried X-Webhook-Key -> {code} {note}")
# ---------------- TYPE B (short rebound, XRP/BTC 15/10 pattern) ----------------
for k in ("DESK_HEAD_WEBHOOK_URL", "DESK_HEAD_WEBHOOK_KEY"): os.environ.pop(k, None)
put_eq(0.0); TB = NOW + 9 * 3600 + 1800   # Fri 03:50? -> use a desk-hours time instead
TB = NOW + 1200                            # 18:40 Rome Thu
json.dump({"ts_unix": TB, "daily_pnl_pct": 0}, open(eq, "w"))
Cd = mk(-1); e20d = H._ema(Cd[:, 4], 20)[-1]; ad = H._atr(Cd)[-1]
def bhit(sym, k_atr=1.0, score=8.0, rr=2.0, stop_atr=1.2):   # 2026-10-23: B stops must be >= 1.0 ATR1h (was 0.6 here)
    e = e20d + k_atr * ad; R = stop_atr * ad
    return {"symbol": sym, "setup_type": "B", "dir": "short", "score": score, "entry": e, "stop": e + R, "t1": e - 0.8 * R, "t2": e - rr * R, "sizing": {"feasible": True}}
n = len(calls); r = H.process([bhit("B1")], TB, loader_for(-1), fake)
ok(len(calls) == n + 1 and calls[-1]["setup_type"] == "B" and calls[-1]["risk_pct"] == 1.5 and calls[-1]["dir"] == "short", f"B good short rebound +1.0 ATR above 1H EMA20 -> sent, risk 1.5% ({r})")
r = H.process([bhit("B1")], TB + 600, loader_for(-1), fake); ok(len(calls) == n + 1, "B dedup 2h")
n = len(calls)
for name, hit, L in [("B score 7.4 (<7.5)", bhit("B2", score=7.4), loader_for(-1)),
                     ("B rebound 1.6 ATR (>1.5)", bhit("B3", k_atr=1.6), loader_for(-1)),
                     ("B entry on trend side of EMA20 (not a rebound)", bhit("B4", k_atr=-0.3), loader_for(-1)),
                     ("B R:R 1.4", bhit("B5", rr=1.4), loader_for(-1)),
                     ("B 4H not aligned", bhit("B6"), loader_for(-1, "4h")),
                     ("B daily contrary (daily uptrend vs short)", bhit("B7"), lambda sym, tf: (mk(1, tf=86400) if tf == "1d" else loader_for(-1)(sym, tf))),
                     ("untyped hit", dict(bhit("B8"), setup_type=None), loader_for(-1)),
                     ("A rules applied to a B-shaped hit", dict(bhit("B9"), setup_type="A"), loader_for(-1))]:
    H.process([hit], TB, L, fake); ok(len(calls) == n, f"filtered: {name}")
H.process([dict(good, symbol="A8", score=8.2)], TB, loader_for(1), fake); ok(calls[-1]["risk_pct"] == 3.0, "A score 8.2 -> risk 3%")
import setup_types as S
ok(S.risk_pct("A", 7.9) == 2.5 and S.risk_pct("A", 8.0) == 3.0 and S.risk_pct("B", 9.5) == 1.5 and S.risk_pct(None, 9) is None, "risk table A 2.5/3, B 1.5 max, untyped None")
import scanner
z = scanner.sizing(8.0, 100.0, 99.0, {"prec": 1, "cs": 1}, 1.5); ok(abs(z["risk_budget"] - round(scanner.EQUITY * 0.015, 2)) < 1e-9 and z["risk_pct"] == 1.5, f"scanner.sizing B: budget {z['risk_budget']} = 1.5% of {scanner.EQUITY}")
z = scanner.sizing(8.0, 100.0, 99.0, {"prec": 1, "cs": 1}, 3.0); ok(abs(z["risk_budget"] - round(scanner.EQUITY * 0.03, 2)) < 1e-9, "scanner.sizing A score 8: 3%")
# ---------------- TYPE B limits (2026-10-22 18:5x) ----------------
H.B_MAX_ALERTS_DAY = 2                                     # cap logic tested at 2 (live: no cap, 10**6, since 2026-10-26)
H.ALERTS = os.path.join(tmp, "b.jsonl")                      # fresh ledger for cap tests
def bl(sym, d="short", **kw):
    h = bhit(sym, **kw)
    if d == "long":
        Cu = mk(1); e20u = H._ema(Cu[:, 4], 20)[-1]; au = H._atr(Cu)[-1]; e = e20u - 1.0 * au; R = 1.2 * au
        h.update(dir="long", entry=e, stop=e - R, t1=e + 0.8 * R, t2=e + 2 * R)
    return h
up_alt_btc_down = lambda sym, tf: mk(-1, tf=H.TFSEC[tf]) if sym == "PF_XBTUSD" else mk(1, tf=H.TFSEC[tf])
down_alt_btc_up = lambda sym, tf: mk(1, tf=H.TFSEC[tf]) if sym == "PF_XBTUSD" else mk(-1, tf=H.TFSEC[tf])
n = len(calls); r = H.process([bl("PF_AAAUSD", "long")], TB, up_alt_btc_down, fake)
ok(len(calls) == n and "BTC 4H bearish" in r, f"B long crypto vs BTC 4H bearish -> blocked ({r})")
r = H.process([bl("PF_BBBUSD", "short")], TB, down_alt_btc_up, fake)
ok(len(calls) == n and "BTC 4H bullish" in r, f"B short crypto vs BTC 4H bullish -> blocked ({r})")
r = H.process([bl("PF_MSTRXUSD", "short")], TB, down_alt_btc_up, fake)
ok(len(calls) == n + 1 and calls[-1]["symbol"] == "PF_MSTRXUSD", f"B short xStock exempt from BTC regime -> sent ({r})")
r = H.process([bl("PF_WTIOILUSD", "short")], TB, down_alt_btc_up, fake)
ok(len(calls) == n + 2, f"B short commodity exempt -> sent; now 2 B alerts today ({r})")
r = H.process([bl("PF_CCCUSD", "short")], TB, loader_for(-1), fake)
ok(len(calls) == n + 2 and "daily cap 2/2" in r, f"3rd B alert same day (other symbol) -> blocked by daily cap ({r})")
r = H.process([dict(good, symbol="PF_A2USD")], TB, loader_for(1), fake)
ok(len(calls) == n + 3 and calls[-1]["setup_type"] == "A", "daily B cap does not block Type A")
H.ALERTS = os.path.join(tmp, "c.jsonl")                      # new day-ledger: cap free again
r = H.process([bl("PF_X1USD"), bl("PF_X2USD"), bl("PF_X3USD")], TB, loader_for(-1), fake)
ok(r.count(":200") == 2 and "daily cap" in r, f"3 B candidates in one scan -> only 2 sent ({r})")
H.ALERTS = os.path.join(tmp, "d.jsonl")
json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "OPEN"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
n = len(calls); r = H.process([bl("PF_NEWUSD")], TB, loader_for(-1), fake)
ok(len(calls) == n and "already active" in r, f"1 Type B trade open -> new B alert blocked ({r})")
json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "CLOSED"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
r = H.process([bl("PF_NEWUSD")], TB, loader_for(-1), fake); ok(len(calls) == n + 1, "B trade closed -> B alert allowed again")
n = len(calls); H.process([dict(bl("PF_CORUSD"), correlated_with="PF_NEWUSD (rho 0.9)")], TB, loader_for(-1), fake)
ok(len(calls) == n, "correlated Type B hit -> filtered (correlation dedup kept)")
_os.environ.get("DESK_TRADES_CODE") and sys.path.insert(0, _os.environ["DESK_TRADES_CODE"])  # cloud bundle: kf.py is box-only
try:
    import kf
except ImportError:
    kf = None; print("SKIP 3 kf guard checks (trades/kf.py is box-only, not in the public bundle)")
if kf is not None:
    json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "OPEN"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
    json.dump({"symbol": "PF_NEWUSD", "setup_type": "B", "status": "PLACED"}, open(os.path.join(TRD, "live-pf-newusd-b.json"), "w"))
    ok("another Type B" in (kf._type_b_conflict("PF_NEWUSD", TRD) or ""), "kf guard: 2nd Type B entry blocked while one is open")
    json.dump({"symbol": "PF_ANEWUSD", "setup_type": "A", "status": "PLACED"}, open(os.path.join(TRD, "live-pf-anewusd.json"), "w"))
    ok(kf._type_b_conflict("PF_ANEWUSD", TRD) is None, "kf guard: Type A entry not affected by the B limit")
    json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "CLOSED"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
    ok(kf._type_b_conflict("PF_NEWUSD", TRD) is None, "kf guard: B entry allowed when no other B is active")
else:
    json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "OPEN"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
    json.dump({"symbol": "PF_NEWUSD", "setup_type": "B", "status": "PLACED"}, open(os.path.join(TRD, "live-pf-newusd-b.json"), "w"))
    json.dump({"symbol": "PF_ANEWUSD", "setup_type": "A", "status": "PLACED"}, open(os.path.join(TRD, "live-pf-anewusd.json"), "w"))
    json.dump({"symbol": "PF_OLDUSD", "setup_type": "B", "status": "CLOSED"}, open(os.path.join(TRD, "live-pf-oldusd-b.json"), "w"))
# ---------------- TYPE B stop limits v3: min max(0.5 ATR1h, 0.4%), max 3% ----------------
H.ALERTS = os.path.join(tmp, "e.jsonl")
json.dump({"symbol": "PF_NEWUSD", "setup_type": "B", "status": "CLOSED"}, open(os.path.join(TRD, "live-pf-newusd-b.json"), "w"))
n = len(calls); r = H.process([bhit("PF_B045USD", stop_atr=0.45)], TB, loader_for(-1), fake)
p_, why = H.qualify(bhit("PF_B045USD", stop_atr=0.45), TB, loader_for(-1))
ok(len(calls) == n and p_ is None and "< min 1.0 ATR" in why, f"B stop 0.45 ATR1h -> filtered ({why})")
pct = lambda k: k * ad / (e20d + ad) * 100
ok(pct(1.2) < 3.0, f"fixture: 1.2 ATR stop = {pct(1.2):.2f}% (< 3%)")
r = H.process([bhit("PF_B100USD", stop_atr=1.0)], TB, loader_for(-1), fake)
ok(len(calls) == n + 1 and calls[-1]["symbol"] == "PF_B100USD", f"B stop 1.0 ATR1h (v4 floor; scanner floors XRP/BTC-like stops here) -> sent ({r})")
kmax = 3.05 / 100 * (e20d + ad) / ad; p_, why = H.qualify(bhit("PF_B305USD", stop_atr=kmax), TB, loader_for(-1))
ok(p_ is None and "> max 3.0%" in why, f"B stop 3.05% -> filtered ({why})")
k29 = 2.9 / 100 * (e20d + ad) / ad; p_, why = H.qualify(bhit("PF_B290USD", stop_atr=k29), TB, loader_for(-1)); ok(p_ is not None, f"B stop 2.9% -> qualifies ({why})")
H.ALERTS = os.path.join(tmp, "f.jsonl")
r = H.process([dict(good, symbol="PF_ASMALL", stop=good["entry"] - 0.3 * a, t1=good["entry"] + 0.24 * a, t2=good["entry"] + 0.6 * a)], TB, loader_for(1), fake)
ok(calls[-1]["symbol"] == "PF_ASMALL", "Type A not affected by the B stop minimum")
H.XSTOCKS_FILE = os.path.join(tmp, "xstocks.json"); json.dump(["PF_NVDAXUSD", "PF_AAPLXUSD", "PF_QQQXUSD"], open(H.XSTOCKS_FILE, "w"))
ok({"PF_QQQXUSD", "PF_AAPLXUSD", "PF_MSTRXUSD"} <= H.xstock_set() and "PF_XRPUSD" not in H.xstock_set(), "xStock set = Kraken category xStocks cache + features.XSTOCKS (QQQX/AAPLX included, crypto not)")
# ---------------- 23/10 NVDAX fixes: 4H struct, rebound >= 0.5 ATR, xStock session, cap not consumed by rejects ----------------
H.ALERTS = os.path.join(tmp, "g.jsonl")
from features import structure
C4 = loader_for(-1)("X", "4h"); C4 = C4[C4[:, 0] + 14400 <= TB]
ok(structure(C4[:, 2], C4[:, 3], C4[:, 4])["st"][-1] == -1, "fixture: synthetic 4H downtrend has struct -1 (LH/LL)")
flat4 = lambda sym, tf: mk(-1, tf=H.TFSEC[tf], wave=False)          # monotonic -> no swings -> struct 0 (neutral), EMAs still aligned
n = len(calls); p_, why = H.qualify(bhit("PF_NV1USD"), TB, flat4)
ok(p_ is not None, f"v3: B with 4H struct 0 (neutral, like XRP 15/10) and EMAs aligned -> qualifies ({why})")
p_, why = H.qualify(bhit("PF_NV1USD", k_atr=0.08), TB, flat4); ok(p_ is None and "rebound" in why, f"NVDAX case (struct 0, rebound 0.08 ATR) -> still rejected by rebound: {why}")
_tc = H.trend_check
H.trend_check = lambda *a_, **k_: dict(_tc(*a_, **k_), struct_4h=1)      # EMAs bearish-aligned, swing structure +1 (XPL case)
p_, why = H.qualify(bhit("PF_NV9USD"), TB, loader_for(-1)); H.trend_check = _tc
ok(p_ is None and "4H struct 1 against" in why, f"B short with 4H struct +1 (against, XPL case) -> rejected: {why}")
up4 = lambda sym, tf: (mk(1, tf=14400) if tf == "4h" else loader_for(-1)(sym, tf))
p_, why = H.qualify(bhit("PF_NV2USD"), TB, up4); ok(p_ is None, f"B short with 4H uptrend -> rejected: {why}")
p_, why = H.qualify(bhit("PF_NV3USD", k_atr=0.08), TB, loader_for(-1)); ok(p_ is None and "rebound" in why, f"B rebound 0.08 ATR (NVDAX) -> rejected: {why}")
p_, why = H.qualify(bhit("PF_NV4USD", k_atr=0.45), TB, loader_for(-1)); ok(p_ is None and "rebound" in why, "B rebound 0.45 ATR -> rejected")
p_, why = H.qualify(bhit("PF_NV5USD", k_atr=0.6), TB, loader_for(-1)); ok(p_ is not None, f"B rebound 0.6 ATR -> qualifies ({why})")
AM = NOW - 10 * 3600 + 5 * 60    # Thu 09:05 Rome (NOW = Thu 19:00 Rome)
json.dump({"ts_unix": AM, "daily_pnl_pct": 0}, open(eq, "w"))
r = H.process([bl("PF_NVDAXUSD")], AM, loader_for(-1), fake)
ok(len(calls) == n and "skip[xstock off-session]" in r and not os.path.exists(H.ALERTS), f"xStock B at 09:05 Rome -> log only, not sent, not in ledger ({r})")
json.dump({"ts_unix": NOW - 2 * 3600 - 300, "daily_pnl_pct": 0}, open(eq, "w"))
r = H.process([bl("PF_NVDAXUSD")], NOW - 2 * 3600 - 300, loader_for(-1), fake)   # Thu 16:55 Rome
ok(len(calls) == n + 1 and calls[-1]["symbol"] == "PF_NVDAXUSD", f"xStock B at 16:55 Rome (US cash) -> sent ({r})")
json.dump({"ts_unix": TB, "daily_pnl_pct": 0}, open(eq, "w"))
H.ALERTS = os.path.join(tmp, "h.jsonl"); n = len(calls)
batch = [bhit("PF_R1USD", stop_atr=0.4), bhit("PF_R2USD", k_atr=0.08), dict(bhit("PF_R3USD"), score=7.0), bl("PF_QQQXUSD"),
         bhit("PF_G1USD"), bhit("PF_G2USD"), bhit("PF_G3USD")]
r = H.process(batch, AM + 3600, loader_for(-1), fake)   # 10:05 Rome: QQQX off-session
sentB = [c["symbol"] for c in calls[n:]]
ok(sentB == ["PF_G1USD", "PF_G2USD"] and "G3USD:s:skip[B: daily cap 2/2" in r, f"4 rejected B (stop 0.4 ATR, rebound 0.08, score 7.0, QQQX off-session) don't consume cap -> G1,G2 sent, G3 capped ({r})")
rows = [json.loads(l) for l in open(H.ALERTS)]; ok(len(rows) == 2 and all(x["filter_ver"] == H.FILTER_VER for x in rows), "ledger: only the 2 delivered rows, tagged filter_ver")
H.ALERTS = os.path.join(tmp, "i.jsonl")
for sym in ("PF_XPLUSD", "PF_NVDAXUSD"):    # delivered 08:25 under the old filter (no filter_ver) -> don't count toward today's cap
    open(H.ALERTS, "a").write(json.dumps({"ts_unix": int(TB) - 600, "setup_type": "B", "symbol": sym, "dir": "short", "delivered": True}) + "\n")
ok(H.b_alerts_today(TB) == 0, "old-filter B alerts (no filter_ver) do not consume the daily cap")
n = len(calls); r = H.process([bhit("PF_K1USD"), bhit("PF_K2USD")], TB, loader_for(-1), fake); ok(len(calls) == n + 2, f"-> 2 new B alerts still allowed today ({r})")
ok(H.b_alerts_today(TB) == 2, "current-filter deliveries counted (2/2)")
print("ALL HEAD_ALERT TESTS PASSED")
