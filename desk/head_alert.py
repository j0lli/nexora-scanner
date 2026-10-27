"""Head of Trading wake-up webhook (deterministic, no Grok). Called by scanner.py after each scan; NEVER raises.
Sends a short JSON POST only for hits that match the 16/10 method and pass every gate:
  * desk hours Mon-Fri 08:00-23:00 Europe/Rome
  * no desk/state/ENTRIES_SUSPENDED; equity.json readable, fresh (<15 min), daily P&L > -4% and < +1.6% (2026-10-26: day closes at +1.6%)
  * scanner hit with setup_type A or B (never Friday hints / untyped), size-feasible, not a correlation duplicate
  * xStocks (features.kind == 'xstock') only Mon-Fri 15:30-22:00 Rome (US cash session); outside -> logged as skip, no alert
  * A: score >= 7; D + 4H + 1H aligned on CLOSED bars; entry on trade side of 1H EMA20 within 1.0 ATR(1H); risk 2.5/3%
  * B: score >= 7.5; 4H aligned (EMA + struct 4H not against the trade); rebound 0.5-1.5 ATR; stop <= 3%; daily close not beyond daily EMA20 against the trade; entry on the opposite side of
       the 1H EMA20 (rebound) within 1.5 ATR(1H); stop distance >= max(0.5 ATR(1H), 0.4%) and <= 3% (2026-10-23 v3, same limits
       as setup_types.B_MIN_STOP_ATR / B_MIN_STOP_PCT); risk 1.5%   (thresholds from setup_types.py)
  * R:R to the final target (T2) >= 1.5
  * dedup: same symbol+direction at most once per 2 h (successful deliveries in desk/state/head_alerts.jsonl)
Secrets: DESK_HEAD_WEBHOOK_URL / DESK_HEAD_WEBHOOK_KEY read from the environment at send time; never logged or stored.
Auth: 'Authorization: Bearer <key>'; on 401/403 retried once with 'X-Webhook-Key: <key>'. The header NAME that worked is
remembered in desk/state/head_webhook_auth.json (no secret). Timeout 5 s."""
import json, os, time, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
ROME = ZoneInfo("Europe/Rome")
DESK = os.path.dirname(os.path.abspath(__file__)); STATE = os.path.join(DESK, "state"); CACHE = os.path.join(DESK, "cache")
ALERTS = os.path.join(STATE, "head_alerts.jsonl"); AUTHF = os.path.join(STATE, "head_webhook_auth.json")
SUSPEND = os.path.join(STATE, "ENTRIES_SUSPENDED"); EQUITY = os.path.join(STATE, "equity.json")
MIN_SCORE, MIN_RR, MAX_DIST_ATR, DEDUP_SEC, TIMEOUT = 7.0, 1.5, 1.0, 2 * 3600, 5
# 2026-10-22 18:5x HoT limits for Type B: 1 open B trade at a time; max 2 delivered B alerts per Rome day (all symbols);
# crypto B long needs BTC 4H not bearish, B short needs BTC 4H not bullish (xstocks/commodities exempt).
B_MAX_OPEN, B_MAX_ALERTS_DAY = 1, 10**6   # 2026-10-26 owner: no daily alert cap; day closes at +1.6% instead
# 2026-10-23: ledger rows carry filter_ver; only rows delivered under the CURRENT filter count toward the daily B cap, so
# alerts that today's filters reject (XPL 0.5-ATR stop, NVDAX 4H-neutral/0.08-ATR rebound, sent 08:25 under the old filter)
# never consume it. Rejected hits are never written to the ledger.
FILTER_VER = "2026-10-27a"   # 27/10 owner: pre-26/10 entry filter (= 2026-10-23d: stop floor 1.0 ATR1h, gross R:R); 26/10a bounce filter + net R:R removed
TRADES = os.environ.get("DESK_TRADES_DIR") or os.path.join(DESK, "no_trades")  # cloud bundle: no trade logs in the public repo
B_ACTIVE = ("PLACED", "PENDING", "OPEN", "WORKING", "LIVE")
STOP_LO, STOP_HI, EQ_STALE = -4.0, 1.6, 15 * 60
TFSEC = {"1d": 86400, "4h": 14400, "1h": 3600}

import sys as _sys; _sys.path.insert(0, DESK)
from desklib import ema as _dl_ema, atr as _dl_atr   # same EMA/ATR as the scanner (features.prep)
import setup_types
def _ema(x, n): return _dl_ema(np.asarray(x, dtype=float), n)
def _atr(C, n=14): return _dl_atr(C[:, 2], C[:, 3], C[:, 4], n)

XSTOCKS_FILE, XSTOCKS_TTL = os.path.join(CACHE, "xstocks.json"), 24 * 3600
def xstock_set():
    """xStock perps = Kraken instruments category 'xStocks' (cached 24h in cache/xstocks.json) + features.XSTOCKS.
    Public endpoint, 5 s timeout; on failure the stale cache (or features.XSTOCKS only) is used. Never raises."""
    from features import XSTOCKS
    out = set(XSTOCKS)
    try:
        if os.path.exists(XSTOCKS_FILE) and time.time() - os.path.getmtime(XSTOCKS_FILE) < XSTOCKS_TTL:
            return out | set(json.load(open(XSTOCKS_FILE)))
        with urllib.request.urlopen("https://futures.kraken.com/derivatives/api/v3/instruments", timeout=TIMEOUT) as r:
            L = sorted(i["symbol"] for i in json.load(r).get("instruments", []) if i.get("category") == "xStocks" and i["symbol"].startswith("PF_"))
        if L: json.dump(L, open(XSTOCKS_FILE + ".tmp", "w")); os.replace(XSTOCKS_FILE + ".tmp", XSTOCKS_FILE)
        return out | set(L)
    except Exception:
        try: return out | set(json.load(open(XSTOCKS_FILE)))
        except Exception: return out

def us_cash_open(now):
    t = datetime.fromtimestamp(now, ROME); m = t.hour * 60 + t.minute
    return t.isoweekday() <= 5 and 930 <= m < 1320          # Mon-Fri 15:30-22:00 Rome

def _candles(sym, tf):
    return np.array(json.load(open(os.path.join(CACHE, f"v2_{sym}_{tf}.json"))), dtype=float)

def trend_check(sym, d, entry, now, loader=_candles):
    """d=+1 long, -1 short. Returns dict(aligned={1d,4h,1h}, dist_1h_atr) on CLOSED bars only."""
    out = {"aligned": {}, "contrary": {}}
    for tf in ("1d", "4h", "1h"):
        C = loader(sym, tf); C = C[C[:, 0] + TFSEC[tf] <= now]
        if len(C) < 55: out["aligned"][tf] = False; continue
        c = C[:, 4]; e20, e50 = _ema(c, 20), _ema(c, 50)
        out["aligned"][tf] = bool(d * (e20[-1] - e50[-1]) > 0 and d * (c[-1] - e20[-1]) > 0)
        out["contrary"][tf] = bool(d * (c[-1] - e20[-1]) < 0)   # close beyond EMA20 on the side opposite to the trade
        if tf == "4h":
            from features import structure
            st = structure(C[:, 2], C[:, 3], c)["st"][-1]; out["struct_4h"] = int(st) if st == st else 0
        if tf == "1h": a1 = float(_atr(C)[-1]); out["atr_1h"] = a1; out["dist_1h_atr"] = float(d * (entry - e20[-1]) / a1)
    return out

def gates(now, equity_path=EQUITY, suspend_path=SUSPEND):
    t = datetime.fromtimestamp(now, ROME)
    if t.isoweekday() > 5 or not (8 <= t.hour < 23): return "outside desk hours"
    if os.path.exists(suspend_path): return "ENTRIES_SUSPENDED"
    try: e = json.load(open(equity_path))
    except Exception as ex: return f"equity.json unreadable ({type(ex).__name__})"
    if now - e.get("ts_unix", 0) > EQ_STALE: return "equity.json stale"
    p = float(e.get("daily_pnl_pct", 0))
    if p <= STOP_LO: return f"daily stop {p:.2f}% <= {STOP_LO}%"
    if p >= STOP_HI: return f"daily target {p:.2f}% >= +{STOP_HI}%"
    return None

def qualify(h, now, loader=_candles):
    """Independent re-check of the scanner's setup_type (A or B) on cached closed candles; untyped hits never alert."""
    d = 1 if h["dir"] == "long" else -1
    if h.get("friday") or h.get("source") == "friday": return None, "friday hint"
    st = h.get("setup_type")
    if st not in ("A", "B"): return None, "no setup_type"
    min_score = setup_types.A_MIN_SCORE if st == "A" else setup_types.B_MIN_SCORE
    if float(h["score"]) < min_score: return None, f"score<{min_score}"
    if not h.get("sizing", {}).get("feasible", False): return None, "not feasible"
    if h.get("correlated_with"): return None, "correlation dup"
    if not us_cash_open(now) and h["symbol"] in xstock_set(): return None, "xstock outside US cash 15:30-22:00 Rome (log only)"
    e, s, t2 = float(h["entry"]), float(h["stop"]), float(h["t2"])
    R = d * (e - s)
    if R <= 0: return None, "stop on wrong side"
    rr = d * (t2 - e) / R
    if rr < MIN_RR: return None, f"RR {rr:.2f}<1.5"
    tc = trend_check(h["symbol"], d, e, now, loader); dist = tc.get("dist_1h_atr")
    if st == "A":
        if not all(tc["aligned"].values()): return None, f"A: not D+4H+1H aligned {tc['aligned']}"
        if dist is None or not (0 <= dist <= MAX_DIST_ATR): return None, f"A: 1H EMA20 dist {dist} ATR out of [0,{MAX_DIST_ATR}]"
    else:
        if not tc["aligned"].get("4h"): return None, "B: 4H not aligned"
        if d * tc.get("struct_4h", 0) < 0: return None, f"B: 4H struct {tc.get('struct_4h')} against the trade"
        if tc["contrary"].get("1d", True): return None, "B: daily contrary"
        if dist is None or not (-setup_types.B_MAX_DIST <= dist <= -setup_types.B_MIN_REBOUND_ATR + 1e-6):
            return None, f"B: 1H rebound {dist} ATR out of [-{setup_types.B_MAX_DIST},-{setup_types.B_MIN_REBOUND_ATR}]"
        a1 = tc.get("atr_1h"); min_sd = max(setup_types.B_MIN_STOP_ATR * (a1 or 0), setup_types.B_MIN_STOP_PCT / 100 * e)
        if R / e * 100 > setup_types.B_MAX_STOP_PCT + 1e-9: return None, f"B: stop {R / e * 100:.2f}% > max {setup_types.B_MAX_STOP_PCT}%"
        if not a1 or R < min_sd * (1 - 1e-6): return None, f"B: stop {R / a1 if a1 else float('nan'):.2f} ATR1h / {R / e * 100:.2f}% < min {setup_types.B_MIN_STOP_ATR} ATR / {setup_types.B_MIN_STOP_PCT}%"
    risk = setup_types.risk_pct(st, float(h["score"]))
    return {"setup_type": st, "risk_pct": risk, "symbol": h["symbol"], "dir": h["dir"], "score": round(float(h["score"]), 2), "entry": e, "stop": s,
            "t1": float(h["t1"]), "t2": t2, "rr": round(rr, 2), "time_rome": datetime.fromtimestamp(now, ROME).strftime("%Y-%m-%d %H:%M %Z")}, "ok"

def active_b_trades(trades_dir=None):
    """Symbols of Type B trades still working/open, from the Executor trade logs (live-*.json with setup_type B)."""
    import glob; out = []
    for f in glob.glob(os.path.join(trades_dir or TRADES, "live-*.json")):
        try: L = json.load(open(f))
        except Exception: continue
        if L.get("setup_type") != "B": continue
        st = str(L.get("status", "")).upper(); P = L.get("protection", {})
        if any(st.startswith(a) for a in B_ACTIVE) or (P.get("placed") and not P.get("flat_cleanup_done")): out.append(L.get("symbol"))
    return out

def b_alerts_today(now):
    day = datetime.fromtimestamp(now, ROME).date(); n = 0
    if os.path.exists(ALERTS):
        for line in open(ALERTS).readlines()[-2000:]:
            try: r = json.loads(line)
            except Exception: continue
            if r.get("delivered") and r.get("setup_type") == "B" and r.get("filter_ver") == FILTER_VER and datetime.fromtimestamp(r["ts_unix"], ROME).date() == day: n += 1
    return n

def btc_4h_regime(now, loader=_candles):
    """+1 bullish (4H aligned up), -1 bearish (aligned down), 0 neutral, on closed 4H bars of PF_XBTUSD."""
    C = loader("PF_XBTUSD", "4h"); C = C[C[:, 0] + TFSEC["4h"] <= now]
    if len(C) < 55: return None
    c = C[:, 4]; e20, e50 = _ema(c, 20)[-1], _ema(c, 50)[-1]
    return 1 if (e20 > e50 and c[-1] > e20) else -1 if (e20 < e50 and c[-1] < e20) else 0

def b_limits(p, now, loader=_candles, trades_dir=None, sent_b_today=None):
    """None if a qualified Type B payload may be sent, else the reason. Crypto only for the BTC regime rule."""
    from features import kind
    if kind(p["symbol"]) == "crypto" and p["symbol"] != "PF_XBTUSD":
        rg = btc_4h_regime(now, loader); d = 1 if p["dir"] == "long" else -1
        if rg is None: return "B: BTC 4H regime unknown"
        if rg == -d: return f"B: BTC 4H {'bearish' if rg < 0 else 'bullish'} vs {p['dir']}"
    act = active_b_trades(trades_dir)
    if len(act) >= B_MAX_OPEN: return f"B: {len(act)} Type B trade already active {act}"
    n = b_alerts_today(now) if sent_b_today is None else sent_b_today
    if n >= B_MAX_ALERTS_DAY: return f"B: daily cap {n}/{B_MAX_ALERTS_DAY} alerts"
    return None

def _recent(now):
    last = {}
    if os.path.exists(ALERTS):
        for line in open(ALERTS).readlines()[-2000:]:
            try: r = json.loads(line)
            except Exception: continue
            if r.get("delivered"): last[(r["symbol"], r["dir"])] = max(last.get((r["symbol"], r["dir"]), 0), r["ts_unix"])
    return last

def post(payload, opener=urllib.request.urlopen):
    url = (os.environ.get("DESK_HEAD_WEBHOOK_URL") or "").strip(); key = (os.environ.get("DESK_HEAD_WEBHOOK_KEY") or "").strip()
    if not url or not key: return None, "webhook env missing"
    if not url.startswith("https://"): return None, "webhook URL not https"
    try: pref = json.load(open(AUTHF)).get("header", "Authorization")
    except Exception: pref = "Authorization"
    order = [pref] + [x for x in ("Authorization", "X-Webhook-Key") if x != pref]
    code, note = None, ""
    for hdr in order:
        val = f"Bearer {key}" if hdr == "Authorization" else key
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", hdr: val})
        try:
            with opener(req, timeout=TIMEOUT) as r: code = r.status
        except urllib.error.HTTPError as e: code = e.code
        except Exception as e: return None, f"transport {type(e).__name__}"
        if code not in (401, 403):
            if 200 <= code < 300:
                json.dump({"header": hdr, "verified_rome": datetime.now(ROME).strftime("%F %T %Z"), "http": code}, open(AUTHF, "w"))
            return code, f"header={hdr}"
        note += f"{hdr}->{code} "
    return code, note.strip()

def process(hits, now=None, loader=_candles, sender=post, log=print):
    """Returns short status string for the scanner log line. Never raises."""
    try:
        now = now or time.time(); g = gates(now)
        if g: return f"head_alert=skip({g})"
        last = _recent(now); sent = []; nb = b_alerts_today(now)
        for h in hits:
            p, why = qualify(h, now, loader)
            if not p:
                if why.startswith("xstock outside"): sent.append(f"{h['symbol'].replace('PF_','')}:{h['dir'][0]}:skip[xstock off-session]")
                continue
            k = (p["symbol"], p["dir"])
            if now - last.get(k, 0) < DEDUP_SEC: continue
            if p["setup_type"] == "B":
                lim = b_limits(p, now, loader, sent_b_today=nb)
                if lim: sent.append(f"{p['symbol'].replace('PF_','')}:{p['dir'][0]}:skip[{lim}]"); continue
            if sender is post:   # 2026-10-27 coordination: candidate card for Zone/Quant/Executor (info only, payload unchanged, never raises)
                try: import coord_card; coord_card.write(p, now, loader)
                except Exception: pass
            code, note = sender(p)
            ok = code is not None and 200 <= code < 300
            open(ALERTS, "a").write(json.dumps({"ts_unix": int(now), **p, "http": code, "delivered": ok, "note": note, "filter_ver": FILTER_VER}) + "\n")
            if ok:
                last[k] = now
                if p["setup_type"] == "B": nb += 1
            sent.append(f"{p['symbol'].replace('PF_','')}:{p['dir'][0]}:{code}")
            if not ok: log(f"{datetime.now(ROME):%F %T} head_alert FAILED {p['symbol']} {p['dir']} http={code} {note}")
        return f"head_alert={sent or 'none'}"
    except Exception as e:
        return f"head_alert=error({type(e).__name__})"
