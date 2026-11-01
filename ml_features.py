"""
Numeric features for the ML meta-label filter.

The SAME function is used in two places, so training and live trading see
identical inputs:
  * collect_trades.py  (historical backtest -> training rows)
  * ict_smc_market_reader_v8.py  (live signal -> win probability)

No look-ahead: only candles that closed BEFORE the signal's close time are used.
"""

import math

FEATURE_COLS = [
    "score", "context", "trigger", "confirm",
    "is_buy", "is_btc",
    "htf_both", "t15_aligned", "h1_aligned",
    "pd_good",
    "sweep_age", "sweep_strong",
    "choch", "bos", "has_displacement", "has_poi_retest",
    "has_ema_pullback", "has_pinbar",
    "n_conflicts",
    "vol_ratio", "atr_rel_bp",
    "risk_atr", "cost_r", "tp2_r", "is_limit",
    "mom12", "mom48", "ema_dist", "last_body_atr", "last_range_atr",
    "range1h_atr", "range4h_atr", "pos_in_4h_range",
    "hour_utc", "weekday",
]


def _ema_last(values, period):
    if len(values) < period:
        return None
    e = sum(values[:period]) / period
    k = 2 / (period + 1)
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def build_features(plan, data5, name):
    side = plan["side"]
    buy = side == "BUY"
    sign = 1 if buy else -1
    wanted = "BULLISH" if buy else "BEARISH"

    cd = plan.get("close_dt")
    if cd is not None:
        data5 = [c for c in data5 if c["dt"] < cd]
    last = data5[-1]
    close = last["close"]
    a = plan["atr5"] or 1e-9

    closes = [c["close"] for c in data5[-300:]]
    e15 = _ema_last(closes, 15)
    ema_dist = sign * (close - e15) / a if e15 is not None else 0.0

    def back(n):
        return data5[-n - 1]["close"] if len(data5) > n else data5[0]["close"]

    mom12 = sign * (close - back(12)) / a
    mom48 = sign * (close - back(48)) / a

    rng = max(last["high"] - last["low"], 1e-12)
    last_body_atr = sign * (last["close"] - last["open"]) / a
    last_range_atr = rng / a

    w1 = data5[-12:]
    w4 = data5[-48:]
    hi1, lo1 = max(c["high"] for c in w1), min(c["low"] for c in w1)
    hi4, lo4 = max(c["high"] for c in w4), min(c["low"] for c in w4)
    range1h_atr = (hi1 - lo1) / a
    range4h_atr = (hi4 - lo4) / a
    pos4 = (close - lo4) / max(hi4 - lo4, 1e-12)

    sweep = plan.get("sweep") or {}
    pd_zone = (plan.get("pd") or {}).get("zone", "UNKNOWN")
    if (buy and pd_zone == "DISCOUNT") or ((not buy) and pd_zone == "PREMIUM"):
        pd_good = 1
    elif pd_zone in ("PREMIUM", "DISCOUNT"):
        pd_good = -1
    else:
        pd_good = 0

    m5 = plan.get("m5") or {}
    reasons = " | ".join(plan.get("reasons", []))
    t15, h1 = plan.get("t15"), plan.get("h1")

    f = {
        "score": plan["score"],
        "context": plan["context"],
        "trigger": plan["trigger"],
        "confirm": plan["confirm"],
        "is_buy": 1 if buy else 0,
        "is_btc": 1 if name == "BTCUSD" else 0,
        "htf_both": 1 if (t15 == wanted and h1 == wanted) else 0,
        "t15_aligned": 1 if t15 == wanted else 0,
        "h1_aligned": 1 if h1 == wanted else 0,
        "pd_good": pd_good,
        "sweep_age": sweep.get("age", -1) if sweep else -1,
        "sweep_strong": 1 if sweep and sweep.get("strength") == "STRONG" else 0,
        "choch": 1 if m5.get("choch") == wanted else 0,
        "bos": 1 if m5.get("bos") == wanted else 0,
        "has_displacement": 1 if "displacement" in reasons else 0,
        "has_poi_retest": 1 if "retest" in reasons else 0,
        "has_ema_pullback": 1 if "EMA pullback" in reasons else 0,
        "has_pinbar": 1 if "pinbar" in reasons else 0,
        "n_conflicts": len(plan.get("conflicts", [])),
        "vol_ratio": plan.get("vol_ratio", 1.0) or 1.0,
        "atr_rel_bp": a / close * 10000.0,
        "risk_atr": plan.get("risk_atr", 0.0),
        "cost_r": plan.get("cost_r", 0.0),
        "tp2_r": plan.get("tp2_r", 0.0),
        "is_limit": 1 if plan.get("entry_type") == "LIMIT" else 0,
        "mom12": mom12,
        "mom48": mom48,
        "ema_dist": ema_dist,
        "last_body_atr": last_body_atr,
        "last_range_atr": last_range_atr,
        "range1h_atr": range1h_atr,
        "range4h_atr": range4h_atr,
        "pos_in_4h_range": pos4,
        "hour_utc": last["dt"].hour,
        "weekday": last["dt"].weekday(),
    }
    out = {}
    for k in FEATURE_COLS:
        v = float(f[k])
        out[k] = v if math.isfinite(v) else 0.0
    return out
