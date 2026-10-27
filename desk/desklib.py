"""Shared, deterministic logic for scanner.py and backtest.py (no LLM, public Kraken Futures endpoints only)."""
import json, os, time, math, urllib.request
import numpy as np

BASE_T = "https://futures.kraken.com/derivatives/api/v3"
BASE_C = "https://futures.kraken.com/api/charts/v1/trade"
DESK = os.environ.get("DESK_ROOT") or os.path.dirname(os.path.abspath(__file__))  # cloud bundle: path only
CACHE = os.path.join(DESK, "cache")
TFSEC = {"1h": 3600, "4h": 14400, "1d": 86400}
UA = {"User-Agent": os.environ.get("DESK_HTTP_UA") or "nexora-desk-scanner/1.0"}  # cloud bundle: optional UA override


def http_json(url, retries=3, timeout=20):
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def fetch_candles(sym, tf, frm, to, sleep=0.15):
    """Paginated candles [t(sec), o, h, l, c, v] sorted, deduped."""
    out = {}
    cur = int(frm)
    for _ in range(20):
        d = http_json(f"{BASE_C}/{sym}/{tf}?from={cur}&to={int(to)}")
        cs = d.get("candles", [])
        for c in cs:
            t = int(c["time"]) // 1000
            out[t] = [t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]), float(c["volume"])]
        time.sleep(sleep)
        if not d.get("more_candles") or not cs:
            break
        nxt = int(cs[-1]["time"]) // 1000 + TFSEC[tf]
        if nxt <= cur:
            break
        cur = nxt
    return np.array([out[k] for k in sorted(out)], dtype=float) if out else np.zeros((0, 6))


def cached_candles(sym, tf, lookback_sec, max_age, now=None):
    now = now or time.time()
    fn = os.path.join(CACHE, f"{sym}_{tf}.json")
    if os.path.exists(fn) and now - os.path.getmtime(fn) < max_age:
        try:
            with open(fn) as f:
                return np.array(json.load(f), dtype=float)
        except Exception:
            pass
    arr = fetch_candles(sym, tf, now - lookback_sec, now)
    tmp = fn + ".tmp"
    with open(tmp, "w") as f:
        json.dump(arr.tolist(), f)
    os.replace(tmp, fn)
    return arr


def ema(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h, l, c, n=14):
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n  # Wilder
    return out


def indicators(a):
    """a: candles array. returns dict of arrays."""
    if len(a) == 0:
        return None
    c, h, l, v = a[:, 4], a[:, 2], a[:, 3], a[:, 5]
    return {"t": a[:, 0], "o": a[:, 1], "h": h, "l": l, "c": c, "v": v,
            "e20": ema(c, 20), "e50": ema(c, 50), "atr": atr(h, l, c, 14)}


def trend(ind, j, d):
    """Full trend on closed bar j in direction d (+1 long/-1 short):
    close beyond EMA20 and EMA50, EMA20 sloping the trade way (vs 3 bars ago)."""
    if j < 53 or np.isnan(ind["e50"][j]):
        return False
    c, e20, e50 = ind["c"][j], ind["e20"][j], ind["e50"][j]
    return d * (c - e20) > 0 and d * (c - e50) > 0 and d * (e20 - ind["e20"][j - 3]) > 0


def strong4h(ind, j, d):
    """close > EMA20 > EMA50 (long) / mirror."""
    if j < 53 or np.isnan(ind["e50"][j]):
        return False
    c, e20, e50 = ind["c"][j], ind["e20"][j], ind["e50"][j]
    return d * (c - e20) > 0 and d * (e20 - e50) > 0


def last_closed(ind, tf, t_now):
    """index of last bar fully closed at unix time t_now (bar open + tf <= t_now)."""
    t = ind["t"]
    k = np.searchsorted(t, t_now - TFSEC[tf], side="right") - 1
    return int(k)


SETUP_BASE = {"A": 0.0, "B": 0.0}


def evaluate(sym, H, F, D, i, d, price, btc4=None, btc_j=None):
    """Evaluate setups at 1H closed bar i (H index), direction d, current price.
    F, D: 4H / 1D indicator dicts. Uses only bars closed at time H.t[i]+3600.
    Returns list of hit dicts."""
    hits = []
    tnow = H["t"][i] + 3600
    j4 = last_closed(F, "4h", tnow)
    jd = last_closed(D, "1d", tnow)
    if i < 60 or j4 < 55 or jd < 55:
        return hits
    a4 = F["atr"][j4]
    if not a4 or np.isnan(a4):
        return hits
    e20_4 = F["e20"][j4]
    ext = (price - e20_4) / a4 * d          # signed extension in trade direction (ATR4h)
    absext = abs(price - e20_4) / a4
    a1 = H["atr"][i]
    o, h, l, c = H["o"][i], H["h"][i], H["l"][i], H["c"][i]
    e1 = H["e20"][i]
    rng = max(h - l, 1e-12)
    trD = trend(D, jd, d)
    tr4 = trend(F, j4, d)

    # --- Setup A: Daily + 4H aligned, within 1.5 ATR4h of 4H EMA20, 1H trigger
    if trD and tr4 and absext <= 1.5:
        reclaim = d * (c - e1) > 0 and d * (H["c"][i - 1] - H["e20"][i - 1]) <= 0
        if d > 0:
            wick = (min(o, c) - l) / rng
            touched = l <= max(e1, e20_4) + 0.25 * a1 or l <= min(H["l"][i - 6:i]) + 0.1 * a1
            closed_pos = (c - l) / rng
        else:
            wick = (h - max(o, c)) / rng
            touched = h >= min(e1, e20_4) - 0.25 * a1 or h >= max(H["h"][i - 6:i]) - 0.1 * a1
            closed_pos = (h - c) / rng
        rejection = touched and wick >= 0.4 and closed_pos >= 0.5 and d * (c - e1) > 0
        if reclaim or rejection:
            q = 2.0 if (reclaim and rejection) else 1.5 if rejection else 1.0
            hits.append(("A", q, {"reclaim_1h_ema20": bool(reclaim), "rejection": bool(rejection)}))

    # --- Setup B: momentum breakout of prior 24h range on volume
    if strong4h(F, j4, d) and ext < 2.0 and ext > -0.5 and i >= 45:
        prev_h = H["h"][i - 24:i].max()
        prev_l = H["l"][i - 24:i].min()
        vavg = H["v"][i - 20:i].mean()
        vr = H["v"][i] / vavg if vavg > 0 else 0
        brk = (c > prev_h) if d > 0 else (c < prev_l)
        aD, e20D, cD = D["atr"][jd], D["e20"][jd], D["c"][jd]
        daily_ok = not (d * (e20D - cD) > aD)   # wrong side by more than 1 daily ATR -> reject
        if brk and vr >= 1.5 and daily_ok:
            q = 2.0 if vr >= 2.5 else 1.5 if vr >= 2.0 else 1.0
            hits.append(("B", q, {"broke": prev_h if d > 0 else prev_l, "vol_ratio": round(vr, 2)}))

    out = []
    for setup, q, extra in hits:
        # structural stop: last 1H swing (5 closed bars) beyond + buffer
        if d > 0:
            sw = H["l"][i - 4:i + 1].min()
            stop = sw - max(0.1 * a1, 0.0005 * price)
        else:
            sw = H["h"][i - 4:i + 1].max()
            stop = sw + max(0.1 * a1, 0.0005 * price)
        # entry: last price, or nearest 1H micro-level within 0.3% on the favourable side
        entry = price
        levels = [e1, (h + l) / 2] + ([extra["broke"]] if setup == "B" else [])
        cand = [lv for lv in levels if d * (price - lv) > 0 and abs(price - lv) / price <= 0.003 and d * (lv - stop) > 0]
        if cand:
            entry = max(cand) if d > 0 else min(cand)
        sd = abs(entry - stop)
        if sd <= 0.3 * a1 * 0.5 or d * (entry - stop) <= 0:
            continue
        sd_pct = sd / entry * 100
        # ---- score rubric (see scanner.py header)
        s_tr = (1.5 if trD else (0.5 if setup == "B" and d * (D["c"][jd] - D["e20"][jd]) > -0.0 else 0.0)) + (1.5 if tr4 else (1.0 if strong4h(F, j4, d) else 0.0))
        s_loc = 2.0 if absext <= 0.5 else 1.5 if absext <= 1.0 else 1.0 if absext <= 1.5 else 0.5 if absext < 2.0 else 0.0
        s_trg = q
        r_atr = sd / a1
        s_stop = 1.5 if r_atr <= 1.5 else 1.0 if r_atr <= 2.5 else 0.5 if r_atr <= 3.5 else 0.0
        if sd_pct > 3.0:
            s_stop = 0.0
        s_ctx = 0.0
        if btc4 is not None:
            jb = last_closed(btc4, "4h", tnow)
            if jb >= 55 and d * (btc4["c"][jb] - btc4["e20"][jb]) > 0:
                s_ctx += 1.0
        if d * (H["e20"][i] - H["e50"][i]) > 0:
            s_ctx += 0.5
        score = round(min(10.0, s_tr + s_loc + s_trg + s_stop + s_ctx), 1)
        out.append({"symbol": sym, "setup": setup, "dir": "long" if d > 0 else "short", "score": score,
                    "score_parts": {"trend": s_tr, "location": s_loc, "trigger": s_trg, "stop": s_stop, "context": s_ctx},
                    "entry": entry, "stop": stop, "stop_pct": round(sd_pct, 3),
                    "t1": entry + d * 0.8 * sd, "t2": entry + d * 2.0 * sd,
                    "ext_atr4h": round(ext, 2), "bar_time": int(H["t"][i]), **{k: v for k, v in extra.items()}})
    return out
