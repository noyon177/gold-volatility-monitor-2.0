import os
import sys
import io
import json
import math
import time
import html
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC MARKET MOVEMENT READER  (v3 — premium edition)
#
# Environment variables:
#   TWELVE_DATA_API_KEY      (required)
#   TELEGRAM_BOT_TOKEN       (required)
#   TELEGRAM_CHAT_ID         (required)
#   STRATEGY_MODE            scalp (default) | swing
#   ACCOUNT_BALANCE          e.g. 10000  -> enables the lot-size calculator
#   RISK_PERCENT             e.g. 1.0    (default 1.0)
#   USE_SESSION_FILTER       1 = scan gold only inside kill zones (default 0 = always)
#   SEND_STATUS_UPDATES / SEND_CHART / SEND_MORNING_BRIEF /
#   SEND_WEEKLY_REPORT       set to 0 to switch that feature off
#
# Strategy (scalp mode):
#   4H bias -> 30M structure/SR -> 15M setup -> 5M confirmation
# Strategy (swing mode):
#   1D bias -> 4H structure/SR -> 1H setup -> 15M confirmation
#
# Scoring (max 12):
#   Context      (max 5): HTF bias 2, structure 1, P/D 1, S/R 1
#   Trigger      (max 5): sweep 2, BOS/CHoCH 1, displacement 1, OB/FVG 1
#   Confirmation (max 2): setup EMA 1, entry pinbar/EMA 1
#   Gates: must have sweep or structure break, and context >= MIN_CONTEXT
#
# v3 additions:
#   1  Auto trade tracking (TP / SL / expiry) + results to Telegram
#   2  Auto news blackout (high-impact USD events)
#   3  Chart image with entry / SL / TP / OB / FVG
#   4  Lot-size calculator
#   5  Daily safety limits (max signals, loss streak cooldown, daily -R)
#   6  TP1 / breakeven alert
#   7  Signal grade A+ / A / B
#   8  Volatility (ATR) + spread + weekend / Friday filters
#   9  Morning brief per symbol
#  10  Weekly performance report
#  11  Swing preset (STRATEGY_MODE=swing)
# ============================================================

# contract_size / lot_step / typical_spread depend on your broker — adjust them.
#   XAUUSD: 1 lot = 100 oz  -> $1 move = $100 per lot
#   BTCUSD: 1 lot = 1 BTC on most CFD brokers
SYMBOLS = [
    {
        "name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3,
        "session_filter": True, "weekend_filter": True,
        "contract_size": 100, "lot_step": 0.01, "min_lot": 0.01,
        "typical_spread": 0.30,
    },
    {
        "name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 3,
        "session_filter": False, "weekend_filter": False,
        "contract_size": 1, "lot_step": 0.01, "min_lot": 0.01,
        "typical_spread": 15.0,
    },
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

def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


# GitHub Actions cron can start several minutes late, so the window is wide.
# It must stay below the setup candle length (15M in scalp mode); duplicates
# are blocked by state.
STALE_SIGNAL_MIN = 14

# Sends one short "NO TRADE / bot alive" message per symbol per hour.
# Turn off with the environment variable SEND_STATUS_UPDATES=0.
SEND_STATUS_UPDATES = os.environ.get("SEND_STATUS_UPDATES", "1") == "1"

RUN_FOREVER = False
LOOP_SECONDS = 60

# ---------------- SESSION FILTER ----------------
# Kill-zone filter (SESSIONS_UTC). Default OFF = scan 24h; XAUUSD still skips the
# weekend / Friday-night market close. Set USE_SESSION_FILTER=1 to scan gold only in kill zones.
USE_SESSION_FILTER = os.environ.get("USE_SESSION_FILTER", "0") == "1"

# ---------------- TRADE TRACKING ----------------
TRACK_TRADES = True
TP1_R = 1.0                  # TP1 distance in R (risk multiples)
PARTIAL_CLOSE_PERCENT = 50   # suggested partial close at TP1
TRADE_EXPIRY_HOURS = 12      # open trade is closed as EXPIRED after this
MAX_OPEN_PER_SYMBOL = 1      # no new signal while a trade is still open
MAX_CLOSED_HISTORY = 500

# ---------------- SIGNAL GRADES ----------------
GRADE_A_PLUS = 11            # score >= 11  -> A+
GRADE_A = 9                  # score >= 9   -> A, otherwise B

# ---------------- DAILY SAFETY (UTC day) ----------------
MAX_SIGNALS_PER_DAY = 4      # 0 = unlimited
MAX_CONSECUTIVE_LOSSES = 2   # 0 = off
COOLDOWN_MINUTES = 60        # pause after the loss streak above
MAX_DAILY_LOSS_R = 3.0       # stop for the day at -3R, 0 = off

# ---------------- VOLATILITY / SPREAD / WEEKEND ----------------
USE_VOLATILITY_FILTER = True
ATR_AVG_BARS = 50
MIN_ATR_RATIO = 0.55         # setup-TF ATR vs its average: below = dead market
MAX_ATR_RATIO = 2.80         # above = news spike / chaos
MIN_RISK_TO_SPREAD = 5.0     # SL distance must be >= 5x typical spread
FRIDAY_CUTOFF_UTC = 19       # no new gold signals after this hour on Friday
SUNDAY_OPEN_UTC = 22         # no gold signals before this hour on Sunday

# ---------------- AUTO NEWS ----------------
USE_AUTO_NEWS = True
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CURRENCIES = ("USD",)   # gold and BTC react to high-impact USD news
NEWS_REFRESH_HOURS = 6

# Manual extra blackouts, UTC. Example: "2026-10-09 12:30"
NEWS_BLACKOUTS_UTC = []
NEWS_WINDOW_MIN = 30         # minutes before AND after each event

# ---------------- LOT SIZE ----------------
ACCOUNT_BALANCE = _env_float("ACCOUNT_BALANCE", 0)
RISK_PERCENT = _env_float("RISK_PERCENT", 1.0)
REFERENCE_RISK_USD = 100.0   # used when ACCOUNT_BALANCE is not set

# ---------------- CHART ----------------
SEND_CHART = os.environ.get("SEND_CHART", "1") == "1"
CHART_BARS = 70

# ---------------- MORNING BRIEF ----------------
SEND_MORNING_BRIEF = os.environ.get("SEND_MORNING_BRIEF", "1") == "1"
BRIEF_HOUR_UTC = 6
BRIEF_WINDOW_HOURS = 4       # brief is sent on the first run inside this window
BRIEF_INTERVAL = "30min"
BRIEF_BARS = 220
ASIAN_RANGE_UTC = (0, 6)

# ---------------- WEEKLY REPORT ----------------
SEND_WEEKLY_REPORT = os.environ.get("SEND_WEEKLY_REPORT", "1") == "1"

# ---------------- STRATEGY MODE / PRESETS ----------------
PRESETS = {
    "scalp": {},
    "swing": {
        "BIAS_INTERVAL": "1day",
        "STRUCTURE_INTERVAL": "4h",
        "SETUP_INTERVAL": "1h",
        "ENTRY_INTERVAL": "15min",
        "BIAS_BARS": 120,
        "STRUCTURE_BARS": 220,
        "SETUP_BARS": 220,
        "ENTRY_BARS": 300,          # 15M x 300 = 75h of tracking history
        "MIN_SCORE": 9,
        "RISK_REWARD": 3.0,
        "USE_SESSION_FILTER": False,
        "STALE_SIGNAL_MIN": 50,     # run the cron hourly in swing mode
        "NEWS_WINDOW_MIN": 60,
        "MAX_SIGNALS_PER_DAY": 2,
        "COOLDOWN_MINUTES": 240,
        "TRADE_EXPIRY_HOURS": 60,
        "BRIEF_HOUR_UTC": 7,
    },
}

STRATEGY_MODE = os.environ.get("STRATEGY_MODE", "scalp").strip().lower()
if STRATEGY_MODE not in PRESETS:
    print(f"[WARN] unknown STRATEGY_MODE '{STRATEGY_MODE}', using scalp")
    STRATEGY_MODE = "scalp"
globals().update(PRESETS[STRATEGY_MODE])

STATE_FILE = Path(
    "ict_smc_state.json" if STRATEGY_MODE == "scalp"
    else f"ict_smc_state_{STRATEGY_MODE}.json"
)

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/3.0"})


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


def parse_iso(s):
    d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def parse_td_datetime(s):
    """Twelve Data gives 'YYYY-MM-DD HH:MM:SS' (intraday) or 'YYYY-MM-DD' (daily)."""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised datetime: {s}")


def in_weekend_block(ts):
    """Gold is closed from Friday night to Sunday night (UTC)."""
    wd = ts.weekday()
    h = ts.hour + ts.minute / 60
    if wd == 5:
        return True
    if wd == 6 and h < SUNDAY_OPEN_UTC:
        return True
    if wd == 4 and h >= FRIDAY_CUTOFF_UTC:
        return True
    return False


def news_events(state):
    """Manual blackouts + auto-fetched high-impact events, as dicts."""
    events = []
    for s in NEWS_BLACKOUTS_UTC:
        try:
            events.append({
                "dt": dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
                "title": "Manual news event",
            })
        except ValueError:
            print(f"[WARN] bad NEWS_BLACKOUTS_UTC entry: {s}")

    cache = (state or {}).get("news_cache") or {}
    for e in cache.get("events", []):
        try:
            events.append({"dt": parse_iso(e["t"]), "title": e.get("title", "High-impact news")})
        except (KeyError, ValueError):
            continue
    return events


def in_news_blackout(ts, state=None):
    """Returns the event title if `ts` is inside a blackout window, else None."""
    for ev in news_events(state):
        if abs((ts - ev["dt"]).total_seconds()) <= NEWS_WINDOW_MIN * 60:
            return ev["title"]
    return None


# ============================================================
# TWELVE DATA
# ============================================================

def interval_minutes(interval):
    if interval.endswith("min"):
        return int(interval[:-3])
    if interval.endswith("day"):
        return int(interval[:-3]) * 1440
    if interval.endswith("h"):
        return int(interval[:-1]) * 60
    raise ValueError(interval)


def tf_label(interval):
    m = interval_minutes(interval)
    if m % 1440 == 0:
        return f"{m // 1440}D"
    if m % 60 == 0:
        return f"{m // 60}H"
    return f"{m}M"


# Timeframe labels follow the active mode (scalp: 4H/30M/15M/5M, swing: 1D/4H/1H/15M)
LBL_BIAS = tf_label(BIAS_INTERVAL)
LBL_STRUCT = tf_label(STRUCTURE_INTERVAL)
LBL_SETUP = tf_label(SETUP_INTERVAL)
LBL_ENTRY = tf_label(ENTRY_INTERVAL)


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
        timestamp = parse_td_datetime(row["datetime"])

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
            reasons.append(f"{LBL_STRUCT} structure aligned")
        elif m30["bias"] != "NEUTRAL":
            conflicts.append(f"{LBL_STRUCT} structure conflict")

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
            reasons.append(f"{LBL_SETUP} CHoCH")
        elif m15["bos"] == wanted:
            trigger += 1
            break_ok = True
            reasons.append(f"{LBL_SETUP} BOS")

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
            reasons.append(f"{LBL_SETUP} EMA 9/15 aligned")

        pin_ok = (side == "BUY" and pin == "bullish") or (side == "SELL" and pin == "bearish")
        if pin_ok or ema5_state["state"] == wanted:
            confirm += 1
            reasons.append(f"{LBL_ENTRY} rejection candle" if pin_ok else f"{LBL_ENTRY} EMA 9/15 aligned")

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
        "close_dt": data15[-1]["dt"] + dt.timedelta(minutes=interval_minutes(SETUP_INTERVAL)),
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


def lot_text(lot):
    if not lot:
        return "n/a"
    if lot["lot"] is None:
        return f"below min lot ({lot['min_lot']}) for ${lot['risk_usd']:,.0f} risk"
    return f"<b>{lot['lot']:.2f}</b> lot  (risk ${lot['risk_usd']:,.0f} · {lot['basis']})"


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

    grade = signal.get("grade", "B")
    mode_tag = "" if STRATEGY_MODE == "scalp" else f" · {STRATEGY_MODE.upper()}"

    return (
        f"{icon} <b>{name} — {signal['side']} SETUP  [{grade}]</b>{mode_tag}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧭 Market Bias: <b>{result['bias']}</b>\n"
        f"⭐ Confluence: <b>{signal['score']}/{MAX_SCORE}</b> "
        f"(ctx {signal['context']}/5 · trig {signal['trigger']}/5 · conf {signal['confirm']}/2)\n\n"

        f"<b>ICT / SMC</b>\n"
        f"Liquidity Sweep: <b>{sweep_text}</b>\n"
        f"Order Block: <b>{ob_text}</b>\n"
        f"FVG: <b>{fvg_text}</b>\n"
        f"Premium/Discount: <b>{signal['pd']['zone']}</b>\n"
        f"{LBL_SETUP} BOS: <b>{signal['m15_structure']['bos'] or 'None'}</b>\n"
        f"{LBL_SETUP} CHoCH: <b>{signal['m15_structure']['choch'] or 'None'}</b>\n\n"

        f"<b>Confirmation</b>\n"
        f"{reasons}\n\n"

        f"<b>TRADE PLAN</b>\n"
        f"Entry: <b>{signal['entry']:.{digits}f}</b>\n"
        f"SL: <b>{signal['sl']:.{digits}f}</b>\n"
        f"TP1 ({TP1_R:g}R): <b>{signal['tp1']:.{digits}f}</b> "
        f"→ close {PARTIAL_CLOSE_PERCENT}% + SL to entry\n"
        f"TP ({RISK_REWARD:g}R): <b>{signal['tp']:.{digits}f}</b>\n"
        f"Risk: {signal['risk']:.{digits}f} ({signal['risk_atr']:.2f} x {LBL_SETUP} ATR)\n"
        f"Lot: {lot_text(signal.get('lot'))}\n\n"

        f"<b>{LBL_STRUCT} S/R</b>\n"
        f"Support: {level_text(support[0], digits) if support else '—'}\n"
        f"Resistance: {level_text(resistance[0], digits) if resistance else '—'}\n\n"

        f"<b>Conflicts</b>\n"
        f"{conflicts if conflicts else 'None'}\n\n"
        f"⏰ Closed {LBL_SETUP} candle: {signal['time']} UTC\n"
        f"⚠️ Rule-based market-reading alert, not a guarantee of price direction. "
        f"Lot size assumes the contract size set in SYMBOLS — verify it with your broker."
    )


# ============================================================
# ANALYSIS
# ============================================================

def fetch_candles(config, interval, bars, cache):
    """Per-run cache: the same (symbol, interval, bars) is only requested once,
    so tracking, brief and analysis share API calls."""
    key = (config["td_symbol"], interval, bars)
    if key not in cache:
        cache[key] = get_candles(config["td_symbol"], interval, bars)
    return cache[key]


def grade_for(score):
    if score >= GRADE_A_PLUS:
        return "A+"
    if score >= GRADE_A:
        return "A"
    return "B"


def lot_size(config, risk_distance):
    contract = config.get("contract_size")
    if not contract or risk_distance <= 0:
        return None

    if ACCOUNT_BALANCE > 0:
        risk_usd = ACCOUNT_BALANCE * RISK_PERCENT / 100
        basis = f"{RISK_PERCENT:g}% of ${ACCOUNT_BALANCE:,.0f}"
    else:
        risk_usd = REFERENCE_RISK_USD
        basis = "reference risk, set ACCOUNT_BALANCE"

    step = config.get("lot_step", 0.01)
    min_lot = config.get("min_lot", step)
    raw = risk_usd / (risk_distance * contract)
    lot = round(math.floor(raw / step + 1e-9) * step, 4)

    return {
        "risk_usd": risk_usd,
        "basis": basis,
        "raw": raw,
        "lot": lot if lot >= min_lot else None,
        "min_lot": min_lot,
    }


def volatility_state(data_setup):
    series = [x for x in atr(data_setup, ATR_PERIOD) if x]
    if len(series) < 20:
        return None
    ref = series[-(ATR_AVG_BARS + 1):-1]
    avg = sum(ref) / len(ref)
    if not avg:
        return None
    return {"atr": series[-1], "avg": avg, "ratio": series[-1] / avg}


def analyze_symbol(config, cache=None):
    cache = cache if cache is not None else {}
    name = config["name"]
    digits = config["digits"]

    data4h = fetch_candles(config, BIAS_INTERVAL, BIAS_BARS, cache)
    data30 = fetch_candles(config, STRUCTURE_INTERVAL, STRUCTURE_BARS, cache)
    data15 = fetch_candles(config, SETUP_INTERVAL, SETUP_BARS, cache)
    data5 = fetch_candles(config, ENTRY_INTERVAL, ENTRY_BARS, cache)

    if not data15:
        raise RuntimeError("No closed setup-timeframe candles")

    # Every timeframe is cut at the close of the last closed setup candle, so
    # the entry-TF confirmation and the setup candle refer to the same moment.
    cutoff = data15[-1]["dt"] + dt.timedelta(minutes=interval_minutes(SETUP_INTERVAL))
    data4h = align_to(data4h, BIAS_INTERVAL, cutoff)
    data30 = align_to(data30, STRUCTURE_INTERVAL, cutoff)
    data5 = align_to(data5, ENTRY_INTERVAL, cutoff)

    if min(len(data4h), len(data30), len(data15), len(data5)) < 60:
        raise RuntimeError("Not enough closed candles")

    bias_data = get_bias(data4h, data30)
    result = score_setup(bias_data, data30, data15, data5)
    result["filter_reason"] = None

    signal = result.get("signal")
    if signal:
        plan = make_trade_plan(signal, data15, digits)
        reason = None

        if plan is None:
            reason = "SL distance outside the allowed ATR range"
        else:
            vol = volatility_state(data15)
            if USE_VOLATILITY_FILTER and vol:
                if vol["ratio"] < MIN_ATR_RATIO:
                    reason = f"volatility too low (ATR x{vol['ratio']:.2f} of average)"
                elif vol["ratio"] > MAX_ATR_RATIO:
                    reason = f"volatility spike (ATR x{vol['ratio']:.2f} of average)"

            spread = config.get("typical_spread")
            if not reason and spread and plan["risk"] < spread * MIN_RISK_TO_SPREAD:
                reason = (
                    f"SL too tight vs spread "
                    f"(risk {plan['risk']:.{digits}f}, needs at least {MIN_RISK_TO_SPREAD:g} x {spread})"
                )

        if reason:
            result["signal"] = None
            result["filter_reason"] = reason
        else:
            plan["grade"] = grade_for(plan["score"])
            sign = 1 if plan["side"] == "BUY" else -1
            plan["tp1"] = plan["entry"] + sign * plan["risk"] * TP1_R
            plan["lot"] = lot_size(config, plan["risk"])
            result["signal"] = plan

    return {
        "name": name,
        "price": data15[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data15[-1]["time"],
        "data15": data15,
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
    reason = result.get("filter_reason")

    reason_line = f"Filter: {html.escape(str(reason))}\n" if reason else ""
    footer = (
        "A setup was found but blocked by a filter. (Hourly status — bot is alive.)"
        if reason else
        "Confluence threshold not reached. (Hourly status — bot is alive.)"
    )

    return (
        f"🟡 <b>{name} — MARKET READER</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b>\n"
        f"Bias: <b>{result.get('bias', 'UNKNOWN')}</b>\n"
        f"{LBL_SETUP} BOS: {m15.get('bos') or 'None'}\n"
        f"{LBL_SETUP} CHoCH: {m15.get('choch') or 'None'}\n"
        f"Liquidity Sweep: {sweep['side'] if sweep else 'None'}\n"
        f"Order Block: {ob['side'] if ob else 'None'}\n"
        f"FVG: {fvg['side'] if fvg else 'None'}\n"
        f"P/D: {pd['zone'] if pd else 'UNKNOWN'}\n"
        f"{reason_line}\n"
        f"<b>ACTION: NO TRADE</b>\n"
        f"{footer}"
    )


def redact(text):
    for secret in (TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def maybe_send_error(state, name, exc, ts):
    """Silence must never hide a failure: at most one error alert per symbol per hour."""
    hour_key = ts.strftime("%Y-%m-%d %H")
    state_key = f"error:{name}"
    if state.get(state_key) == hour_key:
        return

    detail = html.escape(redact(str(exc)), quote=False)[:350]
    send_telegram(
        f"⚠️ <b>{name} — BOT ERROR</b>\n"
        f"{detail}\n"
        f"No new signals for {name} until this is fixed."
    )
    state[state_key] = hour_key
    save_state(state)


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
        "✅ <b>ICT SMC Market Reader</b>\n"
        "Telegram connection works.\n"
        f"Time: {now_utc().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    print("Test message sent")


# ============================================================
# TELEGRAM PHOTO
# ============================================================

def send_telegram_photo(png_bytes, caption=""):
    require_credentials()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    response = SESSION.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption[:1000],
            "parse_mode": "HTML",
        },
        files={"photo": ("chart.png", png_bytes, "image/png")},
        timeout=40,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected photo: {result}")

    return True


# ============================================================
# AUTO NEWS  (high-impact events -> blackout windows)
# ============================================================

def refresh_news(state, ts):
    """Refreshes the cached calendar at most every NEWS_REFRESH_HOURS.
    On any failure the old cache (and the manual list) keep working."""
    if not USE_AUTO_NEWS:
        return

    cache = state.get("news_cache") or {}
    next_try = cache.get("next_try")
    if next_try and ts < parse_iso(next_try):
        return

    try:
        response = SESSION.get(NEWS_FEED_URL, timeout=20)
        response.raise_for_status()
        raw = response.json()

        events = []
        for e in raw:
            impact = str(e.get("impact", "")).strip().lower()
            ccy = e.get("country") or e.get("currency")
            if impact != "high" or ccy not in NEWS_CURRENCIES:
                continue
            events.append({
                "t": parse_iso(e["date"]).isoformat(),
                "title": str(e.get("title", "High-impact news")),
            })

        state["news_cache"] = {
            "fetched": ts.isoformat(),
            "next_try": (ts + dt.timedelta(hours=NEWS_REFRESH_HOURS)).isoformat(),
            "events": events,
        }
        print(f"[news] {len(events)} high-impact {'/'.join(NEWS_CURRENCIES)} events loaded")
    except Exception as exc:
        print(f"[news] refresh failed, keeping old cache: {exc}")
        cache["next_try"] = (ts + dt.timedelta(hours=1)).isoformat()
        cache.setdefault("events", [])
        state["news_cache"] = cache

    save_state(state)


# ============================================================
# TRADE TRACKING  (TP1 alert, TP / SL / EXPIRED)
# ============================================================

def get_open(state):
    return state.get("open_trades", [])


def get_closed(state):
    return state.get("closed_trades", [])


def register_trade(state, signal, config, ts):
    if not TRACK_TRADES:
        return

    state.setdefault("open_trades", []).append({
        "id": f"{config['name']}-{signal['time']}-{signal['side']}",
        "symbol": config["name"],
        "side": signal["side"],
        "grade": signal.get("grade", "B"),
        "score": signal["score"],
        "entry": signal["entry"],
        "sl": signal["sl"],
        "tp": signal["tp"],
        "tp1": signal["tp1"],
        "risk": signal["risk"],
        "digits": config["digits"],
        "entry_dt": signal["close_dt"].isoformat(),
        "sent_dt": ts.isoformat(),
        "tp1_hit": False,
        "checked_until": None,
    })


def r_multiple(trade, price):
    move = price - trade["entry"] if trade["side"] == "BUY" else trade["entry"] - price
    return move / trade["risk"]


def evaluate_trade(t, candles, ts):
    """Walks candles that closed after the last check.
    Returns (result, exit_price, close_dt) or (None, None, None) while open.
    If SL and TP are touched inside the same candle, SL is assumed first
    (conservative)."""
    entry_dt = parse_iso(t["entry_dt"])
    last = parse_iso(t["checked_until"]) if t.get("checked_until") else None
    step = dt.timedelta(minutes=interval_minutes(ENTRY_INTERVAL))
    buy = t["side"] == "BUY"

    for c in candles:
        if c["dt"] < entry_dt:
            continue
        if last and c["dt"] <= last:
            continue

        if buy:
            hit_sl, hit_tp, hit_tp1 = c["low"] <= t["sl"], c["high"] >= t["tp"], c["high"] >= t["tp1"]
        else:
            hit_sl, hit_tp, hit_tp1 = c["high"] >= t["sl"], c["low"] <= t["tp"], c["low"] <= t["tp1"]

        t["checked_until"] = c["dt"].isoformat()

        if hit_sl:
            return "SL", t["sl"], c["dt"] + step
        if hit_tp:
            t["tp1_hit"] = True
            return "TP", t["tp"], c["dt"] + step
        if hit_tp1 and not t.get("tp1_hit"):
            t["tp1_hit"] = True
            t["tp1_alert_pending"] = True

    if ts - entry_dt > dt.timedelta(hours=TRADE_EXPIRY_HOURS) and candles:
        return "EXPIRED", candles[-1]["close"], ts

    return None, None, None


def daily_summary(state, ts):
    day = ts.date()
    todays = [r for r in get_closed(state) if parse_iso(r["close_dt"]).date() == day]
    total_r = sum(r["r"] for r in todays)
    wins = sum(1 for r in todays if r["r"] > 0)
    losses = sum(1 for r in todays if r["r"] < 0)
    return len(todays), total_r, wins, losses


def build_tp1_message(t):
    d = t["digits"]
    return (
        f"🔔 <b>{t['symbol']} {t['side']} — TP1 reached (+{TP1_R:g}R)</b>\n"
        f"Price touched {t['tp1']:.{d}f}.\n"
        f"Suggested: close {PARTIAL_CLOSE_PERCENT}% and move SL to entry "
        f"({t['entry']:.{d}f}).\n"
        f"Final target: {t['tp']:.{d}f}\n"
        f"⚠️ Management suggestion only — your trade, your decision."
    )


def build_close_message(rec, state, ts):
    d = rec["digits"]
    icon = {"TP": "✅", "SL": "❌", "EXPIRED": "⏱"}[rec["result"]]
    title = {"TP": "TAKE PROFIT HIT", "SL": "STOP LOSS HIT", "EXPIRED": "TRADE EXPIRED"}[rec["result"]]
    n, day_r, wins, losses = daily_summary(state, ts)

    tp1_line = "TP1 was reached first\n" if rec.get("tp1_hit") and rec["result"] != "TP" else ""

    return (
        f"{icon} <b>{rec['symbol']} {rec['side']} [{rec['grade']}] — {title}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Entry: {rec['entry']:.{d}f}  →  Exit: {rec['exit']:.{d}f}\n"
        f"Result: <b>{rec['r']:+.2f}R</b> (original plan, SL not moved)\n"
        f"{tp1_line}"
        f"Today: <b>{day_r:+.2f}R</b> ({wins}W / {losses}L, {n} closed)"
    )


def track_symbol(state, config, cache, ts):
    if not TRACK_TRADES:
        return

    name = config["name"]
    mine = [t for t in get_open(state) if t["symbol"] == name]
    if not mine:
        return

    candles = fetch_candles(config, ENTRY_INTERVAL, ENTRY_BARS, cache)

    for t in mine:
        result, price, close_dt = evaluate_trade(t, candles, ts)

        if result:
            rec = {k: t[k] for k in (
                "id", "symbol", "side", "grade", "score", "entry", "sl", "tp",
                "risk", "digits", "entry_dt", "sent_dt",
            )}
            rec.update({
                "result": result,
                "exit": price,
                "r": round(r_multiple(t, price), 2),
                "close_dt": close_dt.isoformat(),
                "tp1_hit": bool(t.get("tp1_hit")),
            })

            state["open_trades"] = [x for x in get_open(state) if x["id"] != t["id"]]
            history = get_closed(state) + [rec]
            state["closed_trades"] = history[-MAX_CLOSED_HISTORY:]
            save_state(state)

            send_telegram(build_close_message(rec, state, ts))
            print(f"[{name}] trade closed: {result} {rec['r']:+.2f}R")

        elif t.pop("tp1_alert_pending", False):
            save_state(state)
            send_telegram(build_tp1_message(t))
            print(f"[{name}] TP1 alert sent")
        else:
            save_state(state)


# ============================================================
# DAILY SAFETY GATE
# ============================================================

def risk_gate(state, ts):
    """Returns a reason string if new signals must be paused, else None."""
    today = ts.date()

    sent_today = sum(
        1 for t in get_open(state) + get_closed(state)
        if parse_iso(t.get("sent_dt") or t["entry_dt"]).date() == today
    )
    if MAX_SIGNALS_PER_DAY and sent_today >= MAX_SIGNALS_PER_DAY:
        return f"daily signal limit reached ({sent_today}/{MAX_SIGNALS_PER_DAY})"

    closed = sorted(get_closed(state), key=lambda r: r["close_dt"])

    day_r = sum(r["r"] for r in closed if parse_iso(r["close_dt"]).date() == today)
    if MAX_DAILY_LOSS_R and day_r <= -MAX_DAILY_LOSS_R:
        return f"daily loss limit reached ({day_r:+.1f}R)"

    streak = 0
    for rec in reversed(closed):
        if rec["result"] == "SL":
            streak += 1
        else:
            break

    if MAX_CONSECUTIVE_LOSSES and streak >= MAX_CONSECUTIVE_LOSSES:
        until = parse_iso(closed[-1]["close_dt"]) + dt.timedelta(minutes=COOLDOWN_MINUTES)
        if ts < until:
            return f"cooldown after {streak} losses in a row (until {until:%H:%M} UTC)"

    return None


# ============================================================
# WEEKLY REPORT
# ============================================================

def stats_block(trades):
    n = len(trades)
    if n == 0:
        return "No closed trades."

    wins = sum(1 for t in trades if t["r"] > 0)
    losses = sum(1 for t in trades if t["r"] < 0)
    flat = n - wins - losses
    total_r = sum(t["r"] for t in trades)
    reached_tp1 = sum(1 for t in trades if t.get("tp1_hit"))
    best = max(t["r"] for t in trades)
    worst = min(t["r"] for t in trades)

    return (
        f"Trades: <b>{n}</b>  ({wins}W / {losses}L / {flat} flat)\n"
        f"Win rate: <b>{wins / n * 100:.0f}%</b>\n"
        f"Total: <b>{total_r:+.2f}R</b>  ·  Avg: {total_r / n:+.2f}R\n"
        f"Best {best:+.2f}R  ·  Worst {worst:+.2f}R\n"
        f"Reached TP1: {reached_tp1}/{n}"
    )


def group_lines(trades, key, order=None):
    groups = {}
    for t in trades:
        groups.setdefault(t[key], []).append(t)

    keys = order if order else sorted(groups)
    lines = []
    for k in keys:
        g = groups.get(k)
        if not g:
            continue
        wins = sum(1 for t in g if t["r"] > 0)
        lines.append(f"{k}: {len(g)} trade{'s' if len(g) != 1 else ''} · {wins / len(g) * 100:.0f}% win · {sum(t['r'] for t in g):+.2f}R")
    return "\n".join(lines) if lines else "—"


def build_weekly_report(state, ts):
    iso = ts.isocalendar()[:2]
    week = [r for r in get_closed(state) if parse_iso(r["close_dt"]).isocalendar()[:2] == iso]
    allt = get_closed(state)

    msg = (
        f"📊 <b>WEEKLY REPORT — {iso[0]}-W{iso[1]:02d}</b>  ({STRATEGY_MODE})\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{stats_block(week)}\n"
    )
    if week:
        msg += (
            f"\n<b>By symbol</b>\n{group_lines(week, 'symbol')}\n"
            f"\n<b>By grade</b>\n{group_lines(week, 'grade', ['A+', 'A', 'B'])}\n"
        )

    msg += f"\nStill open: {len(get_open(state))}"
    if len(allt) > len(week):
        total_r = sum(r["r"] for r in allt)
        wins = sum(1 for r in allt if r["r"] > 0)
        msg += (
            f"\n<b>All-time</b> ({len(allt)} trades): "
            f"{wins / len(allt) * 100:.0f}% win · {total_r:+.2f}R"
        )
    msg += "\n⚠️ R is measured on the original plan (SL not moved to breakeven)."
    return msg


def maybe_send_weekly_report(state, ts):
    if not SEND_WEEKLY_REPORT:
        return

    wd = ts.weekday()
    due = wd in (5, 6) or (wd == 4 and ts.hour >= 21)
    if not due:
        return

    iso = ts.isocalendar()
    key = f"{iso[0]}-W{iso[1]:02d}"
    if state.get("weekly_report") == key:
        return

    send_telegram(build_weekly_report(state, ts))
    state["weekly_report"] = key
    save_state(state)
    print("[report] weekly report sent")


# ============================================================
# CHART IMAGE
# ============================================================

def build_chart_png(name, signal, data15, digits):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception as exc:
        print(f"[chart] matplotlib not available, skipped: {exc}")
        return None

    candles = data15[-CHART_BARS:]
    n = len(candles)
    if n < 10:
        return None

    offset = len(data15) - n
    closes = [c["close"] for c in data15]
    nan = float("nan")
    e_fast = [v if v is not None else nan for v in ema(closes, EMA_FAST)[offset:]]
    e_slow = [v if v is not None else nan for v in ema(closes, EMA_SLOW)[offset:]]

    bg, fg = "#0f1419", "#cfd8dc"
    up, down = "#26a69a", "#ef5350"
    right = n + 9

    fig, ax = plt.subplots(figsize=(10, 6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    for i, c in enumerate(candles):
        col = up if c["close"] >= c["open"] else down
        ax.plot([i, i], [c["low"], c["high"]], color=col, linewidth=1, zorder=2)
        body_low = min(c["open"], c["close"])
        height = max(abs(c["close"] - c["open"]), (c["high"] - c["low"]) * 0.02)
        ax.add_patch(Rectangle((i - 0.32, body_low), 0.64, height,
                               facecolor=col, edgecolor=col, zorder=3))

    ax.plot(range(n), e_fast, color="#ffd54f", linewidth=1.1, label=f"EMA {EMA_FAST}", zorder=4)
    ax.plot(range(n), e_slow, color="#4fc3f7", linewidth=1.1, label=f"EMA {EMA_SLOW}", zorder=4)

    # OB / FVG zones
    index_by_time = {c["time"]: i for i, c in enumerate(candles)}
    for zone, color, label in ((signal.get("ob"), "#42a5f5", "OB"),
                               (signal.get("fvg"), "#ab47bc", "FVG")):
        if not zone:
            continue
        start = index_by_time.get(zone["time"], 0)
        ax.add_patch(Rectangle((start, zone["low"]), right - start, zone["high"] - zone["low"],
                               facecolor=color, alpha=0.18, edgecolor=color, linewidth=0.8, zorder=1))
        ax.text(start + 0.5, zone["high"], f"{label} {zone['side'].lower()}",
                color=color, fontsize=8, va="bottom", zorder=5)

    # liquidity sweep level
    sweep = signal.get("sweep")
    if sweep:
        ax.hlines(sweep["price"], -1, right, colors="#ffa726", linewidth=0.9, linestyles=":", zorder=2)
        ax.text(0.5, sweep["price"], f"sweep {sweep['level'].lower()}",
                color="#ffa726", fontsize=8, va="bottom", zorder=5)

    # trade plan
    entry, sl, tp, tp1 = signal["entry"], signal["sl"], signal["tp"], signal["tp1"]
    x0 = n - 1
    ax.add_patch(Rectangle((x0, min(entry, sl)), right - x0, abs(entry - sl),
                           facecolor=down, alpha=0.22, zorder=1))
    ax.add_patch(Rectangle((x0, min(entry, tp)), right - x0, abs(tp - entry),
                           facecolor=up, alpha=0.22, zorder=1))
    for price, label, color in ((entry, "ENTRY", "#eceff1"), (sl, "SL", down),
                                (tp1, "TP1", "#80cbc4"), (tp, "TP", up)):
        ax.hlines(price, -1, right, colors=color, linewidth=0.9, linestyles="--", zorder=4)
        ax.text(right + 0.3, price, f"{label} {price:.{digits}f}",
                color=color, fontsize=8, va="center", zorder=5)

    lows = [c["low"] for c in candles] + [sl, tp, entry]
    highs = [c["high"] for c in candles] + [sl, tp, entry]
    pad = (max(highs) - min(lows)) * 0.05
    ax.set_ylim(min(lows) - pad, max(highs) + pad)
    ax.set_xlim(-1, right + 12)

    ticks = sorted(set(int(round(x)) for x in
                       [i * (n - 1) / 5 for i in range(6)]))
    ax.set_xticks(ticks)
    ax.set_xticklabels([candles[i]["time"][5:16] for i in ticks], fontsize=8)

    ax.tick_params(colors=fg, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#37474f")
    ax.grid(color="#263238", linewidth=0.5, alpha=0.6)
    ax.legend(loc="upper left", fontsize=8, facecolor=bg, edgecolor="#37474f", labelcolor=fg)
    ax.set_title(
        f"{name} {signal['side']} [{signal.get('grade', 'B')}] · score {signal['score']}/{MAX_SCORE} · {LBL_SETUP}",
        color=fg, fontsize=11,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ============================================================
# MORNING BRIEF
# ============================================================

def fmt_price(x, digits):
    return "—" if x is None else f"{x:.{digits}f}"


def build_morning_brief(config, state, cache, ts):
    name = config["name"]
    digits = config["digits"]

    data_b = fetch_candles(config, BIAS_INTERVAL, BIAS_BARS, cache)
    data_s = fetch_candles(config, STRUCTURE_INTERVAL, STRUCTURE_BARS, cache)
    data_d = fetch_candles(config, BRIEF_INTERVAL, BRIEF_BARS, cache)

    if len(data_b) < 30 or len(data_s) < 40 or not data_d:
        raise RuntimeError("Not enough candles for the brief")

    bias = get_bias(data_b, data_s)
    pd = premium_discount(data_s)
    sr = build_sr(data_s)
    price = data_d[-1]["close"]

    today = ts.date()
    by_day = {}
    for c in data_d:
        by_day.setdefault(c["dt"].date(), []).append(c)

    prev_days = sorted(d for d in by_day if d < today)
    prev = by_day[prev_days[-1]] if prev_days else []
    pdh = max((c["high"] for c in prev), default=None)
    pdl = min((c["low"] for c in prev), default=None)

    asian = [c for c in by_day.get(today, [])
             if ASIAN_RANGE_UTC[0] <= c["dt"].hour < ASIAN_RANGE_UTC[1]]
    a_hi = max((c["high"] for c in asian), default=None)
    a_lo = min((c["low"] for c in asian), default=None)

    support = nearest_level(sr["support"], price)
    resistance = nearest_level(sr["resistance"], price)

    todays_news = sorted(
        (ev for ev in news_events(state) if ev["dt"].date() == today),
        key=lambda ev: ev["dt"],
    )
    news_lines = "\n".join(
        f"• {ev['dt']:%H:%M} UTC — {html.escape(ev['title'])}" for ev in todays_news
    ) or "None (high-impact USD)"

    zones = ""
    if USE_SESSION_FILTER and config.get("session_filter"):
        zones = "Kill zones (UTC): " + ", ".join(f"{a:02d}:00–{b:02d}:00" for a, b in SESSIONS_UTC) + "\n"

    return (
        f"🌅 <b>{name} — MORNING BRIEF</b>  ({STRATEGY_MODE})\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b>\n"
        f"Bias: <b>{bias['bias']}</b>  "
        f"({LBL_BIAS} {bias['4h_structure']['bias']} · {LBL_STRUCT} {bias['30m_structure']['bias']})\n"
        f"Zone: <b>{pd['zone']}</b>\n\n"
        f"<b>Key levels</b>\n"
        f"Prev day high: {fmt_price(pdh, digits)}\n"
        f"Prev day low: {fmt_price(pdl, digits)}\n"
        f"Asian range: {fmt_price(a_lo, digits)} – {fmt_price(a_hi, digits)}\n"
        f"{LBL_STRUCT} support: {level_text(support, digits) if support else '—'}\n"
        f"{LBL_STRUCT} resistance: {level_text(resistance, digits) if resistance else '—'}\n\n"
        f"<b>News today</b>\n{news_lines}\n\n"
        f"{zones}"
        f"Watch for sweeps of the levels above, then a {LBL_SETUP} break in the bias direction."
    )


def maybe_send_brief(state, config, cache, ts):
    if not SEND_MORNING_BRIEF:
        return
    if not (BRIEF_HOUR_UTC <= ts.hour < BRIEF_HOUR_UTC + BRIEF_WINDOW_HOURS):
        return

    name = config["name"]
    key = f"brief:{name}"
    today = ts.strftime("%Y-%m-%d")
    if state.get(key) == today:
        return

    send_telegram(build_morning_brief(config, state, cache, ts))
    state[key] = today
    save_state(state)
    print(f"[{name}] morning brief sent")


# ============================================================
# MAIN
# ============================================================

def run_once():
    state = load_state()
    cache = {}  # per-run candle cache shared by tracking, brief and analysis

    try:
        refresh_news(state, now_utc())
    except Exception as exc:
        print(f"[news] ERROR: {exc}")

    # 1) Track open trades first — always, even outside kill zones.
    for config in SYMBOLS:
        try:
            track_symbol(state, config, cache, now_utc())
        except Exception as exc:
            print(f"[{config['name']}] TRACK ERROR: {exc}")

    # 2) Weekly report (Friday night / weekend, once per ISO week)
    try:
        maybe_send_weekly_report(state, now_utc())
    except Exception as exc:
        print(f"[report] ERROR: {exc}")

    # 3) Scan for new setups
    for config in SYMBOLS:
        name = config["name"]

        try:
            ts = now_utc()

            if config.get("weekend_filter") and in_weekend_block(ts):
                print(f"[{name}] weekend / market closed, skipped")
                continue

            # Morning brief never blocks the scan if it fails.
            try:
                maybe_send_brief(state, config, cache, ts)
            except Exception as exc:
                print(f"[{name}] BRIEF ERROR: {exc}")

            # Filters run BEFORE any analysis call, which also saves quota.
            if USE_SESSION_FILTER and config.get("session_filter") and not in_session(ts):
                print(f"[{name}] outside kill zone, skipped")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nOutside kill zone — scanning paused. (Bot is alive.)",
                    ts,
                )
                continue

            news_title = in_news_blackout(ts, state)
            if news_title:
                print(f"[{name}] news blackout ({news_title}), skipped")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nNews blackout: {html.escape(news_title)} — "
                    f"scanning paused. (Bot is alive.)",
                    ts,
                )
                continue

            gate_reason = risk_gate(state, ts)
            if gate_reason:
                print(f"[{name}] risk gate: {gate_reason}")
                maybe_send_status(
                    state, name,
                    f"🛑 <b>{name}</b>\nNew signals paused: {gate_reason}. (Bot is alive.)",
                    ts,
                )
                continue

            open_here = sum(1 for t in get_open(state) if t["symbol"] == name)
            if MAX_OPEN_PER_SYMBOL and open_here >= MAX_OPEN_PER_SYMBOL:
                print(f"[{name}] trade still open, no new scan")
                continue

            data = analyze_symbol(config, cache)
            result = data["result"]
            signal = result.get("signal")

            if not signal:
                print(
                    f"[{name}] NO TRADE | "
                    f"bias={result.get('bias')} | "
                    f"price={data['price']}"
                    + (f" | filter={result['filter_reason']}" if result.get("filter_reason") else "")
                )
                maybe_send_status(
                    state, name,
                    build_no_trade_message(name, result, data["price"], data["digits"]),
                    ts,
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

            send_telegram(build_signal_message(name, signal, result, data["digits"]))

            # Persist BEFORE the chart, so a chart failure can never cause a duplicate.
            state[name] = key
            register_trade(state, signal, config, now_utc())
            save_state(state)

            print(f"[{name}] SENT {signal['side']} [{signal.get('grade')}] score={signal['score']}/{MAX_SCORE}")

            if SEND_CHART:
                try:
                    png = build_chart_png(name, signal, data["data15"], data["digits"])
                    if png:
                        send_telegram_photo(
                            png,
                            f"{name} {signal['side']} [{signal.get('grade')}] — chart",
                        )
                except Exception as exc:
                    print(f"[{name}] chart error: {exc}")

        except Exception as exc:
            print(f"[{name}] ERROR: {redact(str(exc))}")
            try:
                maybe_send_error(state, name, exc, now_utc())
            except Exception as send_exc:
                print(f"[{name}] could not send error alert: {redact(str(send_exc))}")

    save_state(state)


def main():
    require_credentials()

    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        send_test_message()
        return

    if "--report" in sys.argv:
        send_telegram(build_weekly_report(load_state(), now_utc()))
        print("Report sent")
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
