import os
import json
import argparse
import time
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC MARKET MOVEMENT READER
# New architecture — credentials compatible with old bot
#
# Environment variables:
#   TWELVE_DATA_API_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#
# Strategy:
#   4H bias -> 30M structure/SR -> 15M setup -> 5M confirmation
#   Structure + liquidity + displacement + OB/FVG + P/D
#   + EMA 9/15 + pinbar + ATR risk management
# ============================================================

SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3},
    {"name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 3},
]

# ---------------- DATA ----------------
BIAS_INTERVAL = "4h"
STRUCTURE_INTERVAL = "30min"
SETUP_INTERVAL = "15min"
ENTRY_INTERVAL = "5min"

BIAS_BARS = 180
STRUCTURE_BARS = 220
SETUP_BARS = 220
ENTRY_BARS = 180

# ---------------- INDICATORS ----------------
EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14

# ---------------- ICT / SMC ----------------
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
STRUCTURE_LOOKBACK = 80
LIQUIDITY_LOOKBACK = 60
FVG_LOOKBACK = 50
OB_LOOKBACK = 60

# Minimum body/range ratio for displacement
DISPLACEMENT_BODY_RATIO = 0.65
DISPLACEMENT_ATR_MULT = 1.15

# FVG minimum size as ATR fraction
FVG_MIN_ATR = 0.10

# S/R clustering
SR_CLUSTER_ATR = 0.35
SR_MIN_TOUCHES = 2
SR_MAX_LEVELS = 3

# Signal quality
MIN_SCORE = 7
MAX_SCORE = 10

# Entry must be reasonably close to an actionable ICT location
MAX_LOCATION_ATR = 1.50
LIQUIDITY_SWEEP_ATR = 0.20

# Risk
SL_BUFFER_ATR = 0.12
MIN_SL_ATR = 0.70
MAX_SL_ATR = 2.80
RISK_REWARD = 2.0

# Only closed candles
STALE_SIGNAL_MIN = 8

# Polling is controlled by your scheduler/cron.
# If running continuously, this is the loop interval.
RUN_FOREVER = False
LOOP_SECONDS = 60

STATE_FILE = Path("ict_smc_state.json")

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/1.0"})


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():
    return dt.datetime.now(UTC)


def require_credentials():
    missing = []
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# TWELVE DATA
# ============================================================

def get_candles(symbol, interval, outputsize):
    require_credentials()

    response = SESSION.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")

    candles = []
    for row in reversed(data["values"]):
        timestamp = dt.datetime.strptime(
            row["datetime"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)

        candles.append({
            "time": row["datetime"],
            "dt": timestamp,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    return drop_unclosed(candles, interval)


def interval_minutes(interval):
    if interval.endswith("min"):
        return int(interval[:-3])
    if interval.endswith("h"):
        return int(interval[:-1]) * 60
    raise ValueError(interval)


def drop_unclosed(candles, interval):
    minutes = interval_minutes(interval)
    cutoff = now_utc()
    return [
        c for c in candles
        if c["dt"] + dt.timedelta(minutes=minutes) <= cutoff
    ]


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    out = [None] * len(values)
    out[period - 1] = sum(values[:period]) / period
    k = 2 / (period + 1)

    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr(candles, period=14):
    if len(candles) < period:
        return [None] * len(candles)

    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            tr.append(max(
                c["high"] - c["low"],
                abs(c["high"] - pc),
                abs(c["low"] - pc),
            ))

    out = [None] * len(candles)
    out[period - 1] = sum(tr[:period]) / period

    for i in range(period, len(candles)):
        out[i] = (
            out[i - 1] * (period - 1) + tr[i]
        ) / period

    return out


def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 1e-12)


def pinbar(c):
    r = candle_range(c)
    body = max(candle_body(c), r * 0.01)
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    close_pos = (c["close"] - c["low"]) / r

    if lower >= 2.0 * body and upper <= 0.4 * lower and close_pos >= 0.70:
        return "bullish"
    if upper >= 2.0 * body and lower <= 0.4 * upper and close_pos <= 0.30:
        return "bearish"
    return None


# ============================================================
# PIVOTS / MARKET STRUCTURE
# ============================================================

def find_pivots(candles, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    pivots = []

    for i in range(left, len(candles) - right):
        c = candles[i]
        lefts = candles[i-left:i]
        rights = candles[i+1:i+right+1]

        swing_low = (
            all(c["low"] <= x["low"] for x in lefts)
            and all(c["low"] < x["low"] for x in rights)
        )
        swing_high = (
            all(c["high"] >= x["high"] for x in lefts)
            and all(c["high"] > x["high"] for x in rights)
        )

        if swing_low:
            pivots.append({
                "kind": "low",
                "price": c["low"],
                "idx": i,
                "time": c["time"],
            })
        if swing_high:
            pivots.append({
                "kind": "high",
                "price": c["high"],
                "idx": i,
                "time": c["time"],
            })

    return pivots


def market_structure(candles):
    pivots = find_pivots(candles[-STRUCTURE_LOOKBACK:])
    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return {
            "bias": "NEUTRAL",
            "bos": None,
            "choch": None,
            "highs": highs,
            "lows": lows,
            "swing_high": None,
            "swing_low": None,
        }

    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]

    if h2["price"] > h1["price"] and l2["price"] > l1["price"]:
        structural_bias = "BULLISH"
    elif h2["price"] < h1["price"] and l2["price"] < l1["price"]:
        structural_bias = "BEARISH"
    else:
        structural_bias = "NEUTRAL"

    last = candles[-1]
    previous_bias = None

    # Break of the latest confirmed swing
    if last["close"] > h2["price"]:
        bos = "BULLISH"
        previous_bias = "BEARISH" if structural_bias == "BEARISH" else None
    elif last["close"] < l2["price"]:
        bos = "BEARISH"
        previous_bias = "BULLISH" if structural_bias == "BULLISH" else None
    else:
        bos = None

    choch = None
    if previous_bias and bos and previous_bias != bos:
        choch = bos

    return {
        "bias": structural_bias,
        "bos": bos,
        "choch": choch,
        "highs": highs,
        "lows": lows,
        "swing_high": h2,
        "swing_low": l2,
    }


# ============================================================
# LIQUIDITY
# ============================================================

def liquidity_levels(candles):
    pivots = find_pivots(candles[-LIQUIDITY_LOOKBACK:])
    highs = [p["price"] for p in pivots if p["kind"] == "high"]
    lows = [p["price"] for p in pivots if p["kind"] == "low"]

    # Approximate equal highs/lows by ATR-scaled tolerance.
    atr_now = atr(candles, ATR_PERIOD)[-1]
    if not atr_now:
        return {"equal_high": None, "equal_low": None}

    tolerance = atr_now * 0.20

    def cluster(values):
        if len(values) < 2:
            return None
        values = sorted(values[-8:])
        best = None
        for i in range(len(values)):
            group = [values[i]]
            for j in range(i + 1, len(values)):
                if abs(values[j] - values[i]) <= tolerance:
                    group.append(values[j])
            if len(group) >= 2 and (best is None or len(group) > len(best)):
                best = group
        return sum(best) / len(best) if best else None

    return {
        "equal_high": cluster(highs),
        "equal_low": cluster(lows),
    }


def detect_liquidity_sweep(candles):
    if len(candles) < 30:
        return None

    a = atr(candles, ATR_PERIOD)[-1]
    if not a:
        return None

    levels = liquidity_levels(candles)
    c = candles[-1]

    # Sell-side sweep: takes a prior low then closes back above it.
    low_candidates = [
        x["low"] for x in candles[-25:-1]
        if x["low"] < c["low"] + a * 0.05
    ]
    prior_low = min(low_candidates) if low_candidates else None

    if prior_low is not None:
        if (
            c["low"] < prior_low - a * 0.02
            and c["close"] > prior_low
        ):
            return {
                "side": "SELL_SIDE",
                "price": prior_low,
                "strength": "STRONG" if c["close"] > c["open"] else "NORMAL",
            }

    # Buy-side sweep: takes a prior high then closes back below it.
    high_candidates = [
        x["high"] for x in candles[-25:-1]
        if x["high"] > c["high"] - a * 0.05
    ]
    prior_high = max(high_candidates) if high_candidates else None

    if prior_high is not None:
        if (
            c["high"] > prior_high + a * 0.02
            and c["close"] < prior_high
        ):
            return {
                "side": "BUY_SIDE",
                "price": prior_high,
                "strength": "STRONG" if c["close"] < c["open"] else "NORMAL",
            }

    # Equal-liquidity contextual flag
    if levels["equal_low"] and c["low"] < levels["equal_low"] and c["close"] > levels["equal_low"]:
        return {"side": "SELL_SIDE", "price": levels["equal_low"], "strength": "EQUAL_LEVEL"}

    if levels["equal_high"] and c["high"] > levels["equal_high"] and c["close"] < levels["equal_high"]:
        return {"side": "BUY_SIDE", "price": levels["equal_high"], "strength": "EQUAL_LEVEL"}

    return None


# ============================================================
# DISPLACEMENT
# ============================================================

def displacement(candles):
    if len(candles) < ATR_PERIOD + 2:
        return None

    a = atr(candles, ATR_PERIOD)[-1]
    c = candles[-1]
    if not a:
        return None

    body = candle_body(c)
    ratio = body / candle_range(c)

    if ratio < DISPLACEMENT_BODY_RATIO or body < a * DISPLACEMENT_ATR_MULT:
        return None

    if c["close"] > c["open"]:
        return "BULLISH"
    if c["close"] < c["open"]:
        return "BEARISH"
    return None


# ============================================================
# FAIR VALUE GAP
# ============================================================

def detect_fvg(candles):
    if len(candles) < 3:
        return None

    a = atr(candles, ATR_PERIOD)[-1] or 0
    if a <= 0:
        return None

    # Search newest to oldest.
    start = max(2, len(candles) - FVG_LOOKBACK)

    for i in range(len(candles) - 1, start - 1, -1):
        a1, mid, a3 = candles[i-2], candles[i-1], candles[i]

        # Bullish FVG: third candle low > first candle high.
        if a3["low"] > a1["high"]:
            gap = a3["low"] - a1["high"]
            if gap >= a * FVG_MIN_ATR:
                return {
                    "side": "BULLISH",
                    "low": a1["high"],
                    "high": a3["low"],
                    "index": i,
                    "time": a3["time"],
                    "size": gap,
                }

        # Bearish FVG: third candle high < first candle low.
        if a3["high"] < a1["low"]:
            gap = a1["low"] - a3["high"]
            if gap >= a * FVG_MIN_ATR:
                return {
                    "side": "BEARISH",
                    "low": a3["high"],
                    "high": a1["low"],
                    "index": i,
                    "time": a3["time"],
                    "size": gap,
                }

    return None


# ============================================================
# ORDER BLOCK
# ============================================================

def detect_order_block(candles):
    if len(candles) < 10:
        return None

    a = atr(candles, ATR_PERIOD)
    start = max(2, len(candles) - OB_LOOKBACK)

    # A simple, explicit OB definition:
    # last opposite candle before a strong displacement move.
    for i in range(len(candles) - 1, start - 1, -1):
        if i + 1 >= len(candles):
            continue

        base = candles[i]
        move = candles[i + 1]

        if not a[i + 1]:
            continue

        move_body = candle_body(move)
        move_ratio = move_body / candle_range(move)

        if (
            move["close"] > move["open"]
            and base["close"] < base["open"]
            and move_ratio >= DISPLACEMENT_BODY_RATIO
            and move_body >= a[i + 1] * 0.90
        ):
            return {
                "side": "BULLISH",
                "low": base["low"],
                "high": base["high"],
                "time": base["time"],
            }

        if (
            move["close"] < move["open"]
            and base["close"] > base["open"]
            and move_ratio >= DISPLACEMENT_BODY_RATIO
            and move_body >= a[i + 1] * 0.90
        ):
            return {
                "side": "BEARISH",
                "low": base["low"],
                "high": base["high"],
                "time": base["time"],
            }

    return None


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def premium_discount(candles):
    structure = market_structure(candles)
    hi = structure["swing_high"]
    lo = structure["swing_low"]

    if not hi or not lo or hi["price"] <= lo["price"]:
        return {"zone": "UNKNOWN", "mid": None}

    mid = (hi["price"] + lo["price"]) / 2
    price = candles[-1]["close"]

    if price < mid:
        zone = "DISCOUNT"
    elif price > mid:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    return {"zone": zone, "mid": mid}


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def build_sr(candles):
    if len(candles) < 40:
        return {"support": [], "resistance": [], "atr": None}

    a = atr(candles, ATR_PERIOD)[-1]
    if not a:
        return {"support": [], "resistance": [], "atr": None}

    pivots = find_pivots(candles)
    tolerance = a * SR_CLUSTER_ATR

    clusters = []
    for p in sorted(pivots, key=lambda x: x["price"]):
        if not clusters:
            clusters.append([p])
            continue

        mean = sum(x["price"] for x in clusters[-1]) / len(clusters[-1])
        if abs(p["price"] - mean) <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    price = candles[-1]["close"]
    levels = []

    for group in clusters:
        if len(group) < SR_MIN_TOUCHES:
            continue

        level = sum(x["price"] for x in group) / len(group)
        strength = len(group)

        levels.append({
            "price": level,
            "touches": len(group),
            "strength": strength,
        })

    supports = sorted(
        [x for x in levels if x["price"] < price],
        key=lambda x: price - x["price"]
    )
    resistances = sorted(
        [x for x in levels if x["price"] > price],
        key=lambda x: x["price"] - price
    )

    return {
        "support": supports[:SR_MAX_LEVELS],
        "resistance": resistances[:SR_MAX_LEVELS],
        "atr": a,
    }


def nearest_level(levels, price):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x["price"] - price))


# ============================================================
# EMA / SETUP CONFIRMATION
# ============================================================

def ema_state(candles):
    closes = [c["close"] for c in candles]
    e9 = ema(closes, EMA_FAST)
    e15 = ema(closes, EMA_SLOW)

    if e9[-1] is None or e15[-1] is None or e9[-4] is None or e15[-4] is None:
        return {"state": "UNKNOWN", "ema9": None, "ema15": None}

    if e9[-1] > e15[-1] and e9[-1] > e9[-4] and e15[-1] > e15[-4]:
        state = "BULLISH"
    elif e9[-1] < e15[-1] and e9[-1] < e9[-4] and e15[-1] < e15[-4]:
        state = "BEARISH"
    else:
        state = "NEUTRAL"

    return {"state": state, "ema9": e9[-1], "ema15": e15[-1]}


# ============================================================
# BIAS ENGINE
# ============================================================

def get_bias(data4h, data30):
    s4 = market_structure(data4h)
    s30 = market_structure(data30)
    e4 = ema_state(data4h)

    bull = 0
    bear = 0

    for state in (s4["bias"], s30["bias"], e4["state"]):
        if state == "BULLISH":
            bull += 1
        elif state == "BEARISH":
            bear += 1

    if bull >= 2 and bull > bear:
        bias = "BULLISH"
    elif bear >= 2 and bear > bull:
        bias = "BEARISH"
    else:
        bias = "RANGE"

    return {
        "bias": bias,
        "4h_structure": s4,
        "30m_structure": s30,
        "4h_ema": e4,
    }


# ============================================================
# CONFLUENCE ENGINE
# ============================================================

def in_zone(price, zone, tolerance):
    if not zone:
        return False
    return zone["low"] - tolerance <= price <= zone["high"] + tolerance


def score_setup(bias_data, data30, data15, data5):
    price = data5[-1]["close"]
    a5 = atr(data5, ATR_PERIOD)[-1]

    if not a5:
        return None

    m30 = market_structure(data30)
    m15 = market_structure(data15)
    m5 = market_structure(data5)

    ema15_state = ema_state(data15)
    ema5_state = ema_state(data5)

    sweep = detect_liquidity_sweep(data15)
    fvg = detect_fvg(data15)
    ob = detect_order_block(data15)
    pd = premium_discount(data30)
    disp = displacement(data15)
    pin = pinbar(data5[-1])
    sr = build_sr(data30)

    support = nearest_level(sr["support"], price)
    resistance = nearest_level(sr["resistance"], price)

    # Candidate directions are evaluated separately.
    results = []

    for side in ("BUY", "SELL"):
        score = 0
        reasons = []
        conflicts = []

        wanted = "BULLISH" if side == "BUY" else "BEARISH"

        # 1. HTF bias — strongest contextual component.
        if bias_data["bias"] == wanted:
            score += 2
            reasons.append("HTF bias aligned")
        elif bias_data["bias"] != "RANGE":
            conflicts.append("HTF bias conflict")

        # 2. 30M structure.
        if m30["bias"] == wanted:
            score += 1
            reasons.append("30M structure aligned")
        elif m30["bias"] not in ("NEUTRAL", wanted):
            conflicts.append("30M structure conflict")

        # 3. BOS / CHoCH.
        if m15["bos"] == wanted:
            score += 1
            reasons.append("15M BOS")
        if m15["choch"] == wanted:
            score += 1
            reasons.append("15M CHoCH")

        # 4. Liquidity sweep.
        if side == "BUY" and sweep and sweep["side"] == "SELL_SIDE":
            score += 2
            reasons.append("sell-side liquidity sweep")
        elif side == "SELL" and sweep and sweep["side"] == "BUY_SIDE":
            score += 2
            reasons.append("buy-side liquidity sweep")

        # 5. Displacement.
        if disp == wanted:
            score += 1
            reasons.append("displacement")
        elif disp and disp != wanted:
            conflicts.append("opposite displacement")

        # 6. Order block.
        if ob and ob["side"] == wanted and in_zone(price, ob, a5 * 0.35):
            score += 1
            reasons.append(f"{wanted.lower()} order block")

        # 7. FVG.
        if fvg and fvg["side"] == wanted and in_zone(price, fvg, a5 * 0.35):
            score += 1
            reasons.append(f"{wanted.lower()} FVG")

        # 8. Premium / discount.
        if side == "BUY" and pd["zone"] == "DISCOUNT":
            score += 1
            reasons.append("discount")
        elif side == "SELL" and pd["zone"] == "PREMIUM":
            score += 1
            reasons.append("premium")
        elif pd["zone"] not in ("UNKNOWN", "EQUILIBRIUM"):
            conflicts.append("poor P/D location")

        # 9. EMA.
        if ema15_state["state"] == wanted:
            score += 1
            reasons.append("15M EMA 9/15 aligned")

        if ema5_state["state"] == wanted:
            score += 1
            reasons.append("5M EMA 9/15 aligned")

        # 10. Pinbar.
        if (side == "BUY" and pin == "bullish") or (side == "SELL" and pin == "bearish"):
            score += 1
            reasons.append("5M rejection candle")

        # 11. S/R location.
        if side == "BUY" and support:
            if abs(price - support["price"]) <= a5 * MAX_LOCATION_ATR:
                score += 1
                reasons.append("near support")
        if side == "SELL" and resistance:
            if abs(price - resistance["price"]) <= a5 * MAX_LOCATION_ATR:
                score += 1
                reasons.append("near resistance")

        # Hard conflict filter.
        if len(conflicts) >= 2:
            continue

        if score < MIN_SCORE:
            continue

        results.append({
            "side": side,
            "score": min(score, MAX_SCORE),
            "reasons": reasons,
            "conflicts": conflicts,
            "atr": a5,
            "price": price,
            "pin": pin,
            "m5_structure": m5,
            "m15_structure": m15,
            "m30_structure": m30,
            "ema15": ema15_state,
            "ema5": ema5_state,
            "sweep": sweep,
            "fvg": fvg,
            "ob": ob,
            "pd": pd,
            "sr": sr,
        })

    if not results:
        return {
            "signal": None,
            "bias": bias_data["bias"],
            "m15": m15,
            "m30": m30,
            "ema15": ema15_state,
            "ema5": ema5_state,
            "sweep": sweep,
            "fvg": fvg,
            "ob": ob,
            "pd": pd,
            "sr": sr,
        }

    # Highest score only. If tied, reject to avoid ambiguous direction.
    results.sort(key=lambda x: x["score"], reverse=True)
    if len(results) > 1 and results[0]["score"] == results[1]["score"]:
        return {
            "signal": None,
            "bias": bias_data["bias"],
            "ambiguous": True,
            "candidates": results,
        }

    return {
        "signal": results[0],
        "bias": bias_data["bias"],
        "m15": m15,
        "m30": m30,
        "ema15": ema15_state,
        "ema5": ema5_state,
        "sweep": sweep,
        "fvg": fvg,
        "ob": ob,
        "pd": pd,
        "sr": sr,
    }


# ============================================================
# TRADE PLAN
# ============================================================

def make_trade_plan(signal, data15, digits):
    entry = data15[-1]["close"]
    a = signal["atr"]

    if signal["side"] == "BUY":
        structural_low = min(c["low"] for c in data15[-6:])
        sl = structural_low - a * SL_BUFFER_ATR
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + risk * RISK_REWARD
    else:
        structural_high = max(c["high"] for c in data15[-6:])
        sl = structural_high + a * SL_BUFFER_ATR
        risk = sl - entry
        if risk <= 0:
            return None
        tp = entry - risk * RISK_REWARD

    risk_atr = risk / a
    if not (MIN_SL_ATR <= risk_atr <= MAX_SL_ATR):
        return None

    signal = dict(signal)
    signal.update({
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
        "time": data15[-1]["time"],
        "close_dt": data15[-1]["dt"] + dt.timedelta(minutes=15),
    })
    return signal


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    require_credentials()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = SESSION.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {result}")

    return True


# ============================================================
# STATE / DUPLICATE PROTECTION
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ============================================================
# MESSAGE
# ============================================================

def level_text(level, digits):
    if not level:
        return "—"
    return f"{level['price']:.{digits}f} ({level['touches']}x)"


def build_signal_message(name, signal, result, digits):
    icon = "🟢" if signal["side"] == "BUY" else "🔴"
    sr = result.get("sr", {})

    reasons = "\n".join(f"✓ {x}" for x in signal["reasons"][:12])
    conflicts = "\n".join(f"• {x}" for x in signal["conflicts"])

    sweep = signal.get("sweep")
    fvg = signal.get("fvg")
    ob = signal.get("ob")

    sweep_text = sweep["side"] if sweep else "None"
    fvg_text = fvg["side"] if fvg else "None"
    ob_text = ob["side"] if ob else "None"

    return (
        f"{icon} <b>{name} — {signal['side']} SETUP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧭 Market Bias: <b>{result['bias']}</b>\n"
        f"⭐ Confluence: <b>{signal['score']}/{MAX_SCORE}</b>\n\n"

        f"<b>ICT / SMC</b>\n"
        f"Liquidity Sweep: <b>{sweep_text}</b>\n"
        f"Order Block: <b>{ob_text}</b>\n"
        f"FVG: <b>{fvg_text}</b>\n"
        f"Premium/Discount: <b>{signal['pd']['zone']}</b>\n"
        f"15M BOS: <b>{signal['m15_structure']['bos'] or 'None'}</b>\n"
        f"15M CHoCH: <b>{signal['m15_structure']['choch'] or 'None'}</b>\n\n"

        f"<b>Confirmation</b>\n"
        f"{reasons}\n\n"

        f"<b>TRADE PLAN</b>\n"
        f"Entry: <b>{signal['entry']:.{digits}f}</b>\n"
        f"SL: <b>{signal['sl']:.{digits}f}</b>\n"
        f"TP: <b>{signal['tp']:.{digits}f}</b>\n"
        f"Risk: {signal['risk']:.{digits}f} ({signal['risk_atr']:.2f} ATR)\n\n"

        f"<b>30M S/R</b>\n"
        f"Support: {level_text(sr.get('support', [None])[0], digits) if sr.get('support') else '—'}\n"
        f"Resistance: {level_text(sr.get('resistance', [None])[0], digits) if sr.get('resistance') else '—'}\n\n"

        f"<b>Conflicts</b>\n"
        f"{conflicts if conflicts else 'None'}\n\n"
        f"⏰ Closed 15M candle: {signal['time']} UTC\n"
        f"⚠️ This is a rule-based market-reading alert, not a guarantee of price direction."
    )


def build_no_trade_message(name, result, price, digits):
    sweep = result.get("sweep")
    fvg = result.get("fvg")
    ob = result.get("ob")
    pd = result.get("pd")

    return (
        f"🟡 <b>{name} — MARKET READER</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b>\n"
        f"Bias: <b>{result.get('bias', 'UNKNOWN')}</b>\n"
        f"15M BOS: {result.get('m15', {}).get('bos') or 'None'}\n"
        f"15M CHoCH: {result.get('m15', {}).get('choch') or 'None'}\n"
        f"Liquidity Sweep: {sweep['side'] if sweep else 'None'}\n"
        f"Order Block: {ob['side'] if ob else 'None'}\n"
        f"FVG: {fvg['side'] if fvg else 'None'}\n"
        f"P/D: {pd['zone'] if pd else 'UNKNOWN'}\n\n"
        f"<b>ACTION: NO TRADE</b>\n"
        f"Confluence threshold not reached."
    )


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(config):
    name = config["name"]
    digits = config["digits"]

    data4h = get_candles(config["td_symbol"], BIAS_INTERVAL, BIAS_BARS)
    data30 = get_candles(config["td_symbol"], STRUCTURE_INTERVAL, STRUCTURE_BARS)
    data15 = get_candles(config["td_symbol"], SETUP_INTERVAL, SETUP_BARS)
    data5 = get_candles(config["td_symbol"], ENTRY_INTERVAL, ENTRY_BARS)

    if min(len(data4h), len(data30), len(data15), len(data5)) < 60:
        raise RuntimeError("Not enough closed candles")

    bias_data = get_bias(data4h, data30)
    result = score_setup(bias_data, data30, data15, data5)
    if result is None:
        raise RuntimeError("ATR unavailable - not enough data")

    signal = result.get("signal")
    if signal:
        plan = make_trade_plan(signal, data15, digits)
        if plan:
            result["signal"] = plan
        else:
            result["signal"] = None

    day = data15[-96:]  # about the last 24h of 15M candles
    return {
        "name": name,
        "price": data15[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data15[-1]["time"],
        "bias_data": bias_data,
        "day_high": max(c["high"] for c in day),
        "day_low": min(c["low"] for c in day),
        "day_open": day[0]["open"],
    }


# ============================================================
# MAIN
# ============================================================

def run_once():
    state = load_state()

    for config in SYMBOLS:
        name = config["name"]

        try:
            data = analyze_symbol(config)
            result = data["result"]
            signal = result.get("signal")

            if not signal:
                print(
                    f"[{name}] NO TRADE | "
                    f"bias={result.get('bias')} | "
                    f"price={data['price']}"
                )
                continue

            delay = (
                now_utc() - signal["close_dt"]
            ).total_seconds() / 60

            if delay < 0 or delay > STALE_SIGNAL_MIN:
                print(f"[{name}] stale signal skipped: {delay:.1f}m")
                continue

            key = f"{name}:{signal['time']}:{signal['side']}"

            if state.get(name) == key:
                print(f"[{name}] duplicate skipped")
                continue

            message = build_signal_message(
                name, signal, result, data["digits"]
            )

            send_telegram(message)

            state[name] = key
            save_state(state)

            print(
                f"[{name}] SENT {signal['side']} "
                f"score={signal['score']}/{MAX_SCORE}"
            )

        except Exception as exc:
            print(f"[{name}] ERROR: {exc}")


def main():
    require_credentials()

    if not RUN_FOREVER:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[MAIN] ERROR: {exc}")
        time.sleep(LOOP_SECONDS)



# ============================================================
# ICT + SMC MARKET READER V2 - ENHANCEMENT LAYER
# ============================================================
# This layer adds the requested operational features around the
# existing V1 signal engine. The original V1 strategy remains intact;
# these additions do not claim that the old 7/10 score is validated.

def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _env_time(name, default):
    raw = os.environ.get(name, default)
    try:
        h, m = str(raw).strip().split(":")
        return dt.time(int(h), int(m))
    except Exception:
        h, m = default.split(":")
        return dt.time(int(h), int(m))


# ---------------- SETTINGS (change here, or override with env vars) ----------------
# Killzone / session filter. OFF = a setup is sent at ANY time of day.
V2_SESSION_FILTER_ENABLED = _env_bool("KILLZONE_FILTER", False)

# Daily market report sent to Telegram.
#   DAILY_REPORT           on/off
#   DAILY_REPORT_TIME      "HH:MM" - send time, in the timezone given by DAILY_REPORT_TZ
#   DAILY_REPORT_TZ        UTC offset (hours) used ONLY to decide WHEN to send.
#                          6 = Bangladesh time (1 PM BDT = 07:00 UTC). Use 0 for 1 PM UTC.
#   DAILY_REPORT_GRACE_MIN if the bot was not running at the exact time, it still sends
#                          the report if it starts within this many minutes after it.
# Every time printed inside the report is UTC (UTC+0).
DAILY_REPORT_ENABLED = _env_bool("DAILY_REPORT", True)
DAILY_REPORT_TIME = _env_time("DAILY_REPORT_TIME", "13:00")
DAILY_REPORT_TZ_HOURS = _env_num("DAILY_REPORT_TZ", 6.0, float)
DAILY_REPORT_GRACE_MIN = _env_num("DAILY_REPORT_GRACE_MIN", 180, int)

# Loop interval for --loop mode (seconds).
V2_LOOP_SECONDS = _env_num("LOOP_SECONDS", 60, int)

V2_LONDON_START_UTC = dt.time(7, 0)
V2_LONDON_END_UTC = dt.time(10, 0)
V2_NEW_YORK_START_UTC = dt.time(13, 0)
V2_NEW_YORK_END_UTC = dt.time(16, 0)

V2_NEWS_FILTER_ENABLED = _env_bool("NEWS_FILTER", True)
V2_NEWS_BEFORE_MIN = 30
V2_NEWS_AFTER_MIN = 30
V2_NEWS_EVENTS_FILE = Path(os.environ.get("NEWS_EVENTS_FILE", "news_events.json"))

V2_SPREAD_XAUUSD = float(os.environ.get("SPREAD_XAUUSD", "0.30"))
V2_SPREAD_BTCUSD = float(os.environ.get("SPREAD_BTCUSD", "8.0"))
V2_SLIPPAGE_XAUUSD = float(os.environ.get("SLIPPAGE_XAUUSD", "0.10"))
V2_SLIPPAGE_BTCUSD = float(os.environ.get("SLIPPAGE_BTCUSD", "3.0"))

V2_TP1_R = 1.0
V2_TP1_CLOSE_PCT = 0.30
V2_TP2_R = 2.0
V2_TP2_CLOSE_PCT = 0.40
V2_TRAIL_ATR = 1.0

V2_STATE_FILE = Path(os.environ.get("ICT_SMC_STATE_FILE", "ict_smc_state_v2.json"))
V2_TRADE_LOG = Path(os.environ.get("ICT_SMC_TRADE_LOG", "ict_smc_trades_v2.jsonl"))
V2_BACKTEST_REPORT = Path(os.environ.get("ICT_SMC_BACKTEST_REPORT", "ict_smc_backtest_report.json"))

V2_API_RETRIES = 4
V2_BACKOFF = 1.5
V2_CACHE_DIR = Path(os.environ.get("ICT_SMC_CACHE_DIR", ".ict_smc_cache"))
V2_CACHE_TTL = 30


def v2_parse_time(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    x = str(value).replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(x)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def v2_session(timestamp):
    t = v2_parse_time(timestamp).astimezone(dt.timezone.utc).time()
    if not V2_SESSION_FILTER_ENABLED:
        return "ALL_SESSION"
    if V2_LONDON_START_UTC <= t < V2_LONDON_END_UTC:
        return "LONDON"
    if V2_NEW_YORK_START_UTC <= t < V2_NEW_YORK_END_UTC:
        return "NEW_YORK"
    return "OUTSIDE_KILLZONE"


def v2_load_news():
    events = []
    raw = os.environ.get("NEWS_EVENTS_JSON")
    if raw:
        try:
            events.extend(json.loads(raw))
        except Exception:
            pass
    if V2_NEWS_EVENTS_FILE.exists():
        try:
            data = json.loads(V2_NEWS_EVENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                events.extend(data)
        except Exception:
            pass
    return events


def v2_news_blocked(timestamp):
    if not V2_NEWS_FILTER_ENABLED:
        return False, ""
    t = v2_parse_time(timestamp).astimezone(dt.timezone.utc)
    for e in v2_load_news():
        try:
            if str(e.get("impact", "HIGH")).upper() != "HIGH":
                continue
            et = v2_parse_time(e["time"])
            delta = (t - et).total_seconds() / 60.0
            if -V2_NEWS_BEFORE_MIN <= delta <= V2_NEWS_AFTER_MIN:
                return True, str(e.get("name", "HIGH IMPACT NEWS"))
        except Exception:
            continue
    return False, ""


def v2_atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def v2_load_state():
    try:
        return json.loads(V2_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent_keys": [], "open_trades": [], "closed_trades": []}


def v2_save_state(state):
    state["sent_keys"] = list(dict.fromkeys(state.get("sent_keys", [])))[-500:]
    v2_atomic_json(V2_STATE_FILE, state)


def v2_log_trade(record):
    V2_TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with V2_TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def v2_costs(symbol_name):
    if symbol_name == "XAUUSD":
        return V2_SPREAD_XAUUSD, V2_SLIPPAGE_XAUUSD
    return V2_SPREAD_BTCUSD, V2_SLIPPAGE_BTCUSD


def v2_entry(symbol_name, side, price):
    spread, slip = v2_costs(symbol_name)
    return price + spread / 2 + slip if side == "BUY" else price - spread / 2 - slip


def v2_retry_get(url, params, timeout=20):
    last = None
    for attempt in range(1, V2_API_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("status") == "error":
                raise RuntimeError(data.get("message", "API error"))
            return data
        except Exception as exc:
            last = exc
            if attempt < V2_API_RETRIES:
                time.sleep(V2_BACKOFF ** (attempt - 1))
    print(f"[V2 API] failed: {last}")
    return None


def v2_quote(td_symbol):
    if not TWELVE_DATA_API_KEY:
        return None
    data = v2_retry_get(
        "https://api.twelvedata.com/quote",
        {"symbol": td_symbol, "apikey": TWELVE_DATA_API_KEY},
    )
    try:
        return float(data["close"])
    except Exception:
        return None


def fetch_time_series(td_symbol, interval, outputsize):
    """Raw Twelve Data time_series JSON (newest first), with retries."""
    if not TWELVE_DATA_API_KEY:
        return None
    return v2_retry_get(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": td_symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        },
    )


def v2_historical_sweeps(candles, lookback=80):
    """Scan the whole recent window instead of only the last 15M candle."""
    if len(candles) < 10:
        return []
    start = max(3, len(candles) - lookback)
    events = []
    for i in range(start, len(candles)):
        prior = candles[max(0, i - 12):i]
        hi = max(x["high"] for x in prior)
        lo = min(x["low"] for x in prior)
        c = candles[i]
        if c["high"] > hi and c["close"] < hi:
            events.append({"time": c["datetime"], "side": "BEARISH", "level": hi})
        if c["low"] < lo and c["close"] > lo:
            events.append({"time": c["datetime"], "side": "BULLISH", "level": lo})
    return events


def v2_zone_mitigated(zone_low, zone_high, created_time, candles):
    """True if later price has traded through the zone midpoint."""
    mid = (zone_low + zone_high) / 2.0
    created = v2_parse_time(created_time)
    for c in candles:
        if v2_parse_time(c["datetime"]) <= created:
            continue
        if c["low"] <= mid <= c["high"]:
            return True
    return False


def v2_safe_score(score_func, *args, **kwargs):
    """Prevents missing ATR/None score results from crashing a live run."""
    try:
        result = score_func(*args, **kwargs)
        return result if result is not None else {
            "score": 0, "reasons": [], "conflicts": ["SCORE_NONE"]
        }
    except (TypeError, ValueError, ZeroDivisionError):
        return {
            "score": 0, "reasons": [], "conflicts": ["SCORE_ERROR_OR_ATR_UNAVAILABLE"]
        }


def build_no_trade_message(symbol, reason):
    return f"<b>ICT + SMC V2</b>\n{symbol}: NO TRADE\nReason: {reason}"


def v2_manage_trade(trade, candles):
    """
    Outcome tracking on closed candles after the signal candle.
      TP1 = partial close (V2_TP1_CLOSE_PCT at V2_TP1_R)
      TP2 = second partial (V2_TP2_CLOSE_PCT at V2_TP2_R) -> trade is closed
      After TP1 the stop trails by V2_TRAIL_ATR * ATR (needs c["atr"]).
    Conservative: if SL and a target are touched in the same candle, SL wins.
    Note: the leftover runner (1 - TP1% - TP2%) is not counted in R.
    """
    if trade.get("status") not in ("OPEN", "TP1_HIT"):
        return trade

    buy = trade["side"] == "BUY"
    entry = float(trade["entry"])
    risk = float(trade["risk"])
    tp1 = float(trade["tp1"])
    tp2 = float(trade["tp2"])
    created = v2_parse_time(trade["candle_time"])

    for c in candles:
        if v2_parse_time(c["datetime"]) <= created:
            continue

        sl = float(trade["sl"])  # re-read: it may have been trailed
        stopped = c["low"] <= sl if buy else c["high"] >= sl
        if stopped:
            if trade["status"] == "TP1_HIT":
                remaining = 1.0 - V2_TP1_CLOSE_PCT
                stop_r = ((sl - entry) if buy else (entry - sl)) / risk
                trade["realized_R"] = trade.get("realized_R", 0.0) + remaining * stop_r
                trade["status"] = "TP1_TRAIL_STOP"
            else:
                trade["status"] = "SL"
                trade["realized_R"] = -1.0
            trade["exit_time"] = c["datetime"]
            return trade

        tp1_touched = c["high"] >= tp1 if buy else c["low"] <= tp1
        tp2_touched = c["high"] >= tp2 if buy else c["low"] <= tp2

        if trade["status"] == "OPEN" and tp1_touched:
            trade["status"] = "TP1_HIT"
            trade["tp1_time"] = c["datetime"]
            trade["realized_R"] = V2_TP1_R * V2_TP1_CLOSE_PCT

        if tp2_touched:
            trade["status"] = "TP2_HIT"
            trade["exit_time"] = c["datetime"]
            trade["realized_R"] = trade.get("realized_R", 0.0) + V2_TP2_R * V2_TP2_CLOSE_PCT
            return trade

        # Trail only after TP1.
        if trade["status"] == "TP1_HIT" and c.get("atr"):
            a = float(c["atr"])
            if buy:
                trade["sl"] = max(float(trade["sl"]), c["close"] - a * V2_TRAIL_ATR)
            else:
                trade["sl"] = min(float(trade["sl"]), c["close"] + a * V2_TRAIL_ATR)

    return trade


def v2_backtest_metrics(results):
    wins = sum(1 for x in results if x["R"] > 0)
    losses = sum(1 for x in results if x["R"] < 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in results:
        equity += x["R"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "signals": len(results),
        "wins": wins,
        "losses": losses,
        "win_rate": round(100 * wins / (wins + losses), 2) if wins + losses else 0.0,
        "net_R": round(equity, 4),
        "max_drawdown_R": round(max_dd, 4),
    }


def run_backtest_v2():
    """
    Backtest scaffold using the same market concepts, with explicit
    execution assumptions. It does NOT claim 7/10 is validated until
    the report contains real score/outcome samples.
    """
    reports = []
    for sym in SYMBOLS:
        try:
            # Use existing fetcher if available. This keeps API interface compatible.
            raw15 = fetch_time_series(sym["td_symbol"], "15min", 2500)
            if not raw15 or "values" not in raw15:
                reports.append({"symbol": sym["name"], "error": "No 15M data"})
                continue

            candles = []
            for x in reversed(raw15["values"]):
                candles.append({
                    "datetime": v2_parse_time(x["datetime"]),
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"]),
                })

            results = []
            for i in range(60, len(candles)):
                c = candles[i]
                session = v2_session(c["datetime"])
                if V2_SESSION_FILTER_ENABLED and session == "OUTSIDE_KILLZONE":
                    continue
                blocked, _ = v2_news_blocked(c["datetime"])
                if blocked:
                    continue

                sweeps = v2_historical_sweeps(candles[:i + 1])
                if not sweeps:
                    continue

                sweep = sweeps[-1]
                side = "BUY" if sweep["side"] == "BULLISH" else "SELL"

                ranges = [x["high"] - x["low"] for x in candles[max(0, i-13):i+1]]
                atr = sum(ranges) / len(ranges) if ranges else 0
                if atr <= 0:
                    continue

                entry = v2_entry(sym["name"], side, c["close"])
                if side == "BUY":
                    sl = min(x["low"] for x in candles[max(0, i-5):i+1]) - atr * 0.12
                    risk = entry - sl
                    tp1 = entry + risk * V2_TP1_R
                    tp2 = entry + risk * V2_TP2_R
                else:
                    sl = max(x["high"] for x in candles[max(0, i-5):i+1]) + atr * 0.12
                    risk = sl - entry
                    tp1 = entry - risk * V2_TP1_R
                    tp2 = entry - risk * V2_TP2_R

                if risk <= 0 or risk / atr < 0.70 or risk / atr > 2.80:
                    continue

                outcome = "TIMEOUT"
                realized = 0.0
                tp1_hit = False
                for f in candles[i + 1:i + 97]:  # <= 24h of 15M candles
                    if side == "BUY":
                        if f["low"] <= sl:
                            outcome, realized = "SL", -1.0
                            break
                        if not tp1_hit and f["high"] >= tp1:
                            tp1_hit = True
                            realized += V2_TP1_R * V2_TP1_CLOSE_PCT
                        if f["high"] >= tp2:
                            outcome = "TP2"
                            realized += V2_TP2_R * V2_TP2_CLOSE_PCT
                            break
                    else:
                        if f["high"] >= sl:
                            outcome, realized = "SL", -1.0
                            break
                        if not tp1_hit and f["low"] <= tp1:
                            tp1_hit = True
                            realized += V2_TP1_R * V2_TP1_CLOSE_PCT
                        if f["low"] <= tp2:
                            outcome = "TP2"
                            realized += V2_TP2_R * V2_TP2_CLOSE_PCT
                            break

                results.append({
                    "time": c["datetime"].isoformat(),
                    "side": side,
                    "score": None,
                    "outcome": outcome,
                    "R": realized,
                })

            report = {
                "symbol": sym["name"],
                "metrics": v2_backtest_metrics(results),
                "score_validation": {
                    "status": "NOT_PROVEN",
                    "message": "The full signal engine must be backtested before 7/10 is treated as statistically meaningful."
                },
                "execution_assumptions": {
                    "spread": v2_costs(sym["name"])[0],
                    "slippage": v2_costs(sym["name"])[1],
                    "TP1_R": V2_TP1_R,
                    "TP1_close_pct": V2_TP1_CLOSE_PCT,
                    "TP2_R": V2_TP2_R,
                    "TP2_close_pct": V2_TP2_CLOSE_PCT,
                },
                "results": results,
            }
            reports.append(report)
        except Exception as exc:
            reports.append({"symbol": sym["name"], "error": str(exc)})

    v2_atomic_json(
        V2_BACKTEST_REPORT,
        {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "reports": reports}
    )
    print(json.dumps(reports, indent=2, default=str))


V2_OPEN_STATES = ("OPEN", "TP1_HIT")
V2_TRADE_TIMEOUT_HOURS = 24


def v2_management_candles(td_symbol):
    """Closed 5M candles with ATR, in the shape v2_manage_trade expects."""
    raw = get_candles(td_symbol, ENTRY_INTERVAL, 300)
    atrs = atr(raw, ATR_PERIOD)
    return [
        {
            "datetime": c["dt"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "atr": a,
        }
        for c, a in zip(raw, atrs)
    ]


def v2_update_open_trades(state):
    now = now_utc()

    for sym in SYMBOLS:
        trades = [
            t for t in state.get("open_trades", [])
            if t["symbol"] == sym["name"] and t.get("status") in V2_OPEN_STATES
        ]
        if not trades:
            continue

        try:
            candles = v2_management_candles(sym["td_symbol"])
        except Exception as exc:
            print(f"[V2] {sym['name']} trade update failed: {exc}")
            continue

        for trade in trades:
            v2_manage_trade(trade, candles)

            age_h = (now - v2_parse_time(trade["created_at"])).total_seconds() / 3600
            if trade["status"] in V2_OPEN_STATES and age_h > V2_TRADE_TIMEOUT_HOURS:
                trade["status"] = "TIMEOUT"
                trade["exit_time"] = now.isoformat()
                trade.setdefault("realized_R", 0.0)

            if trade["status"] not in V2_OPEN_STATES:
                v2_log_trade({"event": "TRADE_CLOSED", **trade})
                print(
                    f"[V2] {trade['symbol']} {trade['side']} closed: "
                    f"{trade['status']} R={trade.get('realized_R', 0):.2f}"
                )

    still_open = [t for t in state.get("open_trades", []) if t.get("status") in V2_OPEN_STATES]
    closed_now = [t for t in state.get("open_trades", []) if t.get("status") not in V2_OPEN_STATES]
    state["open_trades"] = still_open
    state["closed_trades"] = (state.get("closed_trades", []) + closed_now)[-500:]


# ============================================================
# DAILY MARKET REPORT
# ============================================================

def _px(x, d):
    return "—" if x is None else f"{x:.{d}f}"


def _zone_text(z, d):
    if not z:
        return "None"
    return f"{z['side']} {_px(z['low'], d)}–{_px(z['high'], d)} ({z['time']} UTC)"


def v2_report_due(state, now):
    """Returns (due, key). One report per day, sent once the scheduled time has passed."""
    if not DAILY_REPORT_ENABLED:
        return False, None

    tz = dt.timezone(dt.timedelta(hours=DAILY_REPORT_TZ_HOURS))
    local_now = now.astimezone(tz)
    scheduled = dt.datetime.combine(local_now.date(), DAILY_REPORT_TIME, tzinfo=tz)

    if local_now < scheduled:
        return False, None
    if (local_now - scheduled).total_seconds() / 60 > DAILY_REPORT_GRACE_MIN:
        return False, None

    key = scheduled.date().isoformat()
    if state.get("last_report_date") == key:
        return False, None
    return True, key


def build_daily_report(snaps, state, now):
    lines = [
        "📊 <b>DAILY MARKET REPORT</b>",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M')} UTC (UTC+0)",
        "━━━━━━━━━━━━━━━━",
    ]

    for sym in SYMBOLS:
        name, d = sym["name"], sym["digits"]
        data = snaps.get(name)
        if not data:
            lines.append(f"<b>{name}</b>: data unavailable\n")
            continue

        r = data["result"]
        bd = data["bias_data"]
        price = data["price"]
        chg = (price - data["day_open"]) / data["day_open"] * 100 if data["day_open"] else 0.0
        m15 = r.get("m15") or {}
        pd = r.get("pd") or {}
        sr = r.get("sr") or {}
        sweep = r.get("sweep")
        sup = (sr.get("support") or [None])[0]
        res = (sr.get("resistance") or [None])[0]

        signal = r.get("signal")
        if signal:
            status = f"🚨 SETUP ACTIVE: {signal['side']} ({signal['score']}/{MAX_SCORE})"
        elif r.get("ambiguous"):
            status = "⚪ Conflicting setups (no clear direction)"
        else:
            status = "⚪ No valid setup right now"

        lines += [
            f"<b>{name}</b>  {_px(price, d)}  ({chg:+.2f}% / ~24H)",
            f"24H range: {_px(data['day_low'], d)} – {_px(data['day_high'], d)}",
            f"HTF Bias: <b>{r.get('bias', 'UNKNOWN')}</b>",
            f"4H structure: {bd['4h_structure']['bias']} | 4H EMA: {bd['4h_ema']['state']}",
            f"30M structure: {bd['30m_structure']['bias']}",
            f"15M BOS: {m15.get('bos') or 'None'} | CHoCH: {m15.get('choch') or 'None'}",
            f"Premium/Discount: {pd.get('zone', 'UNKNOWN')} (mid {_px(pd.get('mid'), d)})",
            f"Liquidity sweep: {sweep['side'] + ' @ ' + _px(sweep['price'], d) if sweep else 'None'}",
            f"Order Block: {_zone_text(r.get('ob'), d)}",
            f"FVG: {_zone_text(r.get('fvg'), d)}",
            f"Support: {_px(sup['price'], d) if sup else '—'} | Resistance: {_px(res['price'], d) if res else '—'}",
            status,
            "",
        ]

    # Trades being tracked + last 24h results
    try:
        open_trades = state.get("open_trades", [])
        cutoff = now - dt.timedelta(hours=24)
        recent = [
            t for t in state.get("closed_trades", [])
            if t.get("exit_time") and v2_parse_time(t["exit_time"]) >= cutoff
        ]
        net_r = sum(float(t.get("realized_R", 0.0)) for t in recent)
        lines.append("<b>Trades</b>")
        if open_trades:
            for t in open_trades:
                lines.append(
                    f"• OPEN {t['symbol']} {t['side']} [{t['status']}] "
                    f"entry {t['entry']:.3f} (signal {t['candle_time']} UTC)"
                )
        else:
            lines.append("• No open tracked trades")
        lines.append(f"• Closed in last 24h: {len(recent)} | Net: {net_r:+.2f}R")
        lines.append("")
    except Exception as exc:
        print(f"[REPORT] trades section failed: {exc}")

    # High-impact news in the next 24h
    try:
        horizon = now + dt.timedelta(hours=24)
        events = []
        for e in v2_load_news():
            if str(e.get("impact", "HIGH")).upper() != "HIGH":
                continue
            et = v2_parse_time(e["time"]).astimezone(UTC)
            if now <= et <= horizon:
                events.append((et, str(e.get("name", "High impact news"))))
        if events:
            lines.append("<b>High-impact news (next 24h, UTC)</b>")
            for et, nm in sorted(events):
                lines.append(f"• {et.strftime('%m-%d %H:%M')} UTC — {nm}")
            lines.append("")
    except Exception as exc:
        print(f"[REPORT] news section failed: {exc}")

    lines.append("⚠️ Rule-based market reading, not a guarantee of price direction.")
    return "\n".join(lines)


def v2_maybe_send_daily_report(state, snaps, force=False):
    now = now_utc()
    if force:
        due, key = True, None
    else:
        due, key = v2_report_due(state, now)
    if not due:
        return

    # Reuse the scan data; fetch only what is missing.
    for sym in SYMBOLS:
        if sym["name"] not in snaps:
            try:
                snaps[sym["name"]] = analyze_symbol(sym)
            except Exception as exc:
                print(f"[REPORT] {sym['name']} failed: {exc}")

    if not any(sym["name"] in snaps for sym in SYMBOLS):
        print("[REPORT] no data available, will retry on next run")
        return

    try:
        send_telegram(build_daily_report(snaps, state, now))
    except Exception as exc:
        print(f"[REPORT] send failed: {exc}")
        return

    if key:
        state["last_report_date"] = key
    v2_save_state(state)
    print("[REPORT] daily report sent")


# ============================================================
# MAIN RUN
# ============================================================

def run_v2_once(force_report=False):
    """
    V1 engine finds the setup; V2 adds news filter (and the optional killzone
    filter, OFF by default), execution costs, duplicate protection, Telegram
    delivery, outcome tracking and the daily report.
    One symbol failing does not stop the other.
    """
    require_credentials()
    state = v2_load_state()

    # 1) Update outcomes of trades signalled earlier.
    try:
        v2_update_open_trades(state)
    except Exception as exc:
        print(f"[V2] trade update error: {exc}")

    # 2) Filters
    now = now_utc()
    session = v2_session(now)
    skip_reason = None

    if V2_SESSION_FILTER_ENABLED and session == "OUTSIDE_KILLZONE":
        skip_reason = "OUTSIDE_KILLZONE"
    else:
        blocked, news = v2_news_blocked(now)
        if blocked:
            skip_reason = f"NEWS_BLACKOUT: {news}"

    snaps = {}

    if skip_reason:
        print(build_no_trade_message("ALL", skip_reason))
    else:
        # 3) Scan
        for sym in SYMBOLS:
            name = sym["name"]
            try:
                data = analyze_symbol(sym)
                snaps[name] = data
                result = data["result"]
                signal = result.get("signal")

                if not signal:
                    print(
                        f"[{name}] NO TRADE | bias={result.get('bias')} | "
                        f"price={data['price']}"
                    )
                    continue

                delay = (now_utc() - signal["close_dt"]).total_seconds() / 60
                if delay < 0 or delay > STALE_SIGNAL_MIN:
                    print(f"[{name}] stale signal skipped: {delay:.1f}m")
                    continue

                side = signal["side"]
                key = f"{name}|{signal['time']}|{side}"
                if key in state.get("sent_keys", []):
                    print(f"[{name}] duplicate skipped")
                    continue

                entry = v2_entry(name, side, float(signal["entry"]))
                sl = float(signal["sl"])
                risk = abs(entry - sl)
                if risk <= 0:
                    print(f"[{name}] invalid risk, skipped")
                    continue

                direction = 1 if side == "BUY" else -1
                tp1 = entry + direction * risk * V2_TP1_R
                tp2 = entry + direction * risk * V2_TP2_R
                d = data["digits"]

                message = build_signal_message(name, signal, result, d) + (
                    f"\n\n<b>V2 EXECUTION PLAN</b>\n"
                    f"Entry (spread+slippage): <b>{entry:.{d}f}</b>\n"
                    f"SL: <b>{sl:.{d}f}</b>\n"
                    f"TP1 ({V2_TP1_R:g}R, {int(V2_TP1_CLOSE_PCT * 100)}%): <b>{tp1:.{d}f}</b>\n"
                    f"TP2 ({V2_TP2_R:g}R, {int(V2_TP2_CLOSE_PCT * 100)}%): <b>{tp2:.{d}f}</b>"
                )
                if V2_SESSION_FILTER_ENABLED:
                    message += f"\nSession: {session}"
                send_telegram(message)

                trade = {
                    "symbol": name,
                    "side": side,
                    "score": signal.get("score"),
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "risk": risk,
                    "candle_time": signal["time"],
                    "created_at": now_utc().isoformat(),
                    "session": session,
                    "status": "OPEN",
                }
                state.setdefault("sent_keys", []).append(key)
                state.setdefault("open_trades", []).append(trade)
                v2_log_trade({"event": "SIGNAL_SENT", **trade})
                v2_save_state(state)  # save right after sending -> no duplicate on crash
                print(f"[{name}] SENT {side} score={signal.get('score')}/{MAX_SCORE}")

            except Exception as exc:
                print(f"[V2] {name} failed: {exc}")

    # 4) Daily report (also runs when the scan was skipped by a filter)
    try:
        v2_maybe_send_daily_report(state, snaps, force=force_report)
    except Exception as exc:
        print(f"[REPORT] error: {exc}")

    v2_save_state(state)


def v2_main():
    parser = argparse.ArgumentParser(description="ICT + SMC Market Reader V2")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="send the daily market report now (ignores schedule)")
    args = parser.parse_args()

    if args.backtest:
        run_backtest_v2()
    elif args.report:
        require_credentials()
        state = v2_load_state()
        v2_maybe_send_daily_report(state, {}, force=True)
    elif args.loop:
        while True:
            try:
                run_v2_once()
            except Exception as exc:
                print(f"[MAIN] ERROR: {exc}")
            time.sleep(V2_LOOP_SECONDS)
    else:
        run_v2_once()


if __name__ == "__main__":
    v2_main()
