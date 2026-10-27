"""Desk setup types from 2026-10-23 (Head of Trading decision 2026-10-22 18:43 Rome). Deterministic, no Grok, no orders.
All checks use CLOSED bars of a features.build_ctx context: i = last closed 1H bar, j4/jd = matching closed 4H / 1D bars.
"Aligned" on a timeframe = EMA20 vs EMA50 in trade direction AND close on the trade side of EMA20.

Type A (FIL 16/10, unchanged): D + 4H + 1H aligned; entry on trade side of 1H EMA20 within 1.0 ATR(1H); score >= 7;
        R:R at T2 >= 1.5; risk 2.5% (score 7-7.9) / 3% (score >= 8).
Type B (XRP/BTC shorts 15/10): 4H aligned; Daily not contrary (daily close not beyond daily EMA20 on the opposite side);
        [2026-10-23 v3: 4H aligned also needs struct 4H (scanner field) not against the trade direction; 0 allowed]
        entry on a 1H rebound AGAINST the trade, on the opposite side of the 1H EMA20, 0.5-1.5 ATR(1H) away [min 0.5 from 23/10], at a clean
        level (1H EMA20/EMA50, 4H EMA20/EMA50, last 1H/4H swing, prior-day high/low) within 0.5 ATR(1H);
        structural stop (2026-10-23 v3) = beyond the nearest unbroken confirmed 1H swing high (short) / low (long) of the
        rebound + 0.1 ATR, floored at max(0.5 ATR1h, 0.4%) (pushed outward only), capped at 3% (features.MAX_STOP_PCT);
        no real swing -> REJECTED (never a synthetic stop at the unconfirmed rebound extreme);
        T1 = 0.8R; T2 = nearest 4H/1D swing in [1.5R, 3R] else 2R; score_B >= 7.5; R:R at T2 >= 1.5; risk 1.5%.
score_B (0-10, v1, built 2026-10-22 IN-SAMPLE from 2 trades -> NOT statistically validated):
  4H trend 2.0 (+1.0 if |EMA20-EMA50| >= 0.5 ATR4h) | Daily: +1.0 close on trade side of EMA20, +0.5 EMA20 vs EMA50 in
  direction | level within 0.25 ATR1h 2.0, within 0.5 ATR1h 1.5 | rebound 0.5-1.5 ATR 1.0, else 0.5 | stop 0.4-1.2 ATR1h
  1.0, <= 2 ATR 0.5 | R:R >= 2 1.0, >= 1.5 0.5 | 24h move vs BTC in trade direction 0.5 (BTC itself 0.25)."""
import numpy as np
A_MIN_SCORE, A_MAX_DIST, B_MIN_SCORE, B_MAX_DIST, B_LEVEL_ATR, MIN_RR = 7.0, 1.0, 7.5, 1.5, 0.5, 1.5
B_RISK_PCT = 1.5
B_MIN_REBOUND_ATR = 0.5   # 2026-10-23 (NVDAX 0.08 ATR veto): real 1H counter-move >= 0.5 ATR beyond the 1H EMA20 (Oct 15: XRP 1.33, BTC 0.98)
B_MIN_STOP_ATR, B_MIN_STOP_PCT = 1.0, 0.4   # v4 23/10 12:30: floor raised to 1 ATR1h (Zone/Quant vetoed XPL, FARTCOIN, DOGE at 0.67-0.82 ATR); replay Oct 15 XRP+BTC still Type B
B_SWING_K, B_SWING_LOOKBACK, B_STOP_BUF_ATR = 1, 48, 0.1   # pivot confirmed by >=1 closed bar each side (k=2 missed the XRP 15/10
#   rebound high 1.346: only 1 bar after it at entry), 48h lookback, 0.1 ATR buffer. XPL 23/10 still caught: its high was the live bar.
import os as _os, sys as _sys; _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from features import MAX_STOP_PCT as B_MAX_STOP_PCT     # 3.0%: same hard cap as the scanner's A/B setups (features.evaluate)

def b_structural_stop(H, i, d, entry, a1, k=None):
    """Type B stop beyond the nearest REAL, unbroken 1H swing of the rebound. d=-1 short (swing highs), +1 long (swing lows).
    Swing = fractal pivot confirmed by k closed bars on each side (p <= i-k), not exceeded by any later closed bar up to i.
    stop = swing +/- 0.1 ATR, pushed further out (never inward) to the floor max(0.5 ATR1h, 0.4%) if closer; > 3% -> reject.
    Returns (stop or None, info)."""
    K = B_SWING_K if k is None else k; X = H["h"] if d < 0 else H["l"]; sgn = -d
    lo = max(0, i - 5); ext = float(X[lo:i + 1].max() if d < 0 else X[lo:i + 1].min())
    min_sd = max(B_MIN_STOP_ATR * a1, B_MIN_STOP_PCT / 100 * entry)
    info = {"rebound_extreme": ext, "min_stop_dist": float(min_sd), "min_stop_atr": B_MIN_STOP_ATR, "swing_k": K}
    sw = None
    for p in range(i - K, max(K, i - B_SWING_LOOKBACK) - 1, -1):          # newest first, closed + confirmed only
        w = X[p - K:p + K + 1]; v = float(X[p])
        if v != (w.max() if d < 0 else w.min()): continue
        later = X[p + 1:i + 1]
        if len(later) and sgn * (v - (later.max() if d < 0 else later.min())) < 0: continue   # broken -> not real any more
        sw = (i - p, v); break                                   # most recent unbroken pivot = nearest one beyond the rebound
    if sw is None:
        info["stop_reject"] = f"no unbroken 1H swing (k={K}) beyond the rebound within {B_SWING_LOOKBACK}h"; return None, info
    stop = sw[1] + sgn * B_STOP_BUF_ATR * a1
    floored = d * (entry - stop) < min_sd
    if floored: stop = entry - d * min_sd
    pct = d * (entry - stop) / entry * 100
    info.update(stop_swing=sw[1], stop_swing_age_bars=sw[0], stop_floored=bool(floored), stop_pct=round(pct, 3))
    if pct > B_MAX_STOP_PCT:
        info["stop_reject"] = f"stop {pct:.2f}% > max {B_MAX_STOP_PCT}%"; return None, info
    return float(stop), info

def risk_pct(setup_type, score):
    if setup_type == "A": return 3.0 if score >= 8 else 2.5
    if setup_type == "B": return B_RISK_PCT
    return None

def aligned(X, j, d):
    if j < 55 or X["e50"][j] != X["e50"][j]: return False
    return bool(d * (X["e20"][j] - X["e50"][j]) > 0 and d * (X["c"][j] - X["e20"][j]) > 0)

def _idx(ctx, i): return int(ctx["j4"][i]), int(ctx["jd"][i])

def type_a(ctx, i, d, entry, stop, t2, score):
    H, F, D = ctx["H"], ctx["F"], ctx["D"]; j4, jd = _idx(ctx, i)
    R = d * (entry - stop); rr = d * (t2 - entry) / R if R > 0 else 0
    dist = d * (entry - H["e20"][i]) / H["atr"][i]
    ck = {"D": aligned(D, jd, d), "4H": aligned(F, j4, d), "1H": aligned(H, i, d), "dist_1h_atr": round(float(dist), 2),
          "dist_ok": bool(0 <= dist <= A_MAX_DIST), "score_ok": score >= A_MIN_SCORE, "rr": round(float(rr), 2), "rr_ok": rr >= MIN_RR}
    ok = ck["D"] and ck["4H"] and ck["1H"] and ck["dist_ok"] and ck["score_ok"] and ck["rr_ok"]
    return ok, ck

def _levels(ctx, i, d):
    H, F, D = ctx["H"], ctx["F"], ctx["D"]; j4, jd = _idx(ctx, i)
    L = {"1H_EMA20": H["e20"][i], "1H_EMA50": H["e50"][i], "4H_EMA20": F["e20"][j4], "4H_EMA50": F["e50"][j4]}
    # rebound goes AGAINST the trade: a short rebounds up into resistance (swing highs), a long dips into support
    if d < 0: L.update({"1H_swing_high": H["sh"][i], "4H_swing_high": F["sh"][j4], "PDH": D["h"][jd]})
    else: L.update({"1H_swing_low": H["sl"][i], "4H_swing_low": F["sl"][j4], "PDL": D["l"][jd]})
    return {k: float(v) for k, v in L.items() if v == v}

def type_b(ctx, i, d, entry, btc_chg24=None, sym_chg24=None, is_btc=False):
    """Returns (candidate or None, checks). entry = live price (or the trade's entry in replay)."""
    H, F, D = ctx["H"], ctx["F"], ctx["D"]; j4, jd = _idx(ctx, i); a1 = float(H["atr"][i])
    st4 = int(F["st"][j4]) if F["st"][j4] == F["st"][j4] else 0   # scanner's 4H trend field (struct 4H: +1 HH/HL, -1 LH/LL, 0 mixed)
    # 2026-10-23 v3: 4H aligned = EMA alignment AND struct 4H not against the trade (XPL +1 vs short rejected; 0 allowed: XRP 15/10)
    ck = {"4H_ema": aligned(F, j4, d), "4H_struct": st4, "D_not_contrary": bool(d * (D["c"][jd] - D["e20"][jd]) >= 0)}
    ck["4H"] = bool(ck["4H_ema"] and d * st4 >= 0)   # v3: struct 4H must not point AGAINST the trade (0 ok)
    reb = -d * (entry - H["e20"][i]) / a1; ck["rebound_1h_atr"] = round(float(reb), 2); ck["rebound_ok"] = bool(B_MIN_REBOUND_ATR - 1e-9 <= reb <= B_MAX_DIST)
    lv = {k: abs(entry - v) / a1 for k, v in _levels(ctx, i, d).items()}
    if reb > 0.5: lv.pop("1H_EMA20", None)          # 'at the 1H EMA20' only counts when price is actually near it
    best = min(lv.items(), key=lambda kv: kv[1]) if lv else (None, 99)
    ck["level"], ck["level_dist_atr"] = best[0], round(float(best[1]), 2); ck["level_ok"] = bool(best[1] <= B_LEVEL_ATR)
    stop, sinfo = b_structural_stop(H, i, d, entry, a1); ck.update(sinfo)
    ck["rebound_swing"], ck["stop"] = sinfo["rebound_extreme"], stop
    R = d * (entry - stop) if stop is not None else 0.0
    ck["stop_ok"] = bool(stop is not None and R >= sinfo["min_stop_dist"] * (1 - 1e-9))
    if not ck["stop_ok"]: return None, ck
    tg = [float(x) for x in (F["sl"][j4], D["l"][jd], H["sl"][i]) if x == x] if d < 0 else [float(x) for x in (F["sh"][j4], D["h"][jd], H["sh"][i]) if x == x]
    tg = sorted([x for x in tg if 1.5 <= d * (x - entry) / R <= 3.0], key=lambda x: d * (x - entry))
    t2 = tg[0] if tg else entry + d * 2 * R; t2_src = "swing" if tg else "2R"
    t1 = entry + d * 0.8 * R; rr = d * (t2 - entry) / R
    s = 2.0 + (1.0 if abs(F["e20"][j4] - F["e50"][j4]) >= 0.5 * F["atr"][j4] else 0) if ck["4H"] else 0.0
    s += (1.0 if d * (D["c"][jd] - D["e20"][jd]) > 0 else 0) + (0.5 if d * (D["e20"][jd] - D["e50"][jd]) > 0 else 0)
    s += 2.0 if best[1] <= 0.25 else 1.5 if best[1] <= 0.5 else 0
    s += 1.0 if 0.5 <= reb <= 1.5 else 0.5 if reb > 0 else 0
    rA = R / a1; s += 1.0 if 0.4 <= rA <= 1.2 else 0.5 if rA <= 2 else 0
    s += 1.0 if rr >= 2 else 0.5 if rr >= 1.5 else 0
    if is_btc: s += 0.25
    elif btc_chg24 is not None and sym_chg24 is not None and d * (sym_chg24 - btc_chg24) > 0: s += 0.5
    ck.update(score_b=round(s, 2), score_ok=s >= B_MIN_SCORE, rr=round(rr, 2), rr_ok=rr >= MIN_RR, stop_atr=round(rA, 2), t2_source=t2_src)
    ok = ck["4H"] and ck["D_not_contrary"] and ck["rebound_ok"] and ck["level_ok"] and ck["stop_ok"] and ck["score_ok"] and ck["rr_ok"]
    cand = {"setup_type": "B", "dir": "long" if d > 0 else "short", "entry": entry, "stop": stop, "t1": t1, "t2": t2,
            "score": round(s, 2), "rr": round(rr, 2), "risk_pct": B_RISK_PCT, "checks": ck}
    return (cand if ok else None), ck
