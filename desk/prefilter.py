"""NEXORA scanner PRE-FILTER (2026-10-28, research build; NOT live until the owner sees the backtest).
Drops scanner hits BEFORE head_alert.process() sends them. Desk entry rules (setup_types.py / head_alert.py /
scanner.py) are NOT changed: this module is called from a wrapper (desk/scanner_pf.py on the box,
cloud/cloud_scan.py in the cloud repo) that wraps head_alert.process.

Checks (only hits with setup_type A or B are judged; anything else passes through untouched, head_alert ignores it):
 F1 STRUCT  structural stop. On CLOSED 4H bars of the last SWING_LOOKBACK_4H bars (30 = 5 days):
            short -> reference = max( most recent confirmed 4H pivot high ABOVE entry (fractal, SWING_K=2 bars each
                     side), highest high of the last SWING_K closed 4H bars if above entry [unconfirmed fresh swing] ).
            long  -> mirror with pivot lows / lowest low below entry.
            stop must be strictly beyond the reference (short: stop > ref, long: stop < ref) -> else DROP
            ("stop inside 4H structure"). No reference on the stop side within the lookback -> pass.
 F2 COUNTER 1H counter-move. short: DROP if the last CLOSED 1H bar's high > highest high of the 6 closed bars before
            it, or if the current (forming) 1H bar's high > highest high of the last 6 closed bars; long mirror (lows).
            Also DROP if the live price is already on the adverse side of the entry by more than 0.1R (short:
            mark > entry + 0.1R, long: mark < entry - 0.1R); mark = h['mark'] / h['last'] (scanner field) or the mark argument.
 F3 TIME    no alerts from 22:30 Europe/Rome (5-min verdict + 20-min entry window must finish before 23:00).
Modes, per check group (2026-10-28 owner t64u: F3 real block, F1/F2 shadow):
  env DESK_PREFILTER     -> F1+F2 mode, default "shadow" (log verdict only, never drop)
  env DESK_PREFILTER_F3  -> F3 mode,    default "on"     (real block)
  each: "on" = drop on failure, "shadow" = log only, "off" = not evaluated.
A hit is dropped only if a check whose mode is "on" fails.
Log: desk/state/prefilter_shadow.jsonl (one row per judged hit with per-check F1/F2/F3 verdicts, modes and whether
it was dropped; no secrets). Never raises into the scanner."""
import json, os, time
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np

PF_VER = "2026-10-28a"
ROME = ZoneInfo("Europe/Rome")
SWING_K, SWING_LOOKBACK_4H, NEWEXT_N = 2, 30, 6
SWING_MAX_PCT = 3.0    # only swings within 3% of entry count (= desk max stop, features.MAX_STOP_PCT): a swing we could never put a stop beyond is not 'the' structure
MARK_TOL_R = 0.1       # adverse-mark tolerance: 0.1R (ticks/spread noise)
CUTOFF_HM = (22, 30)
TF = {"1h": 3600, "4h": 14400}
DESK = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DESK, "state", "prefilter_shadow.jsonl")
MODES = ("on", "shadow", "off")
DEFAULT_MODE, DEFAULT_MODE_F3 = "shadow", "on"


def _dir(h): return 1 if h["dir"] == "long" else -1


def _closed(C, tf, now):
    C = np.asarray(C, dtype=float)
    return C[C[:, 0] + TF[tf] <= now]


def _forming(C, tf, now):
    C = np.asarray(C, dtype=float)
    f = C[(C[:, 0] <= now) & (C[:, 0] + TF[tf] > now)]
    return f[-1] if len(f) else None


def struct_ref(C4, d, entry, k=None, lookback=None, max_pct=None):
    """Reference 4H swing on the STOP side of entry (closed bars only). Returns (ref or None, info)."""
    k = SWING_K if k is None else k; lookback = SWING_LOOKBACK_4H if lookback is None else lookback
    if len(C4) < 2 * k + 2: return None, {"note": "not enough 4H bars"}
    X = C4[:, 2] if d < 0 else C4[:, 3]            # short: highs, long: lows
    n = len(X); lo = max(k, n - lookback)
    mp = SWING_MAX_PCT if max_pct is None else max_pct
    beyond = lambda v: 0 < -d * (v - entry) and abs(v - entry) / entry * 100 <= mp   # stop side, within mp %
    piv = None
    for p in range(n - 1 - k, lo - 1, -1):          # newest confirmed pivot first
        w = X[p - k:p + k + 1]; v = float(X[p])
        if v == (w.max() if d < 0 else w.min()) and beyond(v):
            piv = (n - 1 - p, v); break
    tail = X[n - k:]; tv = float(tail.max() if d < 0 else tail.min())
    cands = [c for c in ((piv[1] if piv else None), (tv if beyond(tv) else None)) if c is not None]
    if not cands: return None, {"pivot": None, "tail": tv}
    ref = max(cands) if d < 0 else min(cands)
    return ref, {"pivot": piv[1] if piv else None, "pivot_age_4h": piv[0] if piv else None, "tail": tv, "ref": ref}


def check_struct(h, now, loader):
    d = _dir(h); e, s = float(h["entry"]), float(h["stop"])
    C4 = _closed(loader(h["symbol"], "4h"), "4h", now)
    ref, info = struct_ref(C4, d, e)
    if ref is None: return True, {"struct": "no 4H swing on stop side in lookback", **info}
    ok = d * (ref - s) > 0                          # short: s > ref ; long: s < ref
    return bool(ok), {**info, "stop": s, "struct": "ok" if ok else f"stop {s:.6g} inside 4H structure (ref {ref:.6g})"}


def check_counter(h, now, loader, mark=None):
    d = _dir(h); e = float(h["entry"])
    A = loader(h["symbol"], "1h"); C = _closed(A, "1h", now); f = _forming(A, "1h", now)
    info = {}
    if len(C) >= NEWEXT_N + 1:
        X = C[:, 2] if d < 0 else C[:, 3]
        prev = X[-NEWEXT_N - 1:-1]; ref = prev.max() if d < 0 else prev.min()
        if d * (ref - X[-1]) > 0:                    # short: last high > ref (long: last low < ref)
            return False, {"counter": f"last closed 1H bar new {'high' if d < 0 else 'low'} {X[-1]:.6g} beyond prev {NEWEXT_N} {ref:.6g}"}
        if f is not None:
            fx = f[2] if d < 0 else f[3]; last6 = X[-NEWEXT_N:]; r2 = last6.max() if d < 0 else last6.min()
            if d * (r2 - fx) > 0:
                return False, {"counter": f"forming 1H bar new {'high' if d < 0 else 'low'} {fx:.6g} beyond last {NEWEXT_N} {r2:.6g}"}
    m = mark if mark is not None else h.get("mark", h.get("last"))
    tol = MARK_TOL_R * abs(e - float(h["stop"]))
    if m is not None and d * (float(m) - e) < -tol:  # short: mark > entry + 0.1R (long mirror)
        return False, {"counter": f"mark {float(m):.6g} already adverse vs entry {e:.6g}"}
    return True, {"counter": "ok"}


def check_time(now):
    t = datetime.fromtimestamp(now, ROME)
    ok = (t.hour, t.minute) < CUTOFF_HM
    return ok, {"time": "ok" if ok else f"{t:%H:%M} >= {CUTOFF_HM[0]}:{CUTOFF_HM[1]:02d} Rome cutoff"}


def judge(h, now, loader, mark=None):
    """Returns {'keep': bool, 'fail': [F1/F2/F3...], 'info': {...}}. Errors in a check -> that check passes (fail-open
    keeps today's behaviour) and is reported in info['errors']."""
    fail, info, errs = [], {}, []
    for name, fn in (("F3_time", lambda: check_time(now)), ("F1_struct", lambda: check_struct(h, now, loader)),
                     ("F2_counter", lambda: check_counter(h, now, loader, mark))):
        try:
            ok, i = fn(); info.update(i)
            if not ok: fail.append(name)
        except Exception as ex:
            errs.append(f"{name}: {type(ex).__name__}: {ex}")
    if errs: info["errors"] = errs
    return {"keep": not fail, "fail": fail, "info": info}


def _mode(v, default):
    v = (v or "").strip().lower()
    return v if v in MODES else default


def env_modes():
    """(F1/F2 mode, F3 mode) from the environment; unknown values fall back to the defaults (shadow, on)."""
    return _mode(os.environ.get("DESK_PREFILTER"), DEFAULT_MODE), _mode(os.environ.get("DESK_PREFILTER_F3"), DEFAULT_MODE_F3)


def filter_hits(hits, now, loader, mode=None, log_path=LOG, mode_f3=None, log=None):
    """Returns the hits to pass on to head_alert.process. Non A/B hits are passed untouched.
    mode = F1/F2 mode, mode_f3 = F3 mode (None -> env/defaults). log(str) optional extra sink (e.g. print in cloud)."""
    em, ef3 = env_modes()
    m12 = _mode(mode, em) if mode is not None else em
    m3 = _mode(mode_f3, ef3) if mode_f3 is not None else (m12 if mode == "off" else ef3)
    if m12 == "off" and m3 == "off": return list(hits)
    out, rows = [], []
    for h in hits:
        if h.get("setup_type") not in ("A", "B"): out.append(h); continue
        j = judge(h, now, loader)
        mode_of = {"F1_struct": m12, "F2_counter": m12, "F3_time": m3}
        verdict = {k: ("off" if mode_of[k] == "off" else ("FAIL" if k in j["fail"] else "pass")) for k in mode_of}
        drop_by = [k for k in j["fail"] if mode_of[k] == "on"]
        rows.append({"ts_unix": int(now), "time_rome": datetime.fromtimestamp(now, ROME).strftime("%F %H:%M"), "pf_ver": PF_VER,
                     "mode_f12": m12, "mode_f3": m3, "symbol": h["symbol"], "dir": h["dir"], "setup_type": h.get("setup_type"),
                     "score": h.get("score"), "entry": h.get("entry"), "stop": h.get("stop"), "verdict": verdict,
                     "dropped": bool(drop_by), "dropped_by": drop_by, "would_drop_if_all_on": not j["keep"], "info": j["info"]})
        if not drop_by: out.append(h)
    if rows and log:
        for r in rows:
            try: log(f"prefilter {r['symbol'].replace('PF_', '')}:{r['dir'][0]} F1={r['verdict']['F1_struct']} F2={r['verdict']['F2_counter']} "
                     f"F3={r['verdict']['F3_time']} {'DROPPED' if r['dropped'] else 'passed'} (F1/F2 {r['mode_f12']}, F3 {r['mode_f3']})")
            except Exception: pass
    if log_path and rows:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as f:
                for r in rows: f.write(json.dumps(r, default=float) + "\n")
        except Exception: pass
    return out


def install(H, mode=None, mode_f3=None, log_path=None, log=None):
    """Wrap head_alert-like module H so H.process(hits, ...) sees only pre-filtered hits. Idempotent. Never raises.
    Defaults (env or built-in): F1/F2 shadow, F3 on."""
    em, ef3 = env_modes()
    m12 = _mode(mode, em) if mode is not None else em
    m3 = _mode(mode_f3, ef3) if mode_f3 is not None else ef3
    if getattr(H.process, "_prefilter", False): return H.process
    orig = H.process

    def process(hits, now=None, loader=None, *a, **k):
        now = now or time.time(); ld = loader or H._candles
        try: hits = filter_hits(hits, now, ld, m12, (log_path if log_path is not None else LOG), m3, log)
        except Exception: pass
        return orig(hits, now, ld, *a, **k)
    process._prefilter = True; process._mode = m12; process._mode_f3 = m3
    H.process = process
    return process
