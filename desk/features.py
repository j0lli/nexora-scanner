"""Nexora Desk feature modules + setup evaluation (deterministic, causal: only closed bars at evaluation time).
Shared by scanner.py (live) and backtest.py (walk-forward). See scanner.py header for the score rubric."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from desklib import ema, atr, TFSEC

ROME = ZoneInfo("Europe/Rome")
XSTOCKS = {"PF_SPCXXUSD", "PF_NVDAXUSD", "PF_TSLAXUSD", "PF_MSTRXUSD"}
COMMOD = {"PF_WTIOILUSD", "PF_XAUUSD", "PF_XAUTUSD"}
FEE_RT = 0.001
MIN_STOP_PCT = 0.6
MAX_STOP_PCT = 3.0

# Setup A parameters chosen by walk-forward on the in-sample window (see backtest_report.md)
DEFAULT_PARAMS = {"zone": 1.0, "lookback": 12, "trigger": "reclaim_hh"}


def kind(sym):
    return "xstock" if sym in XSTOCKS else "commodity" if sym in COMMOD else "crypto"


def session(ts):
    t = datetime.fromtimestamp(ts, ROME)
    m = t.hour * 60 + t.minute
    name = "Asia" if 60 <= m < 540 else "EU" if 540 <= m < 930 else "US" if 930 <= m < 1320 else "late"
    return name, t


def session_ok(sym, ts):
    """xStocks only Mon-Fri 15:30-22:00 Rome, commodities Mon-Fri 08:00-22:30 Rome, crypto always."""
    _, t = session(ts)
    m = t.hour * 60 + t.minute
    k = kind(sym)
    if k == "xstock":
        return t.weekday() < 5 and 930 <= m < 1320
    if k == "commodity":
        return t.weekday() < 5 and 480 <= m < 1350
    return True


def structure(h, l, c, k=2):
    """Fractal swings (confirmed k bars later). Returns dict of arrays:
    st: +1 HH&HL, -1 LH&LL, 0 mixed; reg: direction of last break of structure (close beyond last swing);
    sh/sl: last confirmed swing high/low; bos_up/bos_dn: index of last close above sh / below sl."""
    n = len(c)
    st = np.zeros(n); reg = np.zeros(n); sh = np.full(n, np.nan); sl = np.full(n, np.nan)
    bu = np.full(n, -10**9); bd = np.full(n, -10**9)
    a = b = x = y = np.nan
    r = 0; lbu = lbd = -10**9
    for j in range(n):
        p = j - k
        if p >= k:
            if h[p] == h[p - k:p + k + 1].max():
                a, b = b, h[p]
            if l[p] == l[p - k:p + k + 1].min():
                x, y = y, l[p]
        if b == b and c[j] > b and (j == 0 or c[j - 1] <= b):
            r = 1; lbu = j
        if y == y and c[j] < y and (j == 0 or c[j - 1] >= y):
            r = -1; lbd = j
        st[j] = 1 if (b > a and y > x) else -1 if (b < a and y < x) else 0
        reg[j] = r; sh[j] = b; sl[j] = y; bu[j] = lbu; bd[j] = lbd
    return {"st": st, "reg": reg, "sh": sh, "sl": sl, "bu": bu, "bd": bd}


def prep(a):
    c, h, l, o, v = a[:, 4], a[:, 2], a[:, 3], a[:, 1], a[:, 5]
    X = {"t": a[:, 0], "o": o, "h": h, "l": l, "c": c, "v": v,
         "e20": ema(c, 20), "e50": ema(c, 50), "atr": atr(h, l, c, 14)}
    X.update(structure(h, l, c))
    return X


def build_ctx(sym, h1, h4, d1):
    H, F, D = prep(h1), prep(h4), prep(d1)
    tc = H["t"] + 3600  # evaluation time = close of 1H bar
    j4 = np.searchsorted(F["t"], tc - TFSEC["4h"], side="right") - 1
    jd = np.searchsorted(D["t"], tc - TFSEC["1d"], side="right") - 1
    # ATR(4H) percentile over last 120 4H bars (causal)
    a4 = F["atr"]; pct = np.full(len(a4), np.nan)
    for j in range(140, len(a4)):
        w = a4[j - 120:j + 1]
        pct[j] = (w < a4[j]).mean() * 100
    F["atr_pct"] = pct
    return {"sym": sym, "kind": kind(sym), "H": H, "F": F, "D": D, "j4": j4, "jd": jd}


def trend_full(X, j, d):
    if j < 55 or X["e50"][j] != X["e50"][j]:
        return False
    return d * (X["c"][j] - X["e20"][j]) > 0 and d * (X["c"][j] - X["e50"][j]) > 0 and d * (X["e20"][j] - X["e20"][j - 3]) > 0


def round_levels(p):
    step = 10 ** (math.floor(math.log10(p)) - 1) * 5
    b = math.floor(p / step) * step
    return [b, b + step]


def key_levels(ctx, i):
    """Prior day H/L, prior week H/L, weekly open, 4H/1D last swings, round numbers."""
    H, F, D = ctx["H"], ctx["F"], ctx["D"]
    j4, jd = ctx["j4"][i], ctx["jd"][i]
    T = H["t"][i] + 3600
    L = {}
    if jd >= 1:
        L["PDH"], L["PDL"] = D["h"][jd], D["l"][jd]
    day = (T - 3600) // 86400 * 86400
    ws = (day - 4 * 86400) // 604800 * 604800 + 4 * 86400
    k0, k1 = np.searchsorted(D["t"], ws - 7 * 86400), np.searchsorted(D["t"], ws)
    if k1 - k0 >= 5:
        L["PWH"], L["PWL"] = D["h"][k0:k1].max(), D["l"][k0:k1].min()
    kw = np.searchsorted(H["t"], ws)
    if kw <= i and H["t"][kw] - ws < 7200:
        L["WO"] = H["o"][kw]
    for nm, X, j in (("4H", F, j4), ("1D", D, jd)):
        if j >= 0 and X["sh"][j] == X["sh"][j]:
            L[f"{nm}_SH"], L[f"{nm}_SL"] = X["sh"][j], X["sl"][j]
    r = round_levels(H["c"][i])
    L["RND_lo"], L["RND_hi"] = r
    return {k: float(v) for k, v in L.items() if v == v}


def anchored_vwap(H, i):
    day = H["t"][i] // 86400 * 86400
    i0 = int(np.searchsorted(H["t"], day))
    tp = (H["h"][i0:i + 1] + H["l"][i0:i + 1] + H["c"][i0:i + 1]) / 3
    v = H["v"][i0:i + 1]
    return float((tp * v).sum() / v.sum()) if v.sum() > 0 else float(H["c"][i])


def value_area(H, i, n=24, bins=24):
    lo, hi = H["l"][i - n + 1:i + 1].min(), H["h"][i - n + 1:i + 1].max()
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1); vol = np.zeros(bins)
    for k in range(i - n + 1, i + 1):  # spread each bar's volume uniformly over its range
        a = np.searchsorted(edges, H["l"][k], side="right") - 1
        b = np.searchsorted(edges, H["h"][k], side="left")
        a, b = max(a, 0), min(max(b, a + 1), bins)
        vol[a:b] += H["v"][k] / (b - a)
    poc = int(vol.argmax()); tot = vol.sum(); lo_i = hi_i = poc; acc = vol[poc]
    while acc < 0.7 * tot and (lo_i > 0 or hi_i < bins - 1):
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        dn = vol[lo_i - 1] if lo_i > 0 else -1
        if up >= dn: hi_i += 1; acc += up
        else: lo_i -= 1; acc += dn
    mid = (edges[:-1] + edges[1:]) / 2
    return {"poc": float(mid[poc]), "val": float(edges[lo_i]), "vah": float(edges[hi_i + 1])}


def btc_state(btc, T):
    """BTC 4H regime (+1/-1/0) and 1D EMA trend at time T."""
    if btc is None:
        return 0, 0
    F, D = btc["F"], btc["D"]
    j = np.searchsorted(F["t"], T - TFSEC["4h"], side="right") - 1
    jd = np.searchsorted(D["t"], T - TFSEC["1d"], side="right") - 1
    c, e20, e50 = F["c"][j], F["e20"][j], F["e50"][j]
    r4 = 1 if c > e20 > e50 else -1 if c < e20 < e50 else 0
    rd = 1 if trend_full(D, jd, 1) else -1 if trend_full(D, jd, -1) else 0
    return r4, rd


def rel_strength(ctx, btc, i, n):
    H = ctx["H"]
    if btc is None or i < n:
        return None
    B = btc["H"]; T0, T1 = H["t"][i - n], H["t"][i]
    k0, k1 = np.searchsorted(B["t"], T0), np.searchsorted(B["t"], T1)
    if k1 >= len(B["t"]) or B["t"][k1] != T1 or B["t"][k0] != T0:
        return None
    return float((H["c"][i] / H["c"][i - n] - 1) - (B["c"][k1] / B["c"][k0] - 1)) * 100


def evaluate(ctx, i, d, setup, btc=None, params=None):
    """Return hit dict (with module breakdown) or None. d=+1 long, -1 short."""
    p = params or DEFAULT_PARAMS
    H, F, D = ctx["H"], ctx["F"], ctx["D"]
    j4, jd = int(ctx["j4"][i]), int(ctx["jd"][i])
    if i < 80 or j4 < 150 or jd < 55:
        return None
    T = H["t"][i] + 3600
    sym = ctx["sym"]
    if not session_ok(sym, T):
        return None
    a1, a4, aD = H["atr"][i], F["atr"][j4], D["atr"][jd]
    c, o, h, l = H["c"][i], H["o"][i], H["h"][i], H["l"][i]
    e1 = H["e20"][i]; e20_4, e50_4 = F["e20"][j4], F["e50"][j4]
    # ---- hard filters common: volatility expansion
    if (h - l) > 2.0 * a1 or (H["h"][i - 1] - H["l"][i - 1]) > 2.0 * a1:
        return None
    trD, tr4 = trend_full(D, jd, d), trend_full(F, j4, d)
    strong4 = d * (F["c"][j4] - e20_4) > 0 and d * (e20_4 - e50_4) > 0
    is_alt = ctx["kind"] == "crypto" and sym != "PF_XBTUSD"
    T_btc = btc_state(btc, T) if is_alt else (0, 0)
    rs24 = rel_strength(ctx, btc, i, 24) if is_alt else None
    rs72 = rel_strength(ctx, btc, i, 72) if is_alt else None
    if is_alt and T_btc[0] == -d and rs72 is not None and d * rs72 < 0:
        return None  # BTC regime filter: weak-vs-BTC alt against BTC trend
    info = {}
    if setup == "A":
        # trend aligned D + 4H (EMA trend), daily structure not opposite
        if not (trD and tr4) or D["st"][jd] == -d:
            return None
        N = p["lookback"]
        if d > 0:
            pl = H["l"][i - N:i + 1].min()
        else:
            pl = H["h"][i - N:i + 1].max()
        in_zone = abs(pl - e20_4) <= p["zone"] * a4
        levels = key_levels(ctx, i)
        sup = [(k, v) for k, v in levels.items() if d * (c - v) > 0 and abs(pl - v) <= 0.3 * a4]
        if not (in_zone or sup):
            return None
        # 1H reclaim trigger
        reclaim = d * (c - e1) > 0 and any(d * (H["c"][k] - H["e20"][k]) <= 0 for k in (i - 1, i - 2))
        if not reclaim or d * (c - o) <= 0:
            return None
        if p["trigger"] == "reclaim_hh" and not (d * (c - (H["h"][i - 3:i].max() if d > 0 else H["l"][i - 3:i].min())) > 0):
            return None
        if abs(c - e20_4) > 1.5 * a4:
            return None
        info.update(pullback_extreme=float(pl), in_4h_ema_zone=bool(in_zone), at_levels=[k for k, _ in sup])
        swing = pl
    else:  # Setup B momentum
        if not strong4:
            return None
        prev_h, prev_l = H["h"][i - 24:i].max(), H["l"][i - 24:i].min()
        brk = c > prev_h if d > 0 else c < prev_l
        vavg = H["v"][i - 20:i].mean(); vr = H["v"][i] / vavg if vavg > 0 else 0
        ext = d * (c - e20_4) / a4
        if not brk or vr < 1.5 or ext >= 2.0 or ext < -0.5:
            return None
        if d * (D["e20"][jd] - D["c"][jd]) > aD:
            return None
        levels = key_levels(ctx, i)
        broken = [k for k, v in levels.items() if d * (c - v) > 0 and d * (H["o"][i] - v) <= 0]
        sup = [(k, levels[k]) for k in broken]
        in_zone = False
        swing = H["l"][i - 4:i + 1].min() if d > 0 else H["h"][i - 4:i + 1].max()
        info.update(broke_24h=float(prev_h if d > 0 else prev_l), vol_ratio=round(float(vr), 2), levels_broken=broken)
    # ---- stop: beyond structural swing + buffer, min max(0.6%, 1 ATR1h)
    entry = float(c)
    stop = swing - d * 0.1 * a1
    min_sd = max(MIN_STOP_PCT / 100 * entry, a1)
    if d * (entry - stop) < min_sd:
        stop = entry - d * min_sd
    sd = abs(entry - stop)
    sd_pct = sd / entry * 100
    if sd_pct > MAX_STOP_PCT:
        return None
    fee_R = FEE_RT * entry / sd
    # ---- MODULES
    m = {}
    # 1 structure 0-2.5
    s = 0.0
    s += 0.75 if D["st"][jd] == d else 0.4 if D["reg"][jd] == d else 0
    s += 0.75 if F["st"][j4] == d else 0.4 if F["reg"][j4] == d else 0
    last_bos = H["bu"][i] if d > 0 else H["bd"][i]
    s += 0.5 if i - last_bos <= 6 else 0
    s += 0.5 if (trD and tr4) else 0
    m["structure"] = round(s, 2)
    # 2 location / levels 0-2
    s = (1.5 if (in_zone and sup) else 1.0 if (in_zone or sup) else 0)
    s += 0.5 if abs(entry - e20_4) <= 1.0 * a4 else 0
    m["levels"] = s
    # 3 volatility 0-1
    pct = F["atr_pct"][j4]
    s = (0.5 if 20 <= pct <= 85 else 0) + (0.5 if max(H["h"][k] - H["l"][k] for k in range(i - 2, i + 1)) <= 1.5 * a1 else 0)
    m["volatility"] = s
    # 4 volume 0-1.5
    v20 = H["v"][i - 20:i].mean()
    rv = H["v"][i] / v20 if v20 > 0 else 0
    pull_v = H["v"][i - 5:i].mean()
    pre_v = H["v"][i - 25:i - 5].mean()
    vw = anchored_vwap(H, i)
    s = (0.5 if rv >= 1.2 else 0) + (0.5 if pull_v < pre_v else 0) + (0.5 if d * (entry - vw) > 0 else 0)
    m["volume"] = s
    # 5 regime / relative strength 0-1.5
    if is_alt:
        s = (0.75 if T_btc[0] == d else 0.35 if T_btc[0] == 0 else 0) + (0.75 if (rs72 is not None and d * rs72 > 0) else 0)
    else:
        s = (0.75 if trD else 0) + 0.4
    m["regime_rs"] = round(s, 2)
    # 6 trade quality 0-1.5: fees, stop size, room to next opposing level >= 2R
    opp = [v for v in levels.values() if d * (v - entry) > 0.1 * sd]
    room = min(abs(v - entry) for v in opp) / sd if opp else 9.9
    s = (0.5 if fee_R <= 0.1 else 0) + (0.5 if sd <= 2.0 * a1 else 0) + (0.5 if room >= 2.0 else 0)
    m["trade_quality"] = s
    score = round(min(10.0, sum(m.values())), 2)
    return {"symbol": sym, "setup": setup, "dir": "long" if d > 0 else "short", "score": score, "modules": m,
            "entry": entry, "stop": float(stop), "stop_pct": round(sd_pct, 3), "fee_R": round(fee_R, 3),
            "t1": entry + d * 0.8 * sd, "t2": entry + d * 2.0 * sd, "room_R": round(room, 2),
            "bar_time": int(H["t"][i]), "atr1h": float(a1), "atr4h_pct": None if pct != pct else round(float(pct)),
            "dist_4h_ema20_atr": round(float(d * (entry - e20_4) / a4), 2), "vwap_day": vw, "rel_vol": round(float(rv), 2),
            "rs24_vs_btc": None if rs24 is None else round(rs24, 2), "rs72_vs_btc": None if rs72 is None else round(rs72, 2),
            "btc_4h_regime": T_btc[0] if is_alt else None, "struct": {"1D": int(D["st"][jd]), "4H": int(F["st"][j4]), "1H": int(H["st"][i])},
            "levels": {k: float(f"{v:.6g}") for k, v in levels.items()}, **info}
