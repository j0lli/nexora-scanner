import os as _os, sys as _sys; DESK_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "desk")  # cloud bundle path
"""2026-10-27: the 26/10a bounce pre-filter (R1/R2) and the net-of-fees R:R gate were REMOVED on the owner's instruction
(27/10 08:51). This suite now checks that the active entry filter is the pre-26/10 one (FILTER_VER 2026-10-27a = 2026-10-23d
logic). The old 31 bounce-filter tests are archived in desk/backup_2026-10-27_revert_filter/test_bounce_filter.py."""
import sys, os, inspect
sys.dont_write_bytecode = True; sys.path.insert(0, DESK_DIR)
os.environ.setdefault("DESK_HEAD_WEBHOOK_URL", "http://127.0.0.1:9/x"); os.environ.setdefault("DESK_HEAD_WEBHOOK_KEY", "x")
import setup_types as S, head_alert as H
N = 0
def ok(c, m):
    global N; N += 1; print(("PASS " if c else "FAIL ") + m); c or sys.exit(1)
ok(H.FILTER_VER == "2026-10-27a", f"FILTER_VER {H.FILTER_VER}")
ok(not hasattr(S, "bounce_unrejected") and not hasattr(S, "swings_4h") and not hasattr(S, "B_FEE_RT"), "setup_types: bounce pre-filter + B_FEE_RT removed")
src_s, src_h = inspect.getsource(S), inspect.getsource(H)
ok("rr_ok=rr >= MIN_RR" in src_s and "rr_net" not in src_s and "fee_r" not in src_s, "setup_types: Type B R:R gate is GROSS rr >= 1.5")
ok("1.5 <= d * (x - entry) / R <= 3.0" in src_s, "setup_types: T2 swing window gross 1.5R..3R (pre-26/10)")
code_h = "\n".join(l.split("#")[0] for l in src_h.splitlines())
ok("bounce_unrejected" not in code_h and "rr_net" not in code_h and "e20_4h" not in code_h and "B_FEE_RT" not in code_h, "head_alert: no bounce / net-R:R checks")
ok(H.B_MAX_ALERTS_DAY == 10**6 and H.B_MAX_OPEN == 1, "kept: no daily Type B alert cap, max 1 open Type B")
ok(H.STOP_LO == -4.0 and H.STOP_HI == 1.6, "kept: alert gate -4% .. +1.6% day close")
ok(S.B_MIN_STOP_ATR == 1.0, "kept: Type B stop floor 1 ATR1h")
print(f"ALL {N} BOUNCE_FILTER (REVERT) TESTS PASSED")
