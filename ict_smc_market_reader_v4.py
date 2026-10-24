import os
import sys
import json
import time
import html
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC MARKET MOVEMENT READER  (v4.1 — SCALP mode + trade tracking)
#
# v4.1: remembers every sent signal, reports TP1 / TP2 / SL / breakeven /
#       timeout on Telegram, plus a daily summary and a weekly report.
#
# v4 changes vs v3:
#   * MODE=SCALP (default): 5M trigger, 15M + 1H trend, ~1 API call per
#     symbol per 5 min (15M/1H are built locally from 5M candles).
#   * MODE=SWING keeps the old v3 logic (4H -> 30M -> 15M -> 5M).
#   * SCALP_LOOP_MINUTES>0: stays alive and scans right after each 5M close
#     (use with the v4 yml). 0 = single scan per run.
#   * Gold weekend filter, TP1/TP2 plan, max-chase limit, daily signal cap.
#   * Every scalp threshold can be changed from environment variables.
#
# v3 changes vs v2:
#   * NO TRADE status now says WHY each side (BUY/SELL) was blocked.
#   * Sweep / BOS-CHoCH windows widened 3 -> 6 candles (45 min -> 90 min).
#   * MIN_SCORE 8 -> 7, MIN_CONTEXT 3 -> 2.
#   * New WATCH alert: setup is valid but only short on score (no trade plan).
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
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3, "session_filter": True, "weekend_closed": True},
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
SWEEP_WINDOW = 6
SWEEP_REF_BARS = 20
SWEEP_MIN_DEPTH_ATR = 0.02  # must poke beyond the level by at least this
SWEEP_MAX_DEPTH_ATR = 1.50  # deeper than this = real breakout, not a sweep

# BOS / CHoCH only counts if the break happened in the last N candles
BREAK_WINDOW = 6

# ---------------- SIGNAL QUALITY ----------------
MIN_SCORE = 8
MAX_SCORE = 12
MIN_CONTEXT = 2
MAX_LOCATION_ATR = 1.50

# Wider sweep/break windows can keep one setup alive for several candles,
# so after a signal, the same symbol+side is muted for this long.
SIGNAL_COOLDOWN_MIN = 60

# WATCH alert: gates pass but score is between WATCH_MIN_SCORE and MIN_SCORE-1.
# Set SEND_WATCHLIST=0 to turn it off. One WATCH per symbol+side per cooldown.
WATCH_MIN_SCORE = 6
WATCH_COOLDOWN_MIN = 60
SEND_WATCHLIST = os.environ.get("SEND_WATCHLIST", "1") == "1"

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

# GitHub Actions cron can start several minutes late, so the window is wide.
# It must stay below 15 (the next 15M candle); duplicates are blocked by state.
STALE_SIGNAL_MIN = 14

# Sends one short "NO TRADE / bot alive" message per symbol per hour.
# Turn off with the environment variable SEND_STATUS_UPDATES=0.
SEND_STATUS_UPDATES = os.environ.get("SEND_STATUS_UPDATES", "1") == "1"

RUN_FOREVER = False
LOOP_SECONDS = 60

STATE_FILE = Path("ict_smc_state.json")

# ---------------- MODE ----------------
MODE = os.environ.get("MODE", "SCALP").upper()
SCALP = MODE == "SCALP"


def _num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


# ---------------- SCALP SETTINGS (v4) ----------------
SCALP_BARS = 1000                    # 5M candles per download (~83 h)
SCALP_MAX_SCORE = 12
SCALP_MIN_SCORE = _num("SCALP_MIN_SCORE", 7, int)
SCALP_WATCH_MIN_SCORE = _num("SCALP_WATCH_MIN_SCORE", 6, int)
SCALP_MIN_TRIGGER = 2
SCALP_MIN_CONTEXT = 1
SCALP_MIN_CONFIRM = 1
SCALP_PULLBACK_ATR = 0.15

SCALP_SL_LOOKBACK = 8                # SL beyond the last 8 x 5M candles
SCALP_SL_BUFFER_ATR = 0.10
SCALP_MIN_SL_ATR = 0.80              # tighter than this is widened
SCALP_MAX_SL_ATR = 2.50              # wider than this is rejected
SCALP_TP1_R = 1.0
SCALP_TP2_R = _num("SCALP_TP2_R", 1.5)
SCALP_MAX_CHASE_ATR = 0.30

SCALP_COOLDOWN_MIN = _num("SCALP_COOLDOWN_MIN", 15, int)    # per symbol + side
SCALP_WATCH_COOLDOWN_MIN = 30
SCALP_MAX_SIGNALS_PER_DAY = _num("SCALP_MAX_SIGNALS_PER_DAY", 8, int)
SCALP_STALE_MIN = 8                  # signal older than this is skipped
SCALP_SESSIONS_UTC = [(6, 20)]       # gold only; BTC runs 24/7

SCALP_LOOP_MINUTES = _num("SCALP_LOOP_MINUTES", 0, int)
SCALP_FETCH_DELAY_SEC = 25           # wait after candle close for data to appear
SCALP_FETCH_RETRIES = 2

# ---------------- TRADE TRACKING (v4.1) ----------------
TRADE_MAX_AGE_HOURS = _num("TRADE_MAX_AGE_HOURS", 6, int)  # unresolved -> timeout
TRADE_TP1_PORTION = 0.5          # share of the position closed at TP1
MAX_CLOSED_HISTORY = 300
DAILY_SUMMARY_HOUR_UTC = 21
WEEKLY_REPORT_WEEKDAY = 6        # Sunday
WEEKLY_REPORT_HOUR_UTC = 18
SCALP_RETRY_WAIT_SEC = 10

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/4.1"})


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


def in_session(ts, sessions=None):
    h = ts.hour + ts.minute / 60
    return any(start <= h < end for start, end in (sessions or SESSIONS_UTC))


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
    watch = []
    diag = []

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

        # ---------- GATES (every failed gate is recorded, not just the first) ----------
        total = context + trigger + confirm
        blocked = []
        if not (sweep_ok or break_ok):
            blocked.append("no sweep/BOS/CHoCH")
        if context < MIN_CONTEXT:
            blocked.append(f"context {context}<{MIN_CONTEXT}")
        if len(conflicts) >= 2:
            blocked.append("conflicts: " + ", ".join(conflicts))
        if total < MIN_SCORE:
            blocked.append(f"score {total}<{MIN_SCORE}")

        diag.append({
            "side": side,
            "score": total,
            "context": context,
            "trigger": trigger,
            "confirm": confirm,
            "blocked": blocked,
        })

        entry = {
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
        }

        if not blocked:
            results.append(entry)
        elif (
            len(blocked) == 1
            and blocked[0].startswith("score")
            and total >= WATCH_MIN_SCORE
        ):
            watch.append(entry)

    base["diag"] = diag
    base["watch"] = watch

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
        plan = make_trade_plan(signal, data15, digits)
        if plan is None:
            result["plan_rejected"] = signal["side"]
        result["signal"] = plan

    return {
        "name": name,
        "price": data15[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data15[-1]["time"],
    }


# ============================================================
# STATUS / TEST MESSAGES
# ============================================================

def build_no_trade_message(name, result, price, digits):
    sweep = result.get("sweep")
    fvg = result.get("fvg")
    ob = result.get("ob")
    pd = result.get("pd")
    m15 = result.get("m15") or {}

    why_lines = []
    for d in result.get("diag", []):
        reason = html.escape("; ".join(d["blocked"])) if d["blocked"] else "ok"
        why_lines.append(f"{d['side']} {d['score']}/{MAX_SCORE} — {reason}")
    if result.get("plan_rejected"):
        why_lines.append(
            f"{result['plan_rejected']} setup found, but SL size is outside "
            f"{MIN_SL_ATR}-{MAX_SL_ATR} x ATR"
        )
    if result.get("ambiguous"):
        why_lines.append("BUY and SELL tied — skipped")
    why = ("\n<b>Why no trade</b>\n" + "\n".join(why_lines) + "\n") if why_lines else ""

    return (
        f"🟡 <b>{name} — MARKET READER</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b>\n"
        f"Bias: <b>{result.get('bias', 'UNKNOWN')}</b>\n"
        f"15M BOS: {m15.get('bos') or 'None'}\n"
        f"15M CHoCH: {m15.get('choch') or 'None'}\n"
        f"Liquidity Sweep: {sweep['side'] if sweep else 'None'}\n"
        f"Order Block: {ob['side'] if ob else 'None'}\n"
        f"FVG: {fvg['side'] if fvg else 'None'}\n"
        f"P/D: {pd['zone'] if pd else 'UNKNOWN'}\n"
        f"{why}\n"
        f"<b>ACTION: NO TRADE</b>\n"
        f"Confluence threshold not reached. (Hourly status — bot is alive.)"
    )


def build_watch_message(name, entry, digits):
    icon = "🟢" if entry["side"] == "BUY" else "🔴"
    sweep = entry.get("sweep")
    reasons = "\n".join(f"✓ {x}" for x in entry["reasons"][:10])
    conflicts = ", ".join(entry["conflicts"]) or "None"
    sweep_text = (
        f"{sweep['side']} @ {sweep['price']:.{digits}f} ({sweep['age']} candle ago)"
        if sweep else "None"
    )

    return (
        f"👀 <b>{name} — WATCH {entry['side']}</b> {icon}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{entry['price']:.{digits}f}</b>\n"
        f"Score: <b>{entry['score']}/{MAX_SCORE}</b> "
        f"(need {MIN_SCORE}) — ctx {entry['context']} · trig {entry['trigger']} · conf {entry['confirm']}\n"
        f"Liquidity Sweep: {sweep_text}\n"
        f"15M BOS: {entry['m15_structure']['bos'] or 'None'} · "
        f"CHoCH: {entry['m15_structure']['choch'] or 'None'}\n\n"
        f"{reasons}\n\n"
        f"Conflicts: {html.escape(conflicts)}\n"
        f"⚠️ Early heads-up only — NOT a trade signal. No entry/SL/TP yet."
    )


def maybe_send_watch(state, name, entry, digits, ts):
    """Heads-up for near-miss setups, one per symbol+side per cooldown."""
    if not SEND_WATCHLIST:
        return

    key = f"watchts:{name}:{entry['side']}"
    last = state.get(key)
    if last:
        try:
            last_dt = dt.datetime.fromisoformat(last)
            wcd = SCALP_WATCH_COOLDOWN_MIN if SCALP else WATCH_COOLDOWN_MIN
            if (ts - last_dt).total_seconds() < wcd * 60:
                return
        except ValueError:
            pass

    send_telegram(
        (build_scalp_watch_message if SCALP else build_watch_message)(name, entry, digits)
    )
    state[key] = ts.isoformat()
    save_state(state)
    print(f"[{name}] WATCH {entry['side']} sent score={entry['score']}")


def maybe_send_status(state, name, text, ts):
    """Send at most one status message per symbol per UTC hour."""
    if not SEND_STATUS_UPDATES:
        return

    hour_key = ts.strftime("%Y-%m-%d %H")
    state_key = f"status:{name}"

    if state.get(state_key) == hour_key:
        return

    send_telegram(text)
    state[state_key] = hour_key
    save_state(state)
    print(f"[{name}] status message sent")


def send_test_message():
    send_telegram(
        f"✅ <b>ICT SMC Market Reader v4</b> ({MODE} mode)\n"
        "Telegram connection works.\n"
        f"Time: {now_utc().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    print("Test message sent")


# ============================================================
# SCALP MODE  (v4)
#   One 5M download per symbol per candle. 15M and 1H are built locally
#   from those 5M candles, so API credits stay low (1 call / symbol / 5 min).
#   Trend:   15M + 1H (structure + EMA 9/15)
#   Trigger: 5M sweep / BOS / CHoCH / displacement / OB-FVG retest
#   Entry:   5M EMA 9/15 pullback, pinbar, EMA alignment
# ============================================================

def resample(candles, minutes):
    """Build closed candles of `minutes` from closed 5M candles.
    A bucket is kept only if it is complete (no forming candle)."""
    per = minutes // 5
    out = []
    bucket = None
    group = []

    def flush():
        if bucket is not None and len(group) == per:
            out.append({
                "time": group[0]["time"],
                "dt": group[0]["dt"],
                "open": group[0]["open"],
                "high": max(x["high"] for x in group),
                "low": min(x["low"] for x in group),
                "close": group[-1]["close"],
            })

    for c in candles:
        epoch = int(c["dt"].timestamp())
        b = epoch - epoch % (minutes * 60)
        if b != bucket:
            flush()
            bucket = b
            group = []
        group.append(c)
    flush()
    return out


def vote(*states):
    bull = sum(1 for s in states if s == "BULLISH")
    bear = sum(1 for s in states if s == "BEARISH")
    if bull > bear:
        return "BULLISH"
    if bear > bull:
        return "BEARISH"
    return "NEUTRAL"


def market_closed_weekend(ts):
    """Gold (XAUUSD) weekend close: Fri 21:00 UTC -> Sun 22:00 UTC."""
    wd = ts.weekday()  # Mon=0 ... Sun=6
    if wd == 5:
        return True
    if wd == 4 and ts.hour >= 21:
        return True
    if wd == 6 and ts.hour < 22:
        return True
    return False


def ema_pullback(data5, side, a5):
    closes = [c["close"] for c in data5]
    e9 = ema(closes, EMA_FAST)
    e15 = ema(closes, EMA_SLOW)
    if e9[-1] is None or e15[-1] is None:
        return False
    last3 = data5[-3:]
    last = data5[-1]
    tol = a5 * SCALP_PULLBACK_ATR
    if side == "BUY":
        return (
            e9[-1] > e15[-1]
            and min(c["low"] for c in last3) <= e15[-1] + tol
            and last["close"] > e9[-1]
        )
    return (
        e9[-1] < e15[-1]
        and max(c["high"] for c in last3) >= e15[-1] - tol
        and last["close"] < e9[-1]
    )


def scalp_score(data1h, data15, data5):
    price = data5[-1]["close"]
    a5 = atr(data5, ATR_PERIOD)[-1]

    if not a5 or len(data5) < 60 or len(data15) < 30 or len(data1h) < 20:
        return {"signal": None, "bias": "UNKNOWN", "diag": [], "watch": []}

    m1h = market_structure(data1h)
    e1h = ema_state(data1h)
    m15 = market_structure(data15)
    e15 = ema_state(data15)
    m5 = market_structure(data5)
    e5 = ema_state(data5)

    h1 = vote(m1h["bias"], e1h["state"])
    t15 = vote(m15["bias"], e15["state"])

    sweep = detect_liquidity_sweep(data5)
    disp = displacement(data5)
    pd15 = premium_discount(data15)
    pin = pinbar(data5[-1]) or pinbar(data5[-2])

    if t15 != "NEUTRAL":
        bias = t15
    elif h1 != "NEUTRAL":
        bias = h1
    else:
        bias = "RANGE"

    base = {
        "bias": bias, "t15": t15, "h1": h1,
        "m5": m5, "m15": m15, "ema5": e5,
        "sweep": sweep, "pd": pd15,
        "ob": detect_order_block(data5), "fvg": detect_fvg(data5),
    }

    results, watch, diag = [], [], []

    for side in ("BUY", "SELL"):
        wanted = "BULLISH" if side == "BUY" else "BEARISH"
        opp = "BEARISH" if side == "BUY" else "BULLISH"
        reasons, conflicts = [], []
        context = trigger = confirm = 0

        # ---------- CONTEXT (max 4) ----------
        if t15 == wanted:
            context += 2
            reasons.append("15M trend aligned")
        elif t15 == opp:
            conflicts.append("15M trend against")

        if h1 == wanted:
            context += 1
            reasons.append("1H trend aligned")
        elif h1 == opp:
            conflicts.append("1H trend against")

        if (side == "BUY" and pd15["zone"] == "DISCOUNT") or (
            side == "SELL" and pd15["zone"] == "PREMIUM"
        ):
            context += 1
            reasons.append("good P/D location")
        elif pd15["zone"] in ("PREMIUM", "DISCOUNT"):
            conflicts.append("poor P/D location")

        # ---------- TRIGGER (max 5) ----------
        sweep_ok = (
            sweep is not None
            and ((side == "BUY" and sweep["side"] == "SELL_SIDE")
                 or (side == "SELL" and sweep["side"] == "BUY_SIDE"))
        )
        if sweep_ok:
            trigger += 2
            reasons.append(f"5M {'sell' if side == 'BUY' else 'buy'}-side sweep")

        break_ok = False
        if m5["choch"] == wanted:
            trigger += 1
            break_ok = True
            reasons.append("5M CHoCH")
        elif m5["bos"] == wanted:
            trigger += 1
            break_ok = True
            reasons.append("5M BOS")

        if disp == wanted:
            trigger += 1
            reasons.append("5M displacement")
        elif disp:
            conflicts.append("opposite displacement")

        ob = detect_order_block(data5, wanted)
        fvg = detect_fvg(data5, wanted)
        poi = []
        if ob and in_zone(price, ob, a5 * 0.35):
            poi.append("order block")
        if fvg and in_zone(price, fvg, a5 * 0.35):
            poi.append("FVG")
        if poi:
            trigger += 1
            reasons.append("5M " + " + ".join(poi) + " retest")

        # ---------- ENTRY CONFIRMATION (max 3) ----------
        if e5["state"] == wanted:
            confirm += 1
            reasons.append("5M EMA 9/15 aligned")

        if ema_pullback(data5, side, a5):
            confirm += 1
            reasons.append("EMA pullback + reclaim")

        pin_ok = (side == "BUY" and pin == "bullish") or (side == "SELL" and pin == "bearish")
        if pin_ok:
            confirm += 1
            reasons.append("5M pinbar rejection")

        # ---------- GATES ----------
        total = context + trigger + confirm
        blocked = []
        if not (sweep_ok or break_ok):
            blocked.append("no 5M sweep/BOS/CHoCH")
        if trigger < SCALP_MIN_TRIGGER:
            blocked.append(f"trigger {trigger}<{SCALP_MIN_TRIGGER}")
        if context < SCALP_MIN_CONTEXT:
            blocked.append(f"context {context}<{SCALP_MIN_CONTEXT}")
        if confirm < SCALP_MIN_CONFIRM:
            blocked.append("no 5M entry confirmation")
        if t15 == opp and h1 == opp:
            blocked.append("against 15M + 1H trend")
        if len(conflicts) >= 3:
            blocked.append("conflicts: " + ", ".join(conflicts))
        if total < SCALP_MIN_SCORE:
            blocked.append(f"score {total}<{SCALP_MIN_SCORE}")

        diag.append({
            "side": side, "score": total, "context": context,
            "trigger": trigger, "confirm": confirm, "blocked": blocked,
        })

        entry = {
            "side": side, "score": total, "context": context,
            "trigger": trigger, "confirm": confirm,
            "reasons": reasons, "conflicts": conflicts,
            "atr5": a5, "price": price, "pin": pin,
            "m5": m5, "t15": t15, "h1": h1, "pd": pd15,
            "sweep": sweep if sweep_ok else None,
            "ob": ob, "fvg": fvg,
        }

        if not blocked:
            results.append(entry)
        elif (
            len(blocked) == 1
            and blocked[0].startswith("score")
            and total >= SCALP_WATCH_MIN_SCORE
        ):
            watch.append(entry)

    base["diag"] = diag
    base["watch"] = watch

    if not results:
        return dict(base, signal=None)

    results.sort(key=lambda x: x["score"], reverse=True)
    if len(results) > 1 and results[0]["score"] == results[1]["score"]:
        return dict(base, signal=None, ambiguous=True)

    return dict(base, signal=results[0])


def make_scalp_plan(signal, data5):
    entry = data5[-1]["close"]
    a = signal["atr5"]
    recent = data5[-SCALP_SL_LOOKBACK:]

    if signal["side"] == "BUY":
        sl = min(c["low"] for c in recent) - a * SCALP_SL_BUFFER_ATR
        risk = entry - sl
    else:
        sl = max(c["high"] for c in recent) + a * SCALP_SL_BUFFER_ATR
        risk = sl - entry

    if risk <= 0:
        return None

    # Too tight -> widen to the minimum (noise would stop it out).
    if risk < a * SCALP_MIN_SL_ATR:
        risk = a * SCALP_MIN_SL_ATR
        sl = entry - risk if signal["side"] == "BUY" else entry + risk

    risk_atr = risk / a
    if risk_atr > SCALP_MAX_SL_ATR:
        return None

    sign = 1 if signal["side"] == "BUY" else -1
    plan = dict(signal)
    plan.update({
        "entry": entry,
        "sl": sl,
        "tp1": entry + sign * risk * SCALP_TP1_R,
        "tp": entry + sign * risk * SCALP_TP2_R,
        "risk": risk,
        "risk_atr": risk_atr,
        "chase_limit": entry + sign * a * SCALP_MAX_CHASE_ATR,
        "time": data5[-1]["time"],
        "close_dt": data5[-1]["dt"] + dt.timedelta(minutes=5),
    })
    return plan


def analyze_symbol_scalp(config, loop_mode=False):
    name = config["name"]
    digits = config["digits"]

    data5 = None
    for attempt in range(SCALP_FETCH_RETRIES + 1):
        data5 = get_candles(config["td_symbol"], "5min", SCALP_BARS)
        if not data5:
            raise RuntimeError("No closed 5M candles")
        now = now_utc()
        expected = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) \
            - dt.timedelta(minutes=5)
        # In loop mode we wait for the data provider to publish the newest candle.
        if not loop_mode or data5[-1]["dt"] >= expected or attempt == SCALP_FETCH_RETRIES:
            break
        time.sleep(SCALP_RETRY_WAIT_SEC)

    data15 = resample(data5, 15)
    data1h = resample(data5, 60)

    if len(data5) < 60 or len(data15) < 30 or len(data1h) < 20:
        raise RuntimeError(
            f"Not enough candles (5M={len(data5)}, 15M={len(data15)}, 1H={len(data1h)})"
        )

    result = scalp_score(data1h, data15, data5)

    signal = result.get("signal")
    if signal:
        plan = make_scalp_plan(signal, data5)
        if plan is None:
            result["plan_rejected"] = signal["side"]
        result["signal"] = plan

    return {
        "name": name,
        "price": data5[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data5[-1]["time"],
        "data5": data5,
    }


# ---------------- SCALP MESSAGES ----------------

def build_scalp_signal_message(name, s, result, digits):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    f = lambda v: f"{v:.{digits}f}"
    age = max((now_utc() - s["close_dt"]).total_seconds() / 60, 0)
    reasons = "\n".join(f"✓ {x}" for x in s["reasons"][:12])
    conflicts = ", ".join(s["conflicts"]) or "None"
    sweep = s.get("sweep")
    sweep_text = (
        f"{sweep['side']} @ {f(sweep['price'])} ({sweep['age']} candle ago)" if sweep else "None"
    )
    chase_word = "above" if s["side"] == "BUY" else "below"

    return (
        f"⚡ {icon} <b>{name} — SCALP {s['side']}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⭐ Score: <b>{s['score']}/{SCALP_MAX_SCORE}</b> "
        f"(ctx {s['context']}/4 · trig {s['trigger']}/5 · entry {s['confirm']}/3)\n"
        f"🧭 15M: <b>{s['t15']}</b> · 1H: <b>{s['h1']}</b>\n"
        f"⏱ Signal age: {age:.1f} min\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <b>{f(s['entry'])}</b> (5M close)\n"
        f"SL: <b>{f(s['sl'])}</b>\n"
        f"TP1 ({SCALP_TP1_R:g}R): <b>{f(s['tp1'])}</b>\n"
        f"TP2 ({SCALP_TP2_R:g}R): <b>{f(s['tp'])}</b>\n"
        f"Risk: {f(s['risk'])} ({s['risk_atr']:.2f} x 5M ATR)\n"
        f"Skip if price is already {chase_word} <b>{f(s['chase_limit'])}</b>\n"
        f"After TP1: close part / move SL to breakeven.\n\n"
        f"<b>Why</b>\n{reasons}\n\n"
        f"Liquidity Sweep: {sweep_text}\n"
        f"5M BOS: {s['m5']['bos'] or 'None'} · CHoCH: {s['m5']['choch'] or 'None'}\n"
        f"P/D (15M): {s['pd']['zone']}\n"
        f"Conflicts: {html.escape(conflicts)}\n\n"
        f"⏰ Closed 5M candle: {s['time']} UTC\n"
        f"⚠️ Rule-based alert, not a guarantee. Use your own risk rules."
    )


def build_scalp_watch_message(name, e, digits):
    icon = "🟢" if e["side"] == "BUY" else "🔴"
    reasons = "\n".join(f"✓ {x}" for x in e["reasons"][:10])
    conflicts = ", ".join(e["conflicts"]) or "None"
    return (
        f"👀 <b>{name} — SCALP WATCH {e['side']}</b> {icon}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{e['price']:.{digits}f}</b>\n"
        f"Score: <b>{e['score']}/{SCALP_MAX_SCORE}</b> (need {SCALP_MIN_SCORE}) — "
        f"ctx {e['context']} · trig {e['trigger']} · entry {e['confirm']}\n"
        f"15M: {e['t15']} · 1H: {e['h1']}\n\n"
        f"{reasons}\n\n"
        f"Conflicts: {html.escape(conflicts)}\n"
        f"⚠️ Heads-up only — NOT a trade signal."
    )


def build_scalp_no_trade_message(name, result, price, digits):
    sweep = result.get("sweep")
    m5 = result.get("m5") or {}
    pd = result.get("pd")

    lines = []
    for d in result.get("diag", []):
        reason = html.escape("; ".join(d["blocked"])) if d["blocked"] else "ok"
        lines.append(f"{d['side']} {d['score']}/{SCALP_MAX_SCORE} — {reason}")
    if result.get("plan_rejected"):
        lines.append(
            f"{result['plan_rejected']} setup found, but SL > {SCALP_MAX_SL_ATR} x 5M ATR"
        )
    if result.get("ambiguous"):
        lines.append("BUY and SELL tied — skipped")
    why = ("\n<b>Why no trade</b>\n" + "\n".join(lines) + "\n") if lines else ""

    return (
        f"🟡 <b>{name} — SCALP READER</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b>\n"
        f"15M: {result.get('t15', '?')} · 1H: {result.get('h1', '?')}\n"
        f"5M BOS: {m5.get('bos') or 'None'} · CHoCH: {m5.get('choch') or 'None'}\n"
        f"Liquidity Sweep: {sweep['side'] if sweep else 'None'}\n"
        f"P/D (15M): {pd['zone'] if pd else 'UNKNOWN'}\n"
        f"{why}\n"
        f"<b>ACTION: NO TRADE</b>\n"
        f"(Hourly status — bot is alive.)"
    )


# ============================================================
# TRADE TRACKING + REPORTS  (v4.1)
#   Every sent SCALP signal is remembered. On each scan the bot checks the
#   new 5M candles and reports TP1 / TP2 / SL / breakeven / timeout.
#   Assumption used for R: 50% of the position closes at TP1, stop moves to
#   breakeven, the other 50% runs to TP2. If one candle touches both SL and
#   TP1, the SL is counted first (conservative).
# ============================================================

def _iso(d):
    return d.isoformat()


def _from_iso(s):
    return dt.datetime.fromisoformat(s)


def register_trade(state, name, plan):
    trades = state.setdefault("open_trades", [])
    tid = f"{name}:{plan['time']}:{plan['side']}"
    if any(t["id"] == tid for t in trades):
        return
    start = plan["close_dt"]
    trades.append({
        "id": tid,
        "name": name,
        "side": plan["side"],
        "score": plan["score"],
        "entry": plan["entry"],
        "sl": plan["sl"],
        "tp1": plan["tp1"],
        "tp2": plan["tp"],
        "risk": plan["risk"],
        "tp1_r": SCALP_TP1_R,
        "tp2_r": SCALP_TP2_R,
        "tp1_hit": False,
        "start_dt": _iso(start),
        "checked_until": _iso(start - dt.timedelta(minutes=5)),
        "last_close": plan["entry"],
    })


def _r_of(t, price):
    sign = 1 if t["side"] == "BUY" else -1
    return sign * (price - t["entry"]) / t["risk"]


def _fmt_r(r):
    return f"{r:+.2f}R"


def advance_trade(t, data5, ts):
    """Moves one trade forward. Returns (events, closed) where events is a
    list of Telegram texts and closed is None or (label, r, price)."""
    events = []
    closed = None
    buy = t["side"] == "BUY"
    checked = _from_iso(t["checked_until"])
    deadline = _from_iso(t["start_dt"]) + dt.timedelta(hours=TRADE_MAX_AGE_HOURS)
    part = TRADE_TP1_PORTION
    r_tp2_total = part * t["tp1_r"] + (1 - part) * t["tp2_r"]

    for c in data5:
        if c["dt"] <= checked:
            continue
        if c["dt"] > deadline:
            break
        checked = c["dt"]
        t["last_close"] = c["close"]
        hi, lo = c["high"], c["low"]

        if not t["tp1_hit"]:
            if (lo <= t["sl"]) if buy else (hi >= t["sl"]):
                closed = ("SL", -1.0, t["sl"])
                break
            if (hi >= t["tp1"]) if buy else (lo <= t["tp1"]):
                t["tp1_hit"] = True
                events.append(("TP1", t["tp1"]))
                if (hi >= t["tp2"]) if buy else (lo <= t["tp2"]):
                    closed = ("TP2", r_tp2_total, t["tp2"])
                    break
        else:
            if (lo <= t["entry"]) if buy else (hi >= t["entry"]):
                closed = ("BE", part * t["tp1_r"], t["entry"])
                break
            if (hi >= t["tp2"]) if buy else (lo <= t["tp2"]):
                closed = ("TP2", r_tp2_total, t["tp2"])
                break

    t["checked_until"] = _iso(checked)

    if closed is None and ts > deadline:
        price = t["last_close"]
        rest = _r_of(t, price)
        r = part * t["tp1_r"] + (1 - part) * rest if t["tp1_hit"] else rest
        closed = ("EXPIRED", r, price)

    return events, closed


def _trade_event_text(t, kind, digits, closed=None):
    head = f"<b>{t['name']} {t['side']}</b>"
    f = lambda v: f"{v:.{digits}f}"
    if kind == "TP1":
        return (
            f"✅ {head} — <b>TP1 hit</b> ({f(t['tp1'])})\n"
            f"Take partial profit and move SL to breakeven ({f(t['entry'])}).\n"
            f"TP2 still open: {f(t['tp2'])}"
        )
    label, r, price = closed
    if label == "SL":
        return f"❌ {head} — <b>SL hit</b> at {f(price)}\nResult: <b>{_fmt_r(r)}</b>"
    if label == "TP2":
        return f"🏆 {head} — <b>TP2 hit</b> at {f(price)}\nResult: <b>{_fmt_r(r)}</b> (50% at TP1, 50% at TP2)"
    if label == "BE":
        return f"➖ {head} — back to breakeven after TP1\nResult: <b>{_fmt_r(r)}</b>"
    return (
        f"⌛ {head} — closed by timeout ({TRADE_MAX_AGE_HOURS}h) at {f(price)}\n"
        f"Result: <b>{_fmt_r(r)}</b>"
    )


def _close_trade(state, t, closed, ts):
    hist = state.setdefault("closed_trades", [])
    hist.append({
        "name": t["name"], "side": t["side"], "result": closed[0],
        "r": round(closed[1], 3), "score": t.get("score"),
        "opened": t["start_dt"], "closed": _iso(ts),
    })
    del hist[:-MAX_CLOSED_HISTORY]


def track_trades(state, name, data5, digits, ts):
    trades = state.get("open_trades", [])
    if not any(t["name"] == name for t in trades):
        return

    keep = []
    for t in trades:
        if t["name"] != name:
            keep.append(t)
            continue

        events, closed = advance_trade(t, data5, ts)

        for kind, _price in events:
            try:
                send_telegram(_trade_event_text(t, kind, digits))
            except Exception as exc:
                print(f"[{name}] telegram error (tp1 event): {exc}")

        if closed:
            try:
                send_telegram(_trade_event_text(t, "END", digits, closed))
            except Exception as exc:
                print(f"[{name}] telegram error (close event): {exc}")
            _close_trade(state, t, closed, ts)
            print(f"[{name}] trade closed {closed[0]} {closed[1]:+.2f}R")
        else:
            keep.append(t)

    state["open_trades"] = keep
    save_state(state)


def track_only(state, config, ts):
    """Outside the signal session: keep following open trades only."""
    name = config["name"]
    if not any(t["name"] == name for t in state.get("open_trades", [])):
        return
    data5 = get_candles(config["td_symbol"], "5min", 300)
    track_trades(state, name, data5, config["digits"], ts)


def _stats_lines(trades):
    if not trades:
        return ["No closed trades."]
    n = len(trades)
    total = sum(x["r"] for x in trades)
    wins = sum(1 for x in trades if x["r"] > 0)
    count = {}
    for x in trades:
        count[x["result"]] = count.get(x["result"], 0) + 1
    parts = " · ".join(f"{k} {v}" for k, v in sorted(count.items()))
    return [
        f"Trades: {n} (win {wins}, win rate {wins / n * 100:.0f}%)",
        f"Total: <b>{_fmt_r(total)}</b> (avg {_fmt_r(total / n)})",
        parts,
    ]


def build_report(state, ts, days, title):
    since = ts - dt.timedelta(days=days)
    closed = [
        x for x in state.get("closed_trades", [])
        if _from_iso(x["closed"]) >= since
    ]
    lines = [f"📊 <b>{title}</b>", "━━━━━━━━━━━━━━━━"]
    names = sorted({x["name"] for x in closed})
    for n in names:
        lines.append(f"<b>{n}</b>")
        lines += _stats_lines([x for x in closed if x["name"] == n])
        lines.append("")
    lines.append("<b>All symbols</b>")
    lines += _stats_lines(closed)

    open_trades = state.get("open_trades", [])
    if open_trades:
        lines.append("")
        lines.append(f"Open now: {len(open_trades)}")
        for t in open_trades:
            tp = " (TP1 done)" if t["tp1_hit"] else ""
            lines.append(f"• {t['name']} {t['side']} @ {t['entry']:.3f}{tp}")
    lines.append("")
    lines.append("R assumes 50% closed at TP1, 50% at TP2.")
    return "\n".join(lines), len(closed)


def maybe_send_reports(state, ts):
    # Daily summary after DAILY_SUMMARY_HOUR_UTC (only if something happened today)
    day = ts.strftime("%Y-%m-%d")
    if ts.hour >= DAILY_SUMMARY_HOUR_UTC and state.get("dailysent") != day:
        text, n = build_report(state, ts, 1, "Daily scalp summary (last 24h)")
        if n or state.get("open_trades"):
            send_telegram(text)
        state["dailysent"] = day
        save_state(state)

    # Weekly report on Sunday evening
    iso = ts.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if (
        ts.weekday() == WEEKLY_REPORT_WEEKDAY
        and ts.hour >= WEEKLY_REPORT_HOUR_UTC
        and state.get("weeklysent") != week
    ):
        text, _ = build_report(state, ts, 7, "Weekly scalp report (last 7 days)")
        send_telegram(text)
        state["weeklysent"] = week
        save_state(state)


def send_report_now():
    state = load_state()
    ts = now_utc()
    text, _ = build_report(state, ts, 7, "Scalp report (last 7 days)")
    send_telegram(text)
    print("Report sent")


# ============================================================
# MAIN
# ============================================================

def prune_state(state, ts):
    today = ts.strftime("%Y-%m-%d")
    for k in [k for k in state if k.startswith("daycount:") and not k.endswith(today)]:
        del state[k]


def run_once(loop_mode=False):
    state = load_state()
    ts0 = now_utc()
    prune_state(state, ts0)

    if SCALP:
        try:
            maybe_send_reports(state, ts0)
        except Exception as exc:
            print(f"[REPORT] ERROR: {exc}")

    stale_limit = SCALP_STALE_MIN if SCALP else STALE_SIGNAL_MIN
    cooldown_min = SCALP_COOLDOWN_MIN if SCALP else SIGNAL_COOLDOWN_MIN

    for config in SYMBOLS:
        name = config["name"]

        try:
            ts = now_utc()

            # Filters run BEFORE any API call, which also saves quota.
            if config.get("weekend_closed") and market_closed_weekend(ts):
                print(f"[{name}] weekend, market closed")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nMarket closed (weekend) — scanning paused. (Bot is alive.)",
                    ts,
                )
                continue
            if config.get("session_filter") and not in_session(
                ts, SCALP_SESSIONS_UTC if SCALP else None
            ):
                print(f"[{name}] outside session, skipped")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nOutside trading session — scanning paused. (Bot is alive.)",
                    ts,
                )
                if SCALP:
                    track_only(state, config, ts)
                continue
            if in_news_blackout(ts):
                print(f"[{name}] news blackout, skipped")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nNews blackout — scanning paused. (Bot is alive.)",
                    ts,
                )
                if SCALP:
                    track_only(state, config, ts)
                continue

            data = (
                analyze_symbol_scalp(config, loop_mode=loop_mode)
                if SCALP else analyze_symbol(config)
            )
            result = data["result"]
            signal = result.get("signal")
            max_score = SCALP_MAX_SCORE if SCALP else MAX_SCORE

            if SCALP:
                track_trades(state, name, data["data5"], data["digits"], ts)

            if not signal:
                print(
                    f"[{name}] NO TRADE | bias={result.get('bias')} | price={data['price']}"
                )
                for d in result.get("diag", []):
                    print(f"[{name}]   {d['side']} {d['score']}/{max_score} blocked: {d['blocked']}")

                watch = sorted(result.get("watch", []), key=lambda x: x["score"], reverse=True)
                if watch:
                    maybe_send_watch(state, name, watch[0], data["digits"], ts)

                no_trade = (
                    build_scalp_no_trade_message if SCALP else build_no_trade_message
                )(name, result, data["price"], data["digits"])
                maybe_send_status(state, name, no_trade, ts)
                continue

            delay = (now_utc() - signal["close_dt"]).total_seconds() / 60

            if delay < 0 or delay > stale_limit:
                print(f"[{name}] stale signal skipped: {delay:.1f}m")
                continue

            key = f"{name}:{signal['time']}:{signal['side']}"

            if state.get(name) == key:
                print(f"[{name}] duplicate skipped")
                continue

            cd_key = f"sigts:{name}:{signal['side']}"
            last_sent = state.get(cd_key)
            if last_sent:
                try:
                    since = (ts - dt.datetime.fromisoformat(last_sent)).total_seconds() / 60
                    if since < cooldown_min:
                        print(f"[{name}] cooldown skipped ({since:.0f}m since last {signal['side']})")
                        continue
                except ValueError:
                    pass

            day_key = f"daycount:{name}:{ts.strftime('%Y-%m-%d')}"
            if SCALP and state.get(day_key, 0) >= SCALP_MAX_SIGNALS_PER_DAY:
                print(f"[{name}] daily signal cap reached")
                continue

            message = (
                build_scalp_signal_message if SCALP else build_signal_message
            )(name, signal, result, data["digits"])
            send_telegram(message)

            state[name] = key
            state[cd_key] = ts.isoformat()
            state[day_key] = state.get(day_key, 0) + 1
            if SCALP:
                register_trade(state, name, signal)
            save_state(state)

            print(f"[{name}] SENT {signal['side']} score={signal['score']}/{max_score}")

        except Exception as exc:
            print(f"[{name}] ERROR: {exc}")


def sleep_until_next_5m():
    """Sleep until the next 5M candle close (+ SCALP_FETCH_DELAY_SEC).
    Returns False if that moment is after the loop deadline."""
    now = now_utc()
    boundary = now.replace(second=0, microsecond=0)
    boundary += dt.timedelta(minutes=5 - boundary.minute % 5)
    target = boundary + dt.timedelta(seconds=SCALP_FETCH_DELAY_SEC)
    if (now - target).total_seconds() > 0:
        target += dt.timedelta(minutes=5)
    return target


def run_scalp_loop():
    """Long-running loop: one scan right after every 5M candle closes."""
    deadline = now_utc() + dt.timedelta(minutes=SCALP_LOOP_MINUTES)
    print(f"[LOOP] scalp loop until {deadline.strftime('%H:%M:%S')} UTC")

    run_once(loop_mode=True)  # immediate scan; the stale filter drops old setups

    while True:
        target = sleep_until_next_5m()
        if target >= deadline:
            break
        wait = (target - now_utc()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        try:
            run_once(loop_mode=True)
        except Exception as exc:
            print(f"[LOOP] ERROR: {exc}")

    print("[LOOP] finished, next scheduled job takes over")


def main():
    require_credentials()

    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        send_test_message()
        return

    if "--report" in sys.argv or os.environ.get("SEND_REPORT_NOW") == "1":
        send_report_now()
        return

    if SCALP and SCALP_LOOP_MINUTES > 0:
        run_scalp_loop()
        return

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
