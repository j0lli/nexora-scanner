#!/usr/bin/env python3
"""
Nexora Desk scanner v2 - deterministic, NO LLM, Kraken Futures PUBLIC endpoints only (no keys, no orders).
Universe: whitelist.txt list A always, list B only if live spread <= 0.06%. Evaluated on CLOSED 1H/4H/1D bars;
live price = ticker last. Code: features.py (modules + setups), desklib.py (HTTP, EMA, ATR).

SETUPS
 A  trend pullback (BTC/XRP/FIL model): D and 4H EMA trend aligned (close beyond EMA20 & EMA50, EMA20 sloping),
    daily structure not opposite; within the last 12 1H bars price pulled back into the 4H EMA20 zone (+-1 ATR4h)
    or onto a key level (+-0.3 ATR4h); trigger = 1H close reclaiming the 1H EMA20 (wrong side within prior 2 bars),
    candle in trade direction, close beyond the prior 3 bars' high/low; price <= 1.5 ATR4h from 4H EMA20.
    (zone/lookback/trigger chosen in-sample by walk-forward, see backtest_report.md)
 B  momentum: 4H close>EMA20>EMA50 (mirror), 1H close beyond prior 24h high/low on >=1.5x 20-bar volume,
    extension from 4H EMA20 < 2 ATR4h, daily not > 1 daily ATR beyond its EMA20 on the wrong side.
HARD FILTERS (both): no entry after a >2 ATR(1H) expansion candle (trigger or prior bar); session: xStocks only
    Mon-Fri 15:30-22:00 Rome, commodities Mon-Fri 08:00-22:30 Rome; BTC regime: no alt long when BTC 4H is
    bearish (close<EMA20<EMA50) AND alt RS72 vs BTC < 0 (mirror for shorts); stop <= 3%.
STOP: beyond the structural swing (pullback extreme for A, 5-bar 1H swing for B) - 0.1 ATR1h, widened to at least
    max(0.6%, 1 ATR1h). T1 = +0.8R (half, stop to BE), T2 = +2R. fee_R = 0.10% round trip / stop distance.

CONFLUENCE SCORE 0-10 = sum of backtestable modules, then live-only penalties
 1 structure 0-2.5 : 1D swing structure HH/HL (LH/LL short) 0.75, else last 1D break-of-structure in direction 0.4;
                     same for 4H (0.75/0.4); fresh 1H BOS/CHoCH in direction within 6 bars 0.5; D+4H EMA trend 0.5
 2 levels    0-2   : pullback hit 4H EMA20 zone AND a key level 1.5, one of them 1.0; entry within 1 ATR4h of 4H EMA20 +0.5
                     key levels = prior day H/L, prior week H/L, weekly open, 4H & 1D last swing H/L, round numbers
 3 volatility 0-1  : ATR4h percentile (120 bars) in 20-85 +0.5; no 1H bar > 1.5 ATR in last 3 +0.5
 4 volume    0-1.5 : trigger rel. volume >= 1.2x +0.5; quiet pullback (5-bar avg vol < prior 20) +0.5;
                     price on trade side of daily-anchored VWAP +0.5   (24h value area POC/VAL/VAH reported)
 5 regime/RS 0-1.5 : alts: BTC 4H regime aligned 0.75 (neutral 0.35); RS72 vs BTC in direction 0.75.
                     BTC/commodities/xStocks: own D trend 0.75 + 0.4
 6 trade quality 0-1.5 : fee_R <= 0.10 +0.5; stop <= 2 ATR1h +0.5; room to next opposing key level >= 2R +0.5
 LIVE PENALTIES (not backtestable): funding against trade >= 30%/yr annualized -1.0 (>= 15% -0.5) ["crowded"];
    OI down > 5% over ~4h while price moved our way (covering-driven) -0.5; spread > 0.06% -0.5;
    top-of-book depth < 3x our notional -0.5.
 CORRELATION GUARD: hits in the same direction with 72h 1H-return correlation > 0.8 -> only the best counts
    (others flagged "correlated_with", excluded from signals).
SIZING: equity from state/equity.json (live Kraken via equity.py; fallback DESK_EQUITY, then 34.80); risk 2.5% at score < 8, 3% at >= 8; contracts = risk / stop, rounded down
    to 10^-contractValueTradePrecision; below min size -> min size if risk <= $1.20 else not feasible; notional capped
    at 9x equity (EU retail 10% IM).
OUTPUT: scan_latest.json (all hits, modules, one-line reason, market overview); signals.jsonl (score >= 7 AND feasible
    AND not correlation-duplicate; dedupe symbol+dir+setup 3h); state/deriv_history.jsonl (OI/funding history, 3 days).
API: candles refetched only after a new bar of that TF closes; tickers once per run; instruments every 6h.
"""
import json, os, sys, time, math, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from desklib import http_json, fetch_candles, BASE_T, DESK, CACHE, TFSEC
from features import build_ctx, evaluate, session, kind, FEE_RT, MIN_STOP_PCT
import setup_types  # 2026-10-23 setup_type A/B (see setup_types.py, rules_oct16.md)

ROME = ZoneInfo("Europe/Rome")
# 2026-10-22 18:1x HoT: equity comes from desk/state/equity.json (live Kraken, written by equity.py), not a constant.
# Fallback order: equity.json (even if stale, flagged) -> DESK_EQUITY env -> 34.80 (owner-reset base 2026-10-22).
EQUITY_SOURCE, EQUITY_STALE = "fallback", True
try:
    import equity as _eq
    _e = _eq.load(); EQUITY = float(_e["equity_usd"]); EQUITY_SOURCE = "state/equity.json " + _e["ts_rome"]; EQUITY_STALE = bool(_e["stale"])
except Exception as _ex:
    EQUITY = float(os.environ.get("DESK_EQUITY", "34.80")); EQUITY_SOURCE = f"fallback ({type(_ex).__name__})"
# 2026-10-22 18:1x HoT: Friday hints are NOT a live trigger. Shadow-logged only (friday_hints_shadow), unless
# DESK_FRIDAY_LIVE=1 is set explicitly after validation. scan_latest.json["friday_hints"] stays [] while disabled.
FRIDAY_LIVE = os.environ.get("DESK_FRIDAY_LIVE", "0") == "1"
MAX_RISK_MIN, B_SPREAD_MAX, SIG_MIN, DEDUPE_SEC = 1.20, 0.06, 7.0, 3 * 3600
STATE = os.path.join(DESK, "state"); os.makedirs(STATE, exist_ok=True); os.makedirs(CACHE, exist_ok=True)
LOOKBACK = {"1h": 12 * 86400, "4h": 45 * 86400, "1d": 160 * 86400}


def load_whitelist():
    A, B = [], []
    with open(os.path.join(DESK, "whitelist.txt")) as f:
        for line in f:
            if line.startswith("A:"): A = line[2:].split()
            elif line.startswith("B:"): B = line[2:].split()
    return A, B


def candles(sym, tf, now):
    """Refetch only when a new bar of this TF has closed since the cached copy (+45s for publication)."""
    fn = os.path.join(CACHE, f"v2_{sym}_{tf}.json")
    boundary = now // TFSEC[tf] * TFSEC[tf] + 45
    if os.path.exists(fn) and (os.path.getmtime(fn) >= boundary or now < boundary and now - os.path.getmtime(fn) < TFSEC[tf]):
        try:
            return np.array(json.load(open(fn)), dtype=float)
        except Exception:
            pass
    a = fetch_candles(sym, tf, now - LOOKBACK[tf], now, sleep=0.12)
    json.dump(a.tolist(), open(fn + ".tmp", "w")); os.replace(fn + ".tmp", fn)
    return a


def instruments():
    fn = os.path.join(CACHE, "instruments.json")
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600:
        return json.load(open(fn))
    d = http_json(f"{BASE_T}/instruments")
    m = {i["symbol"]: {"prec": i.get("contractValueTradePrecision", 0), "cs": i.get("contractSize", 1)}
         for i in d.get("instruments", []) if i["symbol"].startswith("PF_")}
    json.dump(m, open(fn, "w")); return m


def deriv_history(tick, syms, now):
    fn = os.path.join(STATE, "deriv_history.jsonl")
    rows = []
    if os.path.exists(fn):
        for line in open(fn):
            try:
                r = json.loads(line)
                if now - r["ts"] < 3 * 86400: rows.append(r)
            except Exception: pass
    rec = {"ts": int(now), "d": {s: [tick[s].get("openInterest"), tick[s].get("last"), tick[s].get("relativeFundingRate")]
                                 for s in syms if s in tick}}
    rows.append(rec)
    with open(fn + ".tmp", "w") as f:
        for r in rows: f.write(json.dumps(r) + "\n")
    os.replace(fn + ".tmp", fn)
    return rows


def oi_change(rows, sym, now, secs):
    past = [r for r in rows if now - r["ts"] >= secs * 0.8 and sym in r["d"]]
    if not past or sym not in rows[-1]["d"]: return None, None
    p = min(past, key=lambda r: abs(now - secs - r["ts"]))
    if abs(now - secs - p["ts"]) > secs * 0.5: return None, None
    oi0, px0, _ = p["d"][sym]; oi1, px1, _ = rows[-1]["d"][sym]
    if not oi0 or not px0: return None, None
    return (oi1 / oi0 - 1) * 100, (px1 / px0 - 1) * 100


def sizing(score, entry, stop, inst, risk_pct=None):
    # 2026-10-23 rules: risk_pct from setup_types (A 2.5/3%, B 1.5%); None = legacy 2.5/3% (untyped hits, never alerted)
    risk_budget = EQUITY * ((risk_pct / 100) if risk_pct is not None else (0.03 if score >= 8 else 0.025))
    sd = abs(entry - stop); cs = inst.get("cs", 1) or 1; prec = inst.get("prec", 0); step = 10 ** (-prec)
    qty = round(math.floor(risk_budget / (sd * cs) / step) * step, max(0, prec))
    feasible, note = True, "ok"
    if qty < step:
        rmin = step * sd * cs
        if rmin > MAX_RISK_MIN: feasible, note = False, f"min size {step} -> risk ${rmin:.2f} > ${MAX_RISK_MIN}"
        else: qty, note = step, f"min size used, risk ${rmin:.2f}"
    notional = qty * entry * cs
    if feasible and notional > 9 * EQUITY:
        qty = round(math.floor(9 * EQUITY / (entry * cs) / step) * step, max(0, prec)); notional = qty * entry * cs
        if qty < step: feasible, note = False, "min size exceeds margin"
        else: note = f"size capped by margin (9x equity), risk ${qty*sd*cs:.2f}"
    return {"risk_pct": risk_pct, "risk_budget": round(risk_budget, 2), "contracts": qty, "min_size": step, "risk_usd": round(qty * sd * cs, 2),
            "notional_usd": round(notional, 2), "margin_usd_10pct": round(notional * 0.1, 2), "feasible": feasible, "note": note}


def fmt(x):
    return float(f"{x:.6g}")


def reason(h):
    s = h["symbol"].replace("PF_", "").replace("USD", "")
    if h.get("setup") == "TB":
        c = h["type_checks"]
        return (f"{s} {h['dir']} TYPE-B {h['score']}: 4H trend, D not contrary, 1H rebound {c['rebound_1h_atr']} ATR at "
                f"{c['level']} ({c['level_dist_atr']} ATR); stop {h['stop_pct']}% RR {c['rr']} risk {h['risk_pct']}%")
    m = h["modules"]
    parts = []
    st = h["struct"]; parts.append(f"struct D{st['1D']:+d}/4H{st['4H']:+d}")
    if h["setup"] == "A":
        z = "4H EMA20" if h.get("in_4h_ema_zone") else ""
        lv = "+".join(h.get("at_levels", []))
        parts.append("pullback to " + " & ".join(x for x in (z, lv) if x) + ", 1H reclaim")
    else:
        parts.append(f"24h breakout vol {h.get('vol_ratio')}x" + (f" thru {'+'.join(h['levels_broken'])}" if h.get("levels_broken") else ""))
    if h.get("rs72_vs_btc") is not None: parts.append(f"RS72 {h['rs72_vs_btc']:+.1f}% BTC4H {h['btc_4h_regime']:+d}")
    parts.append(f"RV {h['rel_vol']}x")
    if h["live"]["funding_ann_pct"] is not None: parts.append(f"fund {h['live']['funding_ann_pct']:+.0f}%/y")
    if h["live"]["flags"]: parts.append("FLAGS " + ",".join(h["live"]["flags"]))
    parts.append(f"stop {h['stop_pct']}% fee {h['fee_R']}R room {h['room_R']}R")
    return f"{s} {h['dir']} {h['setup']} {h['score']}: " + "; ".join(parts)


def main():
    t0 = now = time.time()
    A, B = load_whitelist(); errors = []
    tick = {t["symbol"]: t for t in http_json(f"{BASE_T}/tickers").get("tickers", [])}
    try: inst = instruments()
    except Exception as e: inst = {}; errors.append(f"instruments: {e}")
    spread = lambda t: (t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 100 if t.get("bid") and t.get("ask") else 99
    universe = [s for s in A if s in tick]
    skipped = []
    for s in B:
        if s in tick and spread(tick[s]) <= B_SPREAD_MAX: universe.append(s)
        else: skipped.append(f"{s} spread {spread(tick.get(s, {})):.3f}%")
    # 2026-10-22: user wants ALL liquid crypto perps scanned (free, no Grok usage). Any PF_ perp, not suspended,
    # 24h vol >= ALL_VOL_MIN USD and spread <= ALL_SPREAD_MAX %, beyond lists A/B. TIA stays excluded (Kraken rejection).
    ALL_VOL_MIN, ALL_SPREAD_MAX, EXCLUDE = 500000, 0.15, {"PF_TIAUSD"}
    for s, t in tick.items():
        if (s.startswith("PF_") and s not in universe and s not in EXCLUDE and not t.get("suspended")
                and float(t.get("volumeQuote") or 0) >= ALL_VOL_MIN and spread(t) <= ALL_SPREAD_MAX):
            universe.append(s)
    rows = deriv_history(tick, universe, now)
    ctxs = {}
    for s in ["PF_XBTUSD"] + [u for u in universe if u != "PF_XBTUSD"]:
        try:
            ctxs[s] = build_ctx(s, candles(s, "1h", now), candles(s, "4h", now), candles(s, "1d", now))
        except Exception as e:
            errors.append(f"{s}: {type(e).__name__}: {e}")
    btc = ctxs.get("PF_XBTUSD")
    hits, market = [], []
    sess, _ = session(now)
    for s in universe:
        if s not in ctxs: continue
        try:
            ctx, t = ctxs[s], tick[s]; H = ctx["H"]
            i = int(np.searchsorted(H["t"], now - 3600, side="right") - 1)
            last = float(t["last"]); sp = spread(t)
            fr = t.get("relativeFundingRate"); f_ann = fr * 24 * 365 * 100 if fr is not None else None
            oi4, px4 = oi_change(rows, s, now, 4 * 3600); oi1, _ = oi_change(rows, s, now, 3600)
            j4 = int(ctx["j4"][i]); F = ctx["F"]
            market.append({"symbol": s, "last": last, "chg24h_pct": t.get("change24h"), "spread_pct": round(sp, 4),
                           "funding_ann_pct": None if f_ann is None else round(f_ann, 1),
                           "oi_chg_1h_pct": None if oi1 is None else round(oi1, 2), "oi_chg_4h_pct": None if oi4 is None else round(oi4, 2),
                           "struct_D_4H_1H": [int(ctx["D"]["st"][ctx["jd"][i]]), int(F["st"][j4]), int(H["st"][i])],
                           "dist_4h_ema20_atr": round(float((last - F["e20"][j4]) / F["atr"][j4]), 2)})
            for d in (1, -1):
                for setup in ("A", "B"):
                    h = evaluate(ctx, i, d, setup, btc)
                    if not h: continue
                    # live entry: last price unless it ran > 0.3R our way since trigger close -> limit at trigger close
                    trig = h["entry"]; sd0 = abs(trig - h["stop"])
                    if d * (last - h["stop"]) <= 0: continue  # already stopped
                    if d * (last - trig) > 0.3 * sd0: entry, etype = trig, "limit at trigger close (don't chase)"
                    else: entry, etype = last, "market/limit at last"
                    stop = h["stop"]; min_sd = max(MIN_STOP_PCT / 100 * entry, h["atr1h"])
                    if abs(entry - stop) < min_sd: stop = entry - d * min_sd
                    sd = abs(entry - stop)
                    h.update(entry=fmt(entry), entry_type=etype, stop=fmt(stop), stop_pct=round(sd / entry * 100, 3),
                             fee_R=round(FEE_RT * entry / sd, 3), t1=fmt(entry + d * 0.8 * sd), t2=fmt(entry + d * 2 * sd),
                             vwap_day=fmt(h["vwap_day"]))
                    # live-only modules
                    pen, flags = 0.0, []
                    if f_ann is not None:
                        against = f_ann * d  # long pays when funding > 0
                        if against >= 30: pen -= 1.0; flags.append("crowded_funding")
                        elif against >= 15: pen -= 0.5; flags.append("rich_funding")
                    if oi4 is not None and px4 is not None and d * px4 > 0.5 and oi4 < -5:
                        pen -= 0.5; flags.append("OI_falling_with_move")
                    if sp > 0.06: pen -= 0.5; flags.append("wide_spread")
                    base_score = h["score"]
                    sz = sizing(base_score + pen, entry, stop, inst.get(s, {"prec": 0, "cs": 1}))
                    depth = min((t.get("bidSize") or 0) * (t.get("bid") or 0), (t.get("askSize") or 0) * (t.get("ask") or 0))
                    if depth < 3 * sz["notional_usd"]: pen -= 0.5; flags.append("thin_book")
                    h["score_base"] = base_score; h["score"] = round(max(0.0, base_score + pen), 2)
                    a_ok, a_ck = setup_types.type_a(ctx, i, d, entry, stop, h["t2"], h["score"])
                    h["setup_type"] = "A" if a_ok else None; h["type_checks"] = a_ck
                    h["risk_pct"] = setup_types.risk_pct("A", h["score"]) if a_ok else None
                    h["sizing"] = sizing(h["score"], entry, stop, inst.get(s, {"prec": 0, "cs": 1}), h["risk_pct"])
                    h["live"] = {"penalty": pen, "flags": flags, "funding_ann_pct": None if f_ann is None else round(f_ann, 1),
                                 "oi_chg_1h_pct": oi1, "oi_chg_4h_pct": oi4, "spread_pct": round(sp, 4),
                                 "top_book_usd": round(depth), "vol24h_usd": round(t.get("volumeQuote") or 0),
                                 "session": sess, "kind": kind(s)}
                    try:
                        from features import value_area
                        h["value_area_24h"] = {k: fmt(v) for k, v in (value_area(H, i) or {}).items()}
                    except Exception: pass
                    h["list"] = "A" if s in A else "B"; h["last"] = last
                    h["bar_time_rome"] = datetime.fromtimestamp(h["bar_time"], ROME).strftime("%Y-%m-%d %H:%M")
                    hits.append(h)
            # 2026-10-23 TYPE B (XRP/BTC 15/10 pattern): own deterministic detector, risk 1.5%
            bi = int(np.searchsorted(btc["H"]["t"], now - 3600, side="right") - 1) if btc else None
            c24 = lambda X, k: float((X["c"][k] / X["c"][k - 24] - 1) * 100) if k is not None and k >= 24 else None
            for d in (1, -1):
                cand, ck = setup_types.type_b(ctx, i, d, last, c24(btc["H"], bi) if btc else None, c24(H, i), s == "PF_XBTUSD")
                if not cand: continue
                sd = abs(last - cand["stop"])
                hb = {"symbol": s, "setup": "TB", "setup_type": "B", "dir": cand["dir"], "score": cand["score"], "score_base": cand["score"],
                      "entry": fmt(last), "entry_type": "limit at last (rebound)", "stop": fmt(cand["stop"]), "t1": fmt(cand["t1"]),
                      "t2": fmt(cand["t2"]), "stop_pct": round(sd / last * 100, 3), "fee_R": round(FEE_RT * last / sd, 3),
                      "risk_pct": cand["risk_pct"], "type_checks": ck, "bar_time": float(H["t"][i]), "atr1h": float(H["atr"][i]),
                      "struct": {"1D": int(ctx["D"]["st"][ctx["jd"][i]]), "4H": int(F["st"][j4]), "1H": int(H["st"][i])},
                      "live": {"penalty": 0.0, "flags": [], "funding_ann_pct": None if f_ann is None else round(f_ann, 1),
                               "spread_pct": round(sp, 4), "session": sess, "kind": kind(s)},
                      "list": "A" if s in A else "B", "last": last,
                      "bar_time_rome": datetime.fromtimestamp(float(H["t"][i]), ROME).strftime("%Y-%m-%d %H:%M")}
                hb["sizing"] = sizing(hb["score"], last, cand["stop"], inst.get(s, {"prec": 0, "cs": 1}), cand["risk_pct"])
                hits.append(hb)
        except Exception as e:
            errors.append(f"{s}: {type(e).__name__}: {e}")
    hits.sort(key=lambda h: -h["score"])
    # correlation guard
    for a_i, a in enumerate(hits):
        for b in hits[:a_i]:
            if b["dir"] != a["dir"] or b.get("correlated_with"): continue
            Ha, Hb = ctxs[a["symbol"]]["H"], ctxs[b["symbol"]]["H"]
            common = np.intersect1d(Ha["t"][-73:], Hb["t"][-73:])
            if len(common) < 40: continue
            ca = Ha["c"][np.searchsorted(Ha["t"], common)]; cb = Hb["c"][np.searchsorted(Hb["t"], common)]
            rho = float(np.corrcoef(np.diff(np.log(ca)), np.diff(np.log(cb)))[0, 1])
            if rho > 0.8:
                a["correlated_with"] = f"{b['symbol']} (rho {rho:.2f})"; break
    for h in hits:
        h["reason"] = reason(h) + (f"; CORR dup of {h['correlated_with']}" if h.get("correlated_with") else "")
        h["signal_eligible"] = h["score"] >= SIG_MIN and h["sizing"]["feasible"] and not h.get("correlated_with")
    ts = datetime.now(ROME).strftime("%Y-%m-%d %H:%M:%S %Z")
    # 2026-10-22 FRIDAY HINTS (pattern of good trades XRP/BTC short, FIL long): see friday.py. Output only, never orders.
    fh, fh_new = [], 0
    try:
        import importlib, friday; importlib.reload(friday)
        fh = friday.friday_hints(ctxs, tick, now, fmt)
        fh_new = friday.log_new(fh, now, DESK, ts, fn_name=("friday_hints.jsonl" if FRIDAY_LIVE else "friday_hints_shadow.jsonl"))
    except Exception as e:
        errors.append(f"friday_hints: {type(e).__name__}: {e}")
    out = {"friday_hints": (fh if FRIDAY_LIVE else []), "friday_hints_shadow": ([] if FRIDAY_LIVE else fh), "friday_live": FRIDAY_LIVE, "n_friday_hints": (len(fh) if FRIDAY_LIVE else 0), "friday_new": (fh_new if FRIDAY_LIVE else 0), "timestamp_rome": ts, "equity_assumed": EQUITY, "equity_source": EQUITY_SOURCE, "equity_stale": EQUITY_STALE, "session": sess, "universe": len(universe),
           "b_skipped_spread": skipped, "btc_4h_regime": None, "n_hits": len(hits),
           "n_signal_eligible": sum(h["signal_eligible"] for h in hits),
           "top": [h["reason"] for h in hits[:5]], "hits": hits, "market": market, "errors": errors,
           "runtime_s": round(time.time() - t0, 1)}
    if btc:
        from features import btc_state
        out["btc_4h_regime"], out["btc_1d_trend"] = btc_state(btc, now)
    json.dump(out, open(os.path.join(DESK, "scan_latest.json.tmp"), "w"), indent=1, default=float)
    os.replace(os.path.join(DESK, "scan_latest.json.tmp"), os.path.join(DESK, "scan_latest.json"))
    sigf = os.path.join(DESK, "signals.jsonl"); recent = {}
    if os.path.exists(sigf):
        for line in open(sigf).readlines()[-500:]:
            try: r = json.loads(line); recent[(r["symbol"], r["dir"], r["setup"])] = r["ts_unix"]
            except Exception: pass
    new = 0
    with open(sigf, "a") as f:
        for h in hits:
            k = (h["symbol"], h["dir"], h["setup"])
            if not h["signal_eligible"] or now - recent.get(k, 0) < DEDUPE_SEC: continue
            f.write(json.dumps({"ts_unix": int(now), "ts_rome": ts, "v": 2, **h}, default=float) + "\n"); new += 1; recent[k] = now
    # 2026-10-22 18:4x HoT: deterministic Head wake-up webhook (scanner hits only, never Friday hints). Never blocks.
    try:
        import head_alert; ha = head_alert.process(hits, now)
    except Exception as e:
        ha = f"head_alert=error({type(e).__name__})"
    print(f"{ts} {ha} universe={len(universe)} hits={len(hits)} eligible={out['n_signal_eligible']} new_signals={new} "
          f"top={[h['reason'][:60] for h in hits[:3]]} friday{"" if FRIDAY_LIVE else "_shadow"}={[(h['symbol'].replace('PF_',''), h['dir'][0]) for h in fh]} friday_new={fh_new} equity={EQUITY:.2f}{"(STALE)" if EQUITY_STALE else ""} errors={len(errors)} {out['runtime_s']}s", flush=True)


if __name__ == "__main__":
    import fcntl
    _lk = open(os.path.join(DESK, ".scanner_run.lock"), "w")
    try: fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print("another scanner run in progress, skip"); sys.exit(0)
    try: main()
    except Exception:
        traceback.print_exc(); sys.exit(1)
