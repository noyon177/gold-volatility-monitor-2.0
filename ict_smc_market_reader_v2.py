import os
import json
import time
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC MARKET MOVEMENT READER  (v2 — bug-fixed)
#
# Environment variables:
#   TWELVE_DATA_API_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#
# Strategy:
#   4H bias -> 30M structure/SR -> 15M setup -> 5M confirmation
#
# Scoring (max 12):
#   Context      (max 5): HTF bias 2, 30M structure 1, P/D 1, S/R 1
#   Trigger      (max 5): sweep 2, BOS/CHoCH 1, displacement 1, OB/FVG 1
#   Confirmation (max 2): 15M EMA 1, 5M pinbar/EMA 1
#   Gates: must have sweep or structure break, and context >= MIN_CONTEXT
# ============================================================

SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3, "session_filter": True},
    {"name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 3, "session_filter": False},
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

RATE_LIMIT_WAIT = 20  # seconds to wait once if Twelve Data returns 429

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

DISPLACEMENT_BODY_RATIO = 0.65
DISPLACEMENT_ATR_MULT = 1.15
DISPLACEMENT_WINDOW = 3     # displacement may be up to N candles old

FVG_MIN_ATR = 0.10

SR_CLUSTER_ATR = 0.35
SR_MIN_TOUCHES = 2
SR_MAX_LEVELS = 3

# Liquidity sweep: look back over the last SWEEP_WINDOW candles,
# against the extreme of the SWEEP_REF_BARS candles before them.
SWEEP_WINDOW = 3
SWEEP_REF_BARS = 20
SWEEP_MIN_DEPTH_ATR = 0.02  # must poke beyond the level by at least this
SWEEP_MAX_DEPTH_ATR = 1.50  # deeper than this = real breakout, not a sweep

# BOS / CHoCH only counts if the break happened in the last N candles
BREAK_WINDOW = 3

# ---------------- SIGNAL QUALITY ----------------
MIN_SCORE = 8
MAX_SCORE = 12
MIN_CONTEXT = 3
MAX_LOCATION_ATR = 1.50

# ---------------- RISK (all in 15M ATR) ----------------
SL_BUFFER_ATR = 0.12
MIN_SL_ATR = 0.60
MAX_SL_ATR = 3.00
RISK_REWARD = 2.0

# ---------------- TIME FILTERS ----------------
# Kill zones in UTC (wide enough to cover US/EU daylight-saving shifts)
SESSIONS_UTC = [(6, 10), (12, 16)]

# Manual high-impact news blackout, UTC. Example: "2026-10-09 12:30"
NEWS_BLACKOUTS_UTC = []
NEWS_WINDOW_MIN = 30  # minutes before AND after each event

STALE_SIGNAL_MIN = 8

RUN_FOREVER = False
LOOP_SECONDS = 60

STATE_FILE = Path("ict_smc_state.json")

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/2.0"})


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


def in_session(ts):
    h = ts.hour + ts.minute / 60
    return any(start <= h < end for start, end in SESSIONS_UTC)


def in_news_blackout(ts):
    for s in NEWS_BLACKOUTS_UTC:
        event = dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        if abs((ts - event).total_seconds()) <= NEWS_WINDOW_MIN * 60:
            return True
    return False


# ============================================================
# TWELVE DATA
# ============================================================

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


def align_to(candles, interval, cutoff):
    """Keep only candles that had already closed at `cutoff`."""
    minutes = interval_minutes(interval)
    return [
        c for c in candles
        if c["dt"] + dt.timedelta(minutes=minutes) <= cutoff
    ]


def get_candles(symbol, interval, outputsize):
    require_credentials()

    data = None
    for attempt in range(2):
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

        if data.get("code") == 429 and attempt == 0:
            time.sleep(RATE_LIMIT_WAIT)
            continue
        break

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
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period

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
        lefts = candles[i - left:i]
        rights = candles[i + 1:i + right + 1]

        swing_low = (
            all(c["low"] <= x["low"] for x in lefts)
            and all(c["low"] < x["low"] for x in rights)
        )
        swing_high = (
            all(c["high"] >= x["high"] for x in lefts)
            and all(c["high"] > x["high"] for x in rights)
        )

        if swing_low:
            pivots.append({"kind": "low", "price": c["low"], "idx": i, "time": c["time"]})
        if swing_high:
            pivots.append({"kind": "high", "price": c["high"], "idx": i, "time": c["time"]})

    return pivots


def market_structure(candles):
    recent = candles[-STRUCTURE_LOOKBACK:]
    pivots = find_pivots(recent)
    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]

    empty = {
        "bias": "NEUTRAL", "bos": None, "choch": None,
        "highs": highs, "lows": lows,
        "swing_high": None, "swing_low": None,
    }
    if len(highs) < 2 or len(lows) < 2:
        return empty

    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]

    if h2["price"] > h1["price"] and l2["price"] > l1["price"]:
        structural_bias = "BULLISH"
    elif h2["price"] < h1["price"] and l2["price"] < l1["price"]:
        structural_bias = "BEARISH"
    else:
        structural_bias = "NEUTRAL"

    # FIX: BOS only counts at the moment of the break (first close beyond
    # the swing, within BREAK_WINDOW candles) — not on every candle that
    # happens to sit above/below the swing.
    closes = [c["close"] for c in recent]
    n = len(closes)
    bos = None
    if n > BREAK_WINDOW + 1:
        for i in range(n - 1, n - 1 - BREAK_WINDOW, -1):
            if closes[i] > h2["price"] and closes[i - 1] <= h2["price"]:
                bos = "BULLISH"
                break
            if closes[i] < l2["price"] and closes[i - 1] >= l2["price"]:
                bos = "BEARISH"
                break

    choch = None
    if bos == "BULLISH" and structural_bias == "BEARISH":
        choch = "BULLISH"
    elif bos == "BEARISH" and structural_bias == "BULLISH":
        choch = "BEARISH"

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

    return {"equal_high": cluster(highs), "equal_low": cluster(lows)}


def detect_liquidity_sweep(candles):
    """
    FIX: reference levels are the true extreme (lowest low / highest high)
    of the SWEEP_REF_BARS candles *before* the sweep window, and the sweep
    may have happened in any of the last SWEEP_WINDOW candles as long as
    price is still back inside the level on the latest close.
    """
    n = SWEEP_WINDOW
    if len(candles) < SWEEP_REF_BARS + n + ATR_PERIOD:
        return None

    a = atr(candles, ATR_PERIOD)[-1]
    if not a:
        return None

    ref = candles[-(SWEEP_REF_BARS + n):-n]
    ref_low = min(x["low"] for x in ref)
    ref_high = max(x["high"] for x in ref)

    levels = liquidity_levels(candles[:-n])
    low_refs = [(ref_low, "RANGE_LOW")]
    high_refs = [(ref_high, "RANGE_HIGH")]
    if levels["equal_low"]:
        low_refs.append((levels["equal_low"], "EQUAL_LOW"))
    if levels["equal_high"]:
        high_refs.append((levels["equal_high"], "EQUAL_HIGH"))

    last_close = candles[-1]["close"]
    window = candles[-n:]

    for age, c in enumerate(reversed(window)):  # age 0 = latest candle
        for price, label in low_refs:
            depth = price - c["low"]
            if (
                a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR
                and c["close"] > price
                and last_close > price
            ):
                return {
                    "side": "SELL_SIDE",
                    "price": price,
                    "level": label,
                    "age": age,
                    "strength": "STRONG" if c["close"] > c["open"] else "NORMAL",
                }

        for price, label in high_refs:
            depth = c["high"] - price
            if (
                a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR
                and c["close"] < price
                and last_close < price
            ):
                return {
                    "side": "BUY_SIDE",
                    "price": price,
                    "level": label,
                    "age": age,
                    "strength": "STRONG" if c["close"] < c["open"] else "NORMAL",
                }

    return None


# ============================================================
# DISPLACEMENT
# ============================================================

def displacement(candles):
    """FIX: checks the last DISPLACEMENT_WINDOW candles, newest first."""
    if len(candles) < ATR_PERIOD + DISPLACEMENT_WINDOW:
        return None

    a_series = atr(candles, ATR_PERIOD)

    for k in range(1, DISPLACEMENT_WINDOW + 1):
        c = candles[-k]
        a = a_series[-k]
        if not a:
            continue

        body = candle_body(c)
        ratio = body / candle_range(c)
        if ratio < DISPLACEMENT_BODY_RATIO or body < a * DISPLACEMENT_ATR_MULT:
            continue

        if c["close"] > c["open"]:
            return "BULLISH"
        if c["close"] < c["open"]:
            return "BEARISH"

    return None


# ============================================================
# FAIR VALUE GAP  (side-aware, skips mitigated gaps)
# ============================================================

def detect_fvg(candles, side=None):
    if len(candles) < 3:
        return None

    a = atr(candles, ATR_PERIOD)[-1] or 0
    if a <= 0:
        return None

    start = max(2, len(candles) - FVG_LOOKBACK)

    for i in range(len(candles) - 1, start - 1, -1):
        a1, a3 = candles[i - 2], candles[i]
        later = candles[i + 1:]

        if a3["low"] > a1["high"] and side in (None, "BULLISH"):
            gap = a3["low"] - a1["high"]
            zone_low, zone_high = a1["high"], a3["low"]
            if gap >= a * FVG_MIN_ATR and not any(x["close"] < zone_low for x in later):
                return {
                    "side": "BULLISH", "low": zone_low, "high": zone_high,
                    "index": i, "time": a3["time"], "size": gap,
                }

        if a3["high"] < a1["low"] and side in (None, "BEARISH"):
            gap = a1["low"] - a3["high"]
            zone_low, zone_high = a3["high"], a1["low"]
            if gap >= a * FVG_MIN_ATR and not any(x["close"] > zone_high for x in later):
                return {
                    "side": "BEARISH", "low": zone_low, "high": zone_high,
                    "index": i, "time": a3["time"], "size": gap,
                }

    return None


# ============================================================
# ORDER BLOCK  (side-aware, skips mitigated blocks)
# ============================================================

def detect_order_block(candles, side=None):
    if len(candles) < 10:
        return None

    a = atr(candles, ATR_PERIOD)
    start = max(2, len(candles) - OB_LOOKBACK)

    for i in range(len(candles) - 2, start - 1, -1):
        base = candles[i]
        move = candles[i + 1]
        later = candles[i + 2:]

        if not a[i + 1]:
            continue

        move_body = candle_body(move)
        move_ratio = move_body / candle_range(move)
        strong = move_ratio >= DISPLACEMENT_BODY_RATIO and move_body >= a[i + 1] * 0.90

        if (
            side in (None, "BULLISH")
            and strong
            and move["close"] > move["open"]
            and base["close"] < base["open"]
            and not any(x["close"] < base["low"] for x in later)
        ):
            return {"side": "BULLISH", "low": base["low"], "high": base["high"], "time": base["time"]}

        if (
            side in (None, "BEARISH")
            and strong
            and move["close"] < move["open"]
            and base["close"] > base["open"]
            and not any(x["close"] > base["high"] for x in later)
        ):
            return {"side": "BEARISH", "low": base["low"], "high": base["high"], "time": base["time"]}

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
        levels.append({"price": level, "touches": len(group), "strength": len(group)})

    supports = sorted([x for x in levels if x["price"] < price], key=lambda x: price - x["price"])
    resistances = sorted([x for x in levels if x["price"] > price], key=lambda x: x["price"] - price)

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
# EMA
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

    return {"bias": bias, "4h_structure": s4, "30m_structure": s30, "4h_ema": e4}


# ============================================================
# CONFLUENCE ENGINE
# ============================================================

def in_zone(price, zone, tolerance):
    if not zone:
        return False
    return zone["low"] - tolerance <= price <= zone["high"] + tolerance


def score_setup(bias_data, data30, data15, data5):
    price = data15[-1]["close"]
    a15 = atr(data15, ATR_PERIOD)[-1]

    if not a15:
        return {"signal": None, "bias": bias_data["bias"]}

    m30 = market_structure(data30)
    m15 = market_structure(data15)
    ema15_state = ema_state(data15)
    ema5_state = ema_state(data5)

    sweep = detect_liquidity_sweep(data15)
    disp = displacement(data15)
    pd = premium_discount(data30)
    pin = pinbar(data5[-1])
    sr = build_sr(data30)

    support = nearest_level(sr["support"], price)
    resistance = nearest_level(sr["resistance"], price)

    base = {
        "bias": bias_data["bias"],
        "m15": m15,
        "m30": m30,
        "ema15": ema15_state,
        "ema5": ema5_state,
        "sweep": sweep,
        "fvg": detect_fvg(data15),
        "ob": detect_order_block(data15),
        "pd": pd,
        "sr": sr,
    }

    results = []

    for side in ("BUY", "SELL"):
        wanted = "BULLISH" if side == "BUY" else "BEARISH"
        reasons = []
        conflicts = []
        context = 0
        trigger = 0
        confirm = 0

        # ---------- CONTEXT (max 5) ----------
        if bias_data["bias"] == wanted:
            context += 2
            reasons.append("HTF bias aligned")
        elif bias_data["bias"] != "RANGE":
            conflicts.append("HTF bias conflict")

        if m30["bias"] == wanted:
            context += 1
            reasons.append("30M structure aligned")
        elif m30["bias"] != "NEUTRAL":
            conflicts.append("30M structure conflict")

        if side == "BUY" and pd["zone"] == "DISCOUNT":
            context += 1
            reasons.append("discount")
        elif side == "SELL" and pd["zone"] == "PREMIUM":
            context += 1
            reasons.append("premium")
        elif pd["zone"] in ("PREMIUM", "DISCOUNT"):
            conflicts.append("poor P/D location")

        if side == "BUY" and support and abs(price - support["price"]) <= a15 * MAX_LOCATION_ATR:
            context += 1
            reasons.append("near support")
        if side == "SELL" and resistance and abs(price - resistance["price"]) <= a15 * MAX_LOCATION_ATR:
            context += 1
            reasons.append("near resistance")

        # ---------- TRIGGER (max 5) ----------
        sweep_ok = (
            sweep is not None
            and ((side == "BUY" and sweep["side"] == "SELL_SIDE")
                 or (side == "SELL" and sweep["side"] == "BUY_SIDE"))
        )
        if sweep_ok:
            trigger += 2
            reasons.append(f"{'sell' if side == 'BUY' else 'buy'}-side liquidity sweep")

        break_ok = False
        if m15["choch"] == wanted:
            trigger += 1
            break_ok = True
            reasons.append("15M CHoCH")
        elif m15["bos"] == wanted:
            trigger += 1
            break_ok = True
            reasons.append("15M BOS")

        if disp == wanted:
            trigger += 1
            reasons.append("displacement")
        elif disp:
            conflicts.append("opposite displacement")

        ob = detect_order_block(data15, wanted)
        fvg = detect_fvg(data15, wanted)
        poi = []
        if ob and in_zone(price, ob, a15 * 0.35):
            poi.append("order block")
        if fvg and in_zone(price, fvg, a15 * 0.35):
            poi.append("FVG")
        if poi:
            trigger += 1
            reasons.append(f"{wanted.lower()} " + " + ".join(poi))

        # ---------- CONFIRMATION (max 2) ----------
        if ema15_state["state"] == wanted:
            confirm += 1
            reasons.append("15M EMA 9/15 aligned")

        pin_ok = (side == "BUY" and pin == "bullish") or (side == "SELL" and pin == "bearish")
        if pin_ok or ema5_state["state"] == wanted:
            confirm += 1
            reasons.append("5M rejection candle" if pin_ok else "5M EMA 9/15 aligned")

        # ---------- GATES ----------
        if not (sweep_ok or break_ok):
            continue
        if context < MIN_CONTEXT:
            continue
        if len(conflicts) >= 2:
            continue

        total = context + trigger + confirm
        if total < MIN_SCORE:
            continue

        results.append({
            "side": side,
            "score": total,
            "context": context,
            "trigger": trigger,
            "confirm": confirm,
            "reasons": reasons,
            "conflicts": conflicts,
            "atr": a15,
            "price": price,
            "pin": pin,
            "m15_structure": m15,
            "m30_structure": m30,
            "ema15": ema15_state,
            "ema5": ema5_state,
            "sweep": sweep if sweep_ok else None,
            "fvg": fvg,
            "ob": ob,
            "pd": pd,
            "sr": sr,
        })

    if not results:
        return dict(base, signal=None)

    results.sort(key=lambda x: x["score"], reverse=True)
    if len(results) > 1 and results[0]["score"] == results[1]["score"]:
        return dict(base, signal=None, ambiguous=True, candidates=results)

    return dict(base, signal=results[0])


# ============================================================
# TRADE PLAN  (risk measured in 15M ATR — same TF as the SL)
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

    plan = dict(signal)
    plan.update({
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
        "time": data15[-1]["time"],
        "close_dt": data15[-1]["dt"] + dt.timedelta(minutes=15),
    })
    return plan


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

    sweep_text = (
        f"{sweep['side']} @ {sweep['price']:.{digits}f} ({sweep['age']} candle ago)"
        if sweep else "None"
    )
    fvg_text = fvg["side"] if fvg else "None"
    ob_text = ob["side"] if ob else "None"

    support = sr.get("support", [])
    resistance = sr.get("resistance", [])

    return (
        f"{icon} <b>{name} — {signal['side']} SETUP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧭 Market Bias: <b>{result['bias']}</b>\n"
        f"⭐ Confluence: <b>{signal['score']}/{MAX_SCORE}</b> "
        f"(ctx {signal['context']}/5 · trig {signal['trigger']}/5 · conf {signal['confirm']}/2)\n\n"

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
        f"Risk: {signal['risk']:.{digits}f} ({signal['risk_atr']:.2f} x 15M ATR)\n\n"

        f"<b>30M S/R</b>\n"
        f"Support: {level_text(support[0], digits) if support else '—'}\n"
        f"Resistance: {level_text(resistance[0], digits) if resistance else '—'}\n\n"

        f"<b>Conflicts</b>\n"
        f"{conflicts if conflicts else 'None'}\n\n"
        f"⏰ Closed 15M candle: {signal['time']} UTC\n"
        f"⚠️ This is a rule-based market-reading alert, not a guarantee of price direction."
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

    if not data15:
        raise RuntimeError("No closed 15M candles")

    # FIX: every timeframe is cut at the close of the last closed 15M
    # candle, so 5M confirmation and 15M entry refer to the same moment.
    cutoff = data15[-1]["dt"] + dt.timedelta(minutes=interval_minutes(SETUP_INTERVAL))
    data4h = align_to(data4h, BIAS_INTERVAL, cutoff)
    data30 = align_to(data30, STRUCTURE_INTERVAL, cutoff)
    data5 = align_to(data5, ENTRY_INTERVAL, cutoff)

    if min(len(data4h), len(data30), len(data15), len(data5)) < 60:
        raise RuntimeError("Not enough closed candles")

    bias_data = get_bias(data4h, data30)
    result = score_setup(bias_data, data30, data15, data5)

    signal = result.get("signal")
    if signal:
        result["signal"] = make_trade_plan(signal, data15, digits)

    return {
        "name": name,
        "price": data15[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data15[-1]["time"],
    }


# ============================================================
# MAIN
# ============================================================

def run_once():
    state = load_state()

    for config in SYMBOLS:
        name = config["name"]

        try:
            ts = now_utc()

            # Filters run BEFORE any API call, which also saves quota.
            if config.get("session_filter") and not in_session(ts):
                print(f"[{name}] outside kill zone, skipped")
                continue
            if in_news_blackout(ts):
                print(f"[{name}] news blackout, skipped")
                continue

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

            delay = (now_utc() - signal["close_dt"]).total_seconds() / 60

            if delay < 0 or delay > STALE_SIGNAL_MIN:
                print(f"[{name}] stale signal skipped: {delay:.1f}m")
                continue

            key = f"{name}:{signal['time']}:{signal['side']}"

            if state.get(name) == key:
                print(f"[{name}] duplicate skipped")
                continue

            message = build_signal_message(name, signal, result, data["digits"])
            send_telegram(message)

            state[name] = key
            save_state(state)

            print(f"[{name}] SENT {signal['side']} score={signal['score']}/{MAX_SCORE}")

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


if __name__ == "__main__":
    main()
