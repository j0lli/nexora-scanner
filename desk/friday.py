"""Friday hints (2026-10-22): FREE, deterministic pattern of the desk's good trades (XRP short, BTC short, FIL long 15-16 Oct).
Rule (short; mirror for long): on the last CLOSED Daily, 4H AND 1H bar EMA20<EMA50 and close<EMA20; live mark on the trade
side of the 1H EMA20 and within 1.0 ATR(1H) of it (not extended). Entry = mark. Stop = beyond the 1H swing high/low of the
last 6 closed bars + 0.1 ATR, widened to at least max(1 ATR1h, 0.6%). T1 = entry +- 0.8R. T2 = nearest daily/4H swing
beyond >= 1.5R (and <= 3R), else 2R. Strength = directional 24h change vs BTC (+ = moving our way faster than BTC).
NEVER places orders - output only (scan_latest.json["friday_hints"], friday_hints.jsonl, dedupe symbol+dir 2h)."""
import json, os, time
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np

ROME = ZoneInfo("Europe/Rome")
MAX_EXT_ATR, MIN_STOP_PCT, DEDUPE_SEC = 1.0, 0.6, 2 * 3600


def _closed(X, tfsec, now):
    return int(np.searchsorted(X["t"], now - tfsec, side="right") - 1)


def _aligned(X, j, d):
    if j < 55 or np.isnan(X["e50"][j]): return False
    return d * (X["e50"][j] - X["e20"][j]) < 0 and d * (X["c"][j] - X["e20"][j]) > 0


def _chg(H, i, bars):
    return (H["c"][i] / H["c"][i - bars] - 1) * 100 if i >= bars else None


def _swings(X, j, n=30):
    hi, lo = [], []
    for k in range(max(2, j - n), j - 1):
        if X["h"][k] >= X["h"][k - 2:k + 3].max(): hi.append(X["h"][k])
        if X["l"][k] <= X["l"][k - 2:k + 3].min(): lo.append(X["l"][k])
    return hi, lo


def friday_hints(ctxs, tick, now, fmt):
    out = []
    btc = ctxs.get("PF_XBTUSD")
    ib = _closed(btc["H"], 3600, now) if btc else None
    b24 = float(tick.get("PF_XBTUSD", {}).get("change24h") or 0)
    b72 = _chg(btc["H"], ib, 72) if btc else None
    for s, ctx in ctxs.items():
        t = tick.get(s)
        if not t or not t.get("last"): continue
        H, F, D = ctx["H"], ctx["F"], ctx["D"]
        i, j4, jd = _closed(H, 3600, now), _closed(F, 14400, now), _closed(D, 86400, now)
        if i < 60 or j4 < 55 or jd < 55: continue
        mark = float(t.get("markPrice") or t["last"])
        a1, e1 = H["atr"][i], H["e20"][i]
        if not a1 or np.isnan(a1): continue
        for d in (1, -1):
            if not (_aligned(D, jd, d) and _aligned(F, j4, d) and _aligned(H, i, d)): continue
            dist = (mark - e1) / a1  # signed, ATR1h
            if d * dist < 0 or abs(dist) > MAX_EXT_ATR: continue
            entry = mark
            sw = H["h"][i - 5:i + 1].max() if d < 0 else H["l"][i - 5:i + 1].min()
            stop = sw - d * 0.1 * a1
            min_sd = max(a1, MIN_STOP_PCT / 100 * entry)
            if d * (entry - stop) < min_sd: stop = entry - d * min_sd
            sd = abs(entry - stop)
            if sd / entry * 100 < MIN_STOP_PCT: continue
            t1 = entry + d * 0.8 * sd
            dh, dl = _swings(D, jd); fh, fl = _swings(F, j4)
            lv = (dl + fl + [D["l"][max(0, jd - 9):jd + 1].min()]) if d < 0 else (dh + fh + [D["h"][max(0, jd - 9):jd + 1].max()])
            cand = sorted([x for x in lv if 1.5 * sd <= d * (x - entry) <= 3 * sd], key=lambda x: d * (x - entry))
            if cand: t2, t2src = cand[0] - d * 0.1 * a1, "swing"
            else: t2, t2src = entry + d * 2 * sd, "2R"
            c24 = float(t.get("change24h") or 0); c72 = _chg(H, i, 72)
            strength = d * (c24 - b24)
            out.append({"symbol": s, "dir": "long" if d > 0 else "short", "mark": fmt(mark), "entry": fmt(entry),
                        "stop": fmt(stop), "stop_pct": round(sd / entry * 100, 3), "t1": fmt(t1), "t2": fmt(t2),
                        "t2_source": t2src, "rr_t2": round(abs(t2 - entry) / sd, 2),
                        "dist_1h_ema20_atr": round(float(dist), 2), "chg24h_pct": round(c24, 2),
                        "strength_vs_btc_24h": round(strength, 2),
                        "strength_vs_btc_72h": None if c72 is None or b72 is None else round(d * (c72 - b72), 2),
                        "atr1h_pct": round(float(a1 / entry * 100), 3), "bar_time_1h": int(H["t"][i])})
    out.sort(key=lambda h: -h["strength_vs_btc_24h"])
    return out


def log_new(hints, now, desk, ts_rome, fn_name="friday_hints_shadow.jsonl"):
    fn = os.path.join(desk, fn_name); recent = {}
    if os.path.exists(fn):
        for line in open(fn).readlines()[-1000:]:
            try: r = json.loads(line); recent[(r["symbol"], r["dir"])] = r["ts_unix"]
            except Exception: pass
    new = 0
    with open(fn, "a") as f:
        for h in hints:
            k = (h["symbol"], h["dir"])
            if now - recent.get(k, 0) < DEDUPE_SEC: continue
            f.write(json.dumps({"ts_unix": int(now), "timestamp_rome": ts_rome, **h}, default=float) + "\n")
            recent[k] = now; new += 1
    return new
