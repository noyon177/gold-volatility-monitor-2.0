import os
import sys
import json
import math
import time
import html
import statistics
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC SCALP READER  v8.0
#
# What is new in v8 (on top of v7)
#   * H1 supply / demand zones: base candle + strong impulse, still unbroken,
#     retest count and quality (STRONG / MEDIUM / WEAK) are tracked.
#   * ZONE CONFLICT GATE (two layers): a BUY while price is inside an H1 supply
#     zone, or a SELL while price is inside an H1 demand zone, is blocked
#     (1) inside the scoring engine and (2) again right before Telegram, using
#     the entry price and the live quote.
#   * Strategy mode (ZONE_REQUIRE=1): entry must sit at an aligned zone
#     (BUY at demand, SELL at supply). Optional ZONE_NEED_PIN=1 also demands a
#     5M pinbar. ZONE_ON=0 switches the whole zone layer off.
#   * Opposite zones count as strong liquidity: TP2 stops before them and a
#     trade with no room before an opposing zone is skipped.
#   * Learning: zone quality and zone freshness are saved with every trade and
#     feed the existing learning filter, so the bot stops trading zone types
#     that keep losing (after enough trades).
#
# What is new vs v5.1
#   * Catch-up scan: if GitHub cron starts late, the last few 5M candles are
#     re-checked, so a late run no longer loses the signal. Validity is judged
#     by price (SL / TP1 / limit already touched, max-chase), not by the clock.
#   * Liquidity-based targets: TP2 = next real liquidity (PDH/PDL, Asia range,
#     1H/15M swings, equal highs/lows). Trade is skipped if a strong level is
#     too close to entry (no room to run).
#   * Smart entry: LIMIT order at the 50% of the 5M OB/FVG instead of chasing the
#     candle close (smaller SL, better RR). Fill / cancel is tracked.
#   * Trailing: after TP1 -> SL to breakeven; at the TP1->TP2 midpoint -> SL to TP1.
#   * Volatility filter (dead / spike market), real spread cost in R,
#     spread-vs-SL filter.
#   * Self-adjusting: min score rises after a losing streak, a losing session is
#     paused (30-day rolling, needs enough trades).
#   * Automatic high-impact USD news blackout (free calendar, manual list still works).
#   * Lot size from account balance + risk %.
#   * Twelve Data credit counter + daily budget guard, error alert, Telegram retry.
#   * Dead SWING mode removed. Backtest lives in smc_backtest.py.
#
# Every setting below can be changed with an environment variable
# (GitHub: Settings -> Secrets and variables -> Actions -> Variables).
#
# Secrets: TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# ============================================================

VERSION = "8.0"
UTC = dt.timezone.utc


def _num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _flag(name, default="1"):
    v = os.environ.get(name, "")
    return (default if v == "" else v) == "1"


def _env(name, default):
    return os.environ.get(name) or default


SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3, "weekend_closed": True,
     "spread": _num("XAU_SPREAD", 0.30),             # price units, set to your broker's typical spread
     "value_per_lot": _num("XAU_VALUE_PER_LOT", 100.0)},   # $ per 1.00 price move per 1 lot
    {"name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 3,
     "spread": _num("BTC_SPREAD", 15.0),
     "value_per_lot": _num("BTC_VALUE_PER_LOT", 1.0)},
]

# ---------------- INDICATORS / ICT-SMC PRIMITIVES ----------------
EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
STRUCTURE_LOOKBACK = 80
LIQUIDITY_LOOKBACK = 60
FVG_LOOKBACK = 50
OB_LOOKBACK = 60

DISPLACEMENT_BODY_RATIO = 0.65
DISPLACEMENT_ATR_MULT = 1.15
DISPLACEMENT_WINDOW = 3
FVG_MIN_ATR = 0.10

SWEEP_WINDOW = 6
SWEEP_REF_BARS = 20
SWEEP_MIN_DEPTH_ATR = 0.02
SWEEP_MAX_DEPTH_ATR = 1.50
SWEEP_MAX_AGE = 3                 # sweep must be recent (closed 5M candles)
BREAK_WINDOW = 6

RATE_LIMIT_WAIT = 20  # seconds to wait once if Twelve Data returns 429

STATE_FILE = Path(os.environ.get("STATE_FILE", "ict_smc_state.json"))

# ---------------- SCALP SETTINGS ----------------
SCALP_BARS = 1000                    # 5M candles per download (~83 h)
SCALP_MAX_SCORE = 12
SCALP_MIN_SCORE = _num("SCALP_MIN_SCORE", 7, int)
SCALP_WATCH_MIN_SCORE = _num("SCALP_WATCH_MIN_SCORE", 6, int)
SCALP_MIN_TRIGGER = 2
SCALP_MIN_CONTEXT = 1
SCALP_MIN_CONFIRM = 1
SCALP_PULLBACK_ATR = 0.15

SCALP_SL_LOOKBACK = 8
SCALP_SL_BUFFER_ATR = 0.10
SCALP_MIN_SL_ATR = 0.80
SCALP_MAX_SL_ATR = 2.50

SCALP_TP1_R = 1.0
SCALP_TP2_R = _num("SCALP_TP2_R", 2.0)             # fallback when no liquidity target is found
SCALP_TP2_MIN_R = _num("SCALP_TP2_MIN_R", 2.0)     # TP2 = first liquidity level at least this far
SCALP_MAX_TP2_R = _num("SCALP_MAX_TP2_R", 4.0)     # cap for TP2
SCALP_TP_BUFFER_ATR = 0.05                         # TP placed a little before the level
SCALP_MIN_ROOM_R = _num("SCALP_MIN_ROOM_R", 1.5)   # strong level closer than this -> skip (0 = off)
SCALP_MAX_CHASE_ATR = 0.30

SCALP_COOLDOWN_MIN = _num("SCALP_COOLDOWN_MIN", 15, int)     # per symbol + side
SCALP_WATCH_COOLDOWN_MIN = 30
SCALP_MAX_SIGNALS_PER_DAY = _num("SCALP_MAX_SIGNALS_PER_DAY", 8, int)

# ---------------- H1 SUPPLY / DEMAND ZONES (v8) ----------------
ZONE_ON = _flag("ZONE_ON")                         # master switch for the whole zone layer
ZONE_REQUIRE = _flag("ZONE_REQUIRE")               # 1 = entry must sit at an aligned H1 zone
ZONE_NEED_PIN = _flag("ZONE_NEED_PIN", "0")        # 1 = a 5M pinbar is mandatory too
ZONE_LOOKBACK = _num("ZONE_LOOKBACK", 80, int)     # H1 candles scanned for zones
ZONE_IMPULSE_ATR = _num("ZONE_IMPULSE_ATR", 1.2)   # impulse body >= this x H1 ATR
ZONE_IMPULSE_BODY = 0.60                           # ... and body / range >= this
ZONE_MAX_HEIGHT_ATR = _num("ZONE_MAX_HEIGHT_ATR", 1.6)
ZONE_MAX_TOUCHES = _num("ZONE_MAX_TOUCHES", 3, int)  # more retests than this = exhausted zone
ZONE_TOL_H1_ATR = 0.10                             # price tolerance around a zone (x H1 ATR)
ZONE_TOUCH_BARS = 6                                # recent 5M candles that may have tapped the zone
ZONE_MAX_KEEP = 8

SCALP_CATCHUP_CANDLES = _num("SCALP_CATCHUP_CANDLES", 2, int)  # re-check this many older 5M candles
SCALP_STALE_MIN = _num("SCALP_STALE_MIN", 5 * SCALP_CATCHUP_CANDLES + 6, int)

SCALP_LOOP_MINUTES = _num("SCALP_LOOP_MINUTES", 0, int)
SCALP_FETCH_DELAY_SEC = 25
SCALP_FETCH_RETRIES = 2
SCALP_RETRY_WAIT_SEC = 10

# ---------------- SMART ENTRY (limit at OB / FVG mid) ----------------
SMART_ENTRY = _flag("SMART_ENTRY")
SMART_ENTRY_MIN_ATR = 0.20      # zone mid must be at least this far from price
SMART_ENTRY_MAX_ATR = 1.20      # ... and not farther than this
SMART_MIN_RISK_ATR = 0.50       # limit-entry risk must stay at least this big
LIMIT_EXPIRY_MIN = _num("LIMIT_EXPIRY_MIN", 45, int)

# ---------------- COST / VOLATILITY ----------------
MAX_COST_R = _num("MAX_COST_R", 0.25)            # spread / risk above this -> skip trade
TRADE_COST_R = _num("TRADE_COST_R", 0.0)         # extra flat cost per trade, in R
VOL_LOOKBACK = 288                               # ATR is compared with its 1-day median
VOL_MIN_RATIO = _num("VOL_MIN_RATIO", 0.45)      # below -> dead market (0 = off)
VOL_MAX_RATIO = _num("VOL_MAX_RATIO", 3.0)       # above -> news spike (0 = off)

# ---------------- TRADE TRACKING ----------------
TRADE_MAX_AGE_HOURS = _num("TRADE_MAX_AGE_HOURS", 6, int)
TRADE_TP1_PORTION = 0.5
TRAIL_TRIGGER_FRAC = 0.5          # SL -> TP1 once price covers this part of TP1->TP2
MAX_CLOSED_HISTORY = 1000
DAILY_SUMMARY_HOUR_UTC = _num("DAILY_SUMMARY_HOUR_UTC", 21, int)
WEEKLY_REPORT_WEEKDAY = 6         # Sunday
WEEKLY_REPORT_HOUR_UTC = _num("WEEKLY_REPORT_HOUR_UTC", 18, int)

SL_STREAK_PAUSE = _num("SL_STREAK_PAUSE", 2, int)   # losses in a row -> pause (0 = off)
SL_PAUSE_HOURS = _num("SL_PAUSE_HOURS", 3.0)
STATUS_EVERY_HOURS = max(1, _num("STATUS_EVERY_HOURS", 3, int))
SEND_STATUS_UPDATES = _flag("SEND_STATUS_UPDATES")
SEND_WATCHLIST = _flag("SEND_WATCHLIST")

# ---------------- SELF-ADJUSTING (uses the bot's own closed trades) ----------------
ADAPT_ON = _flag("ADAPT_ON")
ADAPT_WINDOW = 20                 # last N closed trades of the symbol
ADAPT_MIN_TRADES = _num("ADAPT_MIN_TRADES", 15, int)
ADAPT_BAD_R = _num("ADAPT_BAD_R", 3.0)            # window total <= -3R -> min score +1, <= -6R -> +2
ADAPT_SESSION_MIN = _num("ADAPT_SESSION_MIN", 12, int)
ADAPT_SESSION_AVG_R = _num("ADAPT_SESSION_AVG_R", 0.25)  # session avg < -0.25R -> session paused

# Evidence-based learning: only suppress a feature bucket after a meaningful sample.
LEARN_ON = _flag("LEARN_ON")
LEARN_WINDOW_DAYS = _num("LEARN_WINDOW_DAYS", 120, int)
LEARN_MIN_TRADES = _num("LEARN_MIN_TRADES", 20, int)
LEARN_BAD_AVG_R = _num("LEARN_BAD_AVG_R", 0.20)
LEARN_BAD_WINRATE = _num("LEARN_BAD_WINRATE", 0.45)
MAX_OPEN_PER_SYMBOL = _num("MAX_OPEN_PER_SYMBOL", 1, int)
MAX_OPEN_TOTAL = _num("MAX_OPEN_TOTAL", 2, int)
DAILY_LOSS_LIMIT_R = _num("DAILY_LOSS_LIMIT_R", 3.0)  # pause new entries after -3R net today

# ---------------- NEWS ----------------
NEWS_BLACKOUTS_UTC = []           # manual extras, e.g. "2026-11-05 13:30"
NEWS_WINDOW_MIN = _num("NEWS_WINDOW_MIN", 30, int)   # before AND after each event
NEWS_AUTO = _flag("NEWS_AUTO")
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CURRENCIES = tuple(
    x.strip().upper() for x in _env("NEWS_CURRENCIES", "USD").split(",") if x.strip()
)
NEWS_REFRESH_HOURS = 6

# ---------------- ACCOUNT (lot size in the alert) ----------------
ACCOUNT_BALANCE = _num("ACCOUNT_BALANCE", 0.0)    # 0 = do not show lot size
RISK_PCT = _num("RISK_PCT", 1.0)

# ---------------- API BUDGET / HEALTH ----------------
TWELVE_DAILY_LIMIT = _num("TWELVE_DAILY_LIMIT", 800, int)   # 0 = no guard
ERROR_ALERT_AFTER = 3             # consecutive failed scans -> one Telegram alert

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"ICT-SMC-Scalp-Reader/{VERSION}"})

_API = {"calls": 0}   # Twelve Data requests made by this process since the last flush

# ============================================================
# BASIC HELPERS / TWELVE DATA / INDICATORS
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
    return any(start <= h < end for start, end in (sessions or []))


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


def get_candles(symbol, interval, outputsize):
    require_credentials()

    data = None
    for attempt in range(2):
        _API["calls"] += 1
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

        # Some API plans return HTTP 429; others return code=429 in JSON.
        if response.status_code == 429:
            if attempt == 0:
                time.sleep(RATE_LIMIT_WAIT)
                continue
            raise RuntimeError("Twelve Data HTTP 429 rate limit")

        response.raise_for_status()
        data = response.json()
        if data.get("code") == 429 and attempt == 0:
            time.sleep(RATE_LIMIT_WAIT)
            continue
        break

    if not isinstance(data, dict) or "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")

    # Use timestamp as key: duplicated/out-of-order provider rows must not
    # create false 15M/1H candles or distort indicators.
    by_time = {}
    for row in data["values"]:
        try:
            timestamp = dt.datetime.strptime(
                row["datetime"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
            o = float(row["open"])
            h = float(row["high"])
            lo = float(row["low"])
            c = float(row["close"])
            if not all(math.isfinite(x) for x in (o, h, lo, c)):
                continue
            if h < max(o, c, lo) or lo > min(o, c, h):
                continue
            by_time[timestamp] = {
                "time": row["datetime"],
                "dt": timestamp,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
            }
        except (KeyError, TypeError, ValueError):
            continue

    candles = [by_time[k] for k in sorted(by_time)]
    if not candles:
        raise RuntimeError(f"No valid candles returned for {symbol}")
    return drop_unclosed(candles, interval)


def get_current_price(symbol):
    """Fetch a live quote for the final max-chase safety check."""
    _API["calls"] += 1
    response = SESSION.get(
        "https://api.twelvedata.com/price",
        params={"symbol": symbol, "apikey": TWELVE_DATA_API_KEY},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if "price" not in payload:
        raise RuntimeError(f"Twelve Data price error: {payload}")
    return float(payload["price"])


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
# STRUCTURE / LIQUIDITY / FVG / OB
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


def in_zone(price, zone, tolerance):
    if not zone:
        return False
    return zone["low"] - tolerance <= price <= zone["high"] + tolerance


# ============================================================
# H1 SUPPLY / DEMAND ZONES + CONFLICT GATE  (v8)
# ============================================================

_ZONE_RANK = {"STRONG": 3, "MEDIUM": 2, "WEAK": 1}


def find_htf_zones(candles):
    """Active (not yet broken) demand / supply zones on closed H1 candles.

    Demand: base candle right before a strong bullish impulse.
    Supply: base candle right before a strong bearish impulse.
    A zone dies when a candle CLOSES beyond its far edge.
    """
    n = len(candles)
    if n < ATR_PERIOD + 5:
        return []
    a = atr(candles, ATR_PERIOD)
    found = []
    for i in range(max(1, n - ZONE_LOOKBACK), n - 1):
        base, move = candles[i], candles[i + 1]
        av = a[i + 1]
        if not av:
            continue
        body = candle_body(move)
        if body < av * ZONE_IMPULSE_ATR or body / candle_range(move) < ZONE_IMPULSE_BODY:
            continue
        later = candles[i + 2:]
        if move["close"] > move["open"]:
            side = "DEMAND"
            low, high = base["low"], max(base["open"], base["close"])
            broken = any(x["close"] < low for x in later)
        elif move["close"] < move["open"]:
            side = "SUPPLY"
            low, high = min(base["open"], base["close"]), base["high"]
            broken = any(x["close"] > high for x in later)
        else:
            continue
        height = high - low
        if broken or height <= 0 or height > av * ZONE_MAX_HEIGHT_ATR:
            continue
        touches = sum(1 for x in later if x["low"] <= high and x["high"] >= low)
        if touches > ZONE_MAX_TOUCHES:
            continue
        strength = body / av
        if strength >= 1.8 and touches <= 1:
            quality = "STRONG"
        elif strength >= 1.4 or touches == 0:
            quality = "MEDIUM"
        else:
            quality = "WEAK"
        found.append({
            "side": side, "low": low, "high": high, "touches": touches,
            "strength": round(strength, 2), "quality": quality,
            "time": base["time"], "index": i,
        })

    found.sort(key=lambda z: z["index"], reverse=True)       # newest first
    out = []
    for z in found:                                           # drop near-duplicates
        h = z["high"] - z["low"]
        if any(o["side"] == z["side"]
               and min(o["high"], z["high"]) - max(o["low"], z["low"]) > 0.5 * h for o in out):
            continue
        out.append(z)
        if len(out) >= ZONE_MAX_KEEP:
            break
    return out


def zone_check(side, price, recent5, zones, tol):
    """('CONFLICT'|'ALIGNED'|'NONE', zone).

    CONFLICT: BUY inside a supply zone / SELL inside a demand zone
              (price inside BOTH kinds of zone is a conflict for both sides).
    ALIGNED : BUY at a demand zone / SELL at a supply zone - price is in it, or one of the
              last few 5M candles tapped it and price has not closed through it.
    """
    want = "DEMAND" if side == "BUY" else "SUPPLY"
    opp = "SUPPLY" if side == "BUY" else "DEMAND"

    def inside(z):
        return z["low"] - tol <= price <= z["high"] + tol

    for z in zones:
        if z["side"] == opp and inside(z):
            return "CONFLICT", z

    best = None
    for z in zones:
        if z["side"] != want:
            continue
        tapped = inside(z) or any(
            c["low"] <= z["high"] + tol and c["high"] >= z["low"] - tol for c in recent5)
        on_side = price >= z["low"] - tol if want == "DEMAND" else price <= z["high"] + tol
        if tapped and on_side and (best is None or _ZONE_RANK[z["quality"]] > _ZONE_RANK[best["quality"]]):
            best = z
    return ("ALIGNED", best) if best else ("NONE", None)


def final_zone_check(signal, live_price):
    """Second, independent conflict check right before the alert is sent.
    Looks at the planned entry AND the live quote. Fails closed on a conflict."""
    if not ZONE_ON:
        return True, ""
    zones = signal.get("zones") or []
    tol = signal.get("zone_tol", 0.0) or 0.0
    side = signal["side"]
    opp = "SUPPLY" if side == "BUY" else "DEMAND"
    for label, px in (("entry", signal.get("entry")), ("live price", live_price)):
        if px is None:
            continue
        for z in zones:
            if z["side"] == opp and z["low"] - tol <= px <= z["high"] + tol:
                return False, (f"{side} {label} {px:.2f} is inside an H1 {opp.lower()} zone "
                               f"{z['low']:.2f}-{z['high']:.2f}")
    return True, ""


def zone_levels(signal):
    """Opposing zone edges act as strong liquidity: TP stops before them, no-room trades are skipped."""
    out = []
    if not ZONE_ON:
        return out
    for z in signal.get("zones") or []:
        if signal["side"] == "BUY" and z["side"] == "SUPPLY":
            out.append((z["low"], "H1 supply zone", True))
        elif signal["side"] == "SELL" and z["side"] == "DEMAND":
            out.append((z["high"], "H1 demand zone", True))
    return out


def _zone_of(signal):
    return (signal.get("zone") or {}).get("zone") or {}


def zone_line(s):
    z = _zone_of(s)
    if not z:
        return "none"
    return f"{z['side']} {z['low']:.2f}-{z['high']:.2f} · {z['quality']} · touches {z['touches']}"


# ============================================================
# STATE / SCORING ENGINE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"state_version": 3, "open_trades": [], "closed_trades": []}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state root must be a JSON object")
        state.setdefault("state_version", 3)
        state.setdefault("open_trades", [])
        state.setdefault("closed_trades", [])
        # v5 migration: do not re-process the candle in which the signal was issued.
        for trade in state.get("open_trades", []):
            if trade.get("start_dt"):
                first_manageable = _from_iso(trade["start_dt"]) - dt.timedelta(microseconds=1)
                old_checked = trade.get("checked_until")
                if not old_checked or _from_iso(old_checked) < first_manageable:
                    trade["checked_until"] = _iso(first_manageable)
        return state
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        # Preserve a corrupt file for diagnosis instead of silently overwriting it.
        backup = STATE_FILE.with_suffix(f".corrupt-{int(time.time())}.json")
        try:
            STATE_FILE.replace(backup)
            print(f"[STATE] invalid state moved to {backup}: {exc}")
        except OSError:
            print(f"[STATE] invalid state could not be backed up: {exc}")
        return {"state_version": 3, "open_trades": [], "closed_trades": []}


def save_state(state):
    state["state_version"] = 3
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _parse_sessions(text):
    """'6-21' or '6-10,12-16' -> [(6, 21)]. '', 'off', 'none' -> None."""
    text = (text or "").strip().lower()
    if text in ("", "off", "none", "0"):
        return None
    out = []
    try:
        for part in text.split(","):
            a, b = part.split("-")
            out.append((int(a), int(b)))
    except ValueError:
        return None
    return out or None


def grade_of(score):
    return "A" if score >= 10 else "B" if score >= 8 else "C"


def session_name(ts):
    h = ts.hour
    if h < 7:
        return "ASIA"
    if h < 12:
        return "LONDON"
    if h < 21:
        return "NEW_YORK"
    return "LATE"


def short_reasons(blocked):
    """Compact version of the blocked list for Telegram."""
    out = []
    for b in blocked:
        if b == "no 5M sweep/BOS/CHoCH":
            x = "no 5M trigger"
        elif b.startswith("trigger"):
            x = "weak trigger"
        elif b == "no 5M entry confirmation":
            x = "no entry confirm"
        elif b.startswith("against"):
            x = "against trend"
        elif b.startswith("context"):
            x = "weak context"
        elif b.startswith("volatility"):
            x = "dead/spiky volatility"
        elif b.startswith("zone conflict"):
            x = "zone conflict"
        elif b.startswith("not at"):
            x = "not at H1 zone"
        elif b == "no 5M pinbar":
            x = "no pinbar"
        else:
            continue  # score / conflicts are not shown
        if x == "weak trigger" and "no 5M trigger" in out:
            continue
        if x not in out:
            out.append(x)
    return out[:2]


def maybe_send_status(state, name, text, ts):
    """Send at most one status message per symbol per UTC hour."""
    if not SEND_STATUS_UPDATES:
        return

    hour_key = ts.strftime("%Y-%m-%d") + f" b{ts.hour // STATUS_EVERY_HOURS}"
    state_key = f"status:{name}"

    if state.get(state_key) == hour_key:
        return

    send_telegram(text)
    state[state_key] = hour_key
    save_state(state)
    print(f"[{name}] status message sent")


def resample(candles, minutes):
    """Build only complete, gap-free higher-timeframe candles from 5M data."""
    if minutes < 5 or minutes % 5:
        raise ValueError("minutes must be a multiple of 5")
    per = minutes // 5
    out = []
    bucket = None
    group = []

    def flush():
        if bucket is None or len(group) != per:
            return
        # A count-only check can accept a bucket with a missing bar and a
        # duplicate bar. Verify exact 5-minute timestamp continuity.
        bucket_start = dt.datetime.fromtimestamp(bucket, UTC)
        expected = [bucket_start + dt.timedelta(minutes=5 * k) for k in range(per)]
        if [x["dt"] for x in group] != expected:
            return
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


def scalp_score(data1h, data15, data5, cfg=None):
    cfg = cfg or {"min_score": SCALP_MIN_SCORE, "watch_min": SCALP_WATCH_MIN_SCORE}
    price = data5[-1]["close"]
    atr_series = atr(data5, ATR_PERIOD)
    a5 = atr_series[-1]

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
    pin = pinbar(data5[-1])  # only the latest fully closed 5M candle may confirm entry

    zones, ztol, recent5 = [], 0.0, data5[-ZONE_TOUCH_BARS:]
    if ZONE_ON:
        zones = find_htf_zones(data1h)
        ztol = (atr(data1h, ATR_PERIOD)[-1] or 0.0) * ZONE_TOL_H1_ATR

    if t15 != "NEUTRAL":
        bias = t15
    elif h1 != "NEUTRAL":
        bias = h1
    else:
        bias = "RANGE"

    base = {
        "bias": bias, "t15": t15, "h1": h1,
        "m5": m5, "m15": m15, "ema5": e5,
        "sweep": sweep, "pd": pd15, "min_score": cfg["min_score"],
        "ob": detect_order_block(data5), "fvg": detect_fvg(data5),
        "zones": zones,
    }

    vol_ratio, vol_block = volatility_state(atr_series, a5)
    base["vol_ratio"] = vol_ratio

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
            and sweep.get("age", 999) <= SWEEP_MAX_AGE
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

        # H1 zone layer (v8): conflict gate + optional "must be at a zone" strategy rule
        zstatus, zone = "NONE", None
        if ZONE_ON:
            zstatus, zone = zone_check(side, price, recent5, zones, ztol)
            if zstatus == "CONFLICT":
                blocked.append(
                    f"zone conflict: {side} inside H1 {zone['side'].lower()} zone "
                    f"{zone['low']:.2f}-{zone['high']:.2f}")
            elif zstatus == "ALIGNED":
                reasons.append(
                    f"at H1 {zone['side'].lower()} zone ({zone['quality']}, touches {zone['touches']})")
            elif ZONE_REQUIRE:
                blocked.append("not at an aligned H1 zone")
            if ZONE_NEED_PIN and not pin_ok:
                blocked.append("no 5M pinbar")
        if not (sweep_ok or break_ok):
            blocked.append("no 5M sweep/BOS/CHoCH")
        if trigger < SCALP_MIN_TRIGGER:
            blocked.append(f"trigger {trigger}<{SCALP_MIN_TRIGGER}")
        if context < SCALP_MIN_CONTEXT:
            blocked.append(f"context {context}<{SCALP_MIN_CONTEXT}")
        if confirm < SCALP_MIN_CONFIRM:
            blocked.append("no 5M entry confirmation")
        if t15 != wanted and h1 != wanted:
            blocked.append("no higher timeframe trend alignment")
        if t15 == opp and h1 == opp:
            blocked.append("against 15M + 1H trend")
        if len(conflicts) >= 3:
            blocked.append("conflicts: " + ", ".join(conflicts))
        if vol_block:
            blocked.append(vol_block)
        if total < cfg["min_score"]:
            blocked.append(f"score {total}<{cfg['min_score']}")

        diag.append({
            "side": side, "score": total, "context": context,
            "trigger": trigger, "confirm": confirm, "blocked": blocked,
            "min_score": cfg["min_score"],
        })

        entry = {
            "side": side, "score": total, "context": context,
            "trigger": trigger, "confirm": confirm,
            "reasons": reasons, "conflicts": conflicts,
            "atr5": a5, "price": price, "pin": pin,
            "min_score": cfg["min_score"], "blocked": list(blocked),
            "grade": grade_of(total),
            "m5": m5, "t15": t15, "h1": h1, "pd": pd15,
            "sweep": sweep if sweep_ok else None,
            "ob": ob, "fvg": fvg, "vol_ratio": vol_ratio,
            "zone": {"status": zstatus, "zone": zone}, "zones": zones, "zone_tol": ztol,
        }

        if not blocked:
            results.append(entry)
        elif (
            total >= cfg["watch_min"]
            and len(blocked) <= 2
            and not any(b.startswith(("against", "conflicts", "context", "volatility", "zone conflict")) for b in blocked)
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


def build_scalp_watch_message(name, e, digits):
    icon = "🟢" if e["side"] == "BUY" else "🔴"
    need = e.get("min_score", SCALP_MIN_SCORE)
    missing = ", ".join(short_reasons(e.get("blocked", []))) or f"score {e['score']}/{need}"
    reasons = "\n".join(f"✓ {x}" for x in e["reasons"][:8])
    return (
        f"👀 <b>{name} — ALMOST READY {e['side']}</b> {icon}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{e['price']:.{digits}f}</b>\n"
        f"Score: <b>{e['score']}/{SCALP_MAX_SCORE}</b> (need {need}) — "
        f"ctx {e['context']} · trig {e['trigger']} · entry {e['confirm']}\n"
        f"15M: {e['t15']} · 1H: {e['h1']}\n"
        f"⏳ Missing: <b>{html.escape(missing)}</b>\n\n"
        f"{reasons}\n\n"
        f"⚠️ Heads-up only — NOT a trade signal. Check the chart yourself."
    )



# ============================================================
# PER-SYMBOL SETTINGS
# ============================================================

def scalp_cfg(config):
    """Per-symbol settings. BTCUSD can be tuned with BTC_* variables."""
    cfg = {
        "min_score": SCALP_MIN_SCORE,
        "min_sl_atr": SCALP_MIN_SL_ATR,
        "max_sl_atr": SCALP_MAX_SL_ATR,
        "sl_lookback": SCALP_SL_LOOKBACK,
        "sessions": _parse_sessions(_env("XAU_SESSIONS_UTC", "6-20")),
        "smart_entry": SMART_ENTRY,
        "tp2_r": SCALP_TP2_R,
        "tp2_min_r": SCALP_TP2_MIN_R,
        "max_tp2_r": SCALP_MAX_TP2_R,
        "min_room_r": SCALP_MIN_ROOM_R,
        "spread": config.get("spread", 0.0),
        "value_per_lot": config.get("value_per_lot", 0.0),
    }
    if config["name"] == "BTCUSD":
        cfg["min_score"] = _num("BTC_MIN_SCORE", 8, int)
        cfg["min_sl_atr"] = _num("BTC_MIN_SL_ATR", 1.1)
        cfg["max_sl_atr"] = _num("BTC_MAX_SL_ATR", 3.0)
        cfg["sl_lookback"] = _num("BTC_SL_LOOKBACK", 10, int)
        cfg["sessions"] = _parse_sessions(_env("BTC_SESSIONS_UTC", "6-21"))
    cfg["watch_min"] = max(cfg["min_score"] - 1, 1)
    return cfg


def volatility_state(atr_series, a5):
    """Compare the current 5M ATR with its one-day median."""
    vals = [x for x in atr_series[-VOL_LOOKBACK:] if x]
    if len(vals) < 50 or not a5:
        return 1.0, None
    ratio = a5 / statistics.median(vals)
    if VOL_MIN_RATIO > 0 and ratio < VOL_MIN_RATIO:
        return ratio, f"volatility too low ({ratio:.2f}x)"
    if VOL_MAX_RATIO > 0 and ratio > VOL_MAX_RATIO:
        return ratio, f"volatility spike ({ratio:.2f}x)"
    return ratio, None


# ============================================================
# NEWS  (free calendar + manual list)
# ============================================================

def refresh_news(state, ts):
    """Download this week's high-impact events at most every NEWS_REFRESH_HOURS."""
    if not NEWS_AUTO:
        return
    last = state.get("news_fetched")
    if last:
        try:
            if (ts - _from_iso(last)).total_seconds() < NEWS_REFRESH_HOURS * 3600:
                return
        except ValueError:
            pass
    state["news_fetched"] = _iso(ts)
    try:
        resp = SESSION.get(NEWS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        events = []
        for row in resp.json():
            if str(row.get("impact", "")).lower() != "high":
                continue
            if str(row.get("country", "")).upper() not in NEWS_CURRENCIES:
                continue
            d = dt.datetime.fromisoformat(row["date"])
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            events.append(d.astimezone(UTC).strftime("%Y-%m-%d %H:%M"))
        state["news_events"] = sorted(set(events))
        print(f"[NEWS] {len(events)} high-impact events loaded")
    except Exception as exc:
        # retry in ~1 hour; the manual list and older events keep working
        state["news_fetched"] = _iso(ts - dt.timedelta(hours=NEWS_REFRESH_HOURS - 1))
        print(f"[NEWS] calendar fetch failed (manual list still works): {exc}")


def _news_times(state):
    out = []
    for s in list(NEWS_BLACKOUTS_UTC) + list((state or {}).get("news_events", [])):
        try:
            out.append(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC))
        except ValueError:
            continue
    return out


def in_news_blackout(ts, state=None):
    return any(abs((ts - ev).total_seconds()) <= NEWS_WINDOW_MIN * 60 for ev in _news_times(state))


def next_news(ts, state, within_hours=3):
    soon = [ev for ev in _news_times(state) if 0 <= (ev - ts).total_seconds() <= within_hours * 3600]
    return min(soon) if soon else None


# ============================================================
# SELF-ADJUSTING  (30-day rolling, needs enough trades)
# ============================================================

def adaptive_adjust(state, name, session, ts):
    """Returns (min_score_bonus, reason_to_pause_or_None)."""
    if not ADAPT_ON:
        return 0, None
    since = ts - dt.timedelta(days=30)
    hist = []
    for x in state.get("closed_trades", []):
        if x.get("name") != name:
            continue
        try:
            if _from_iso(x["closed"]) >= since:
                hist.append(x)
        except (KeyError, ValueError):
            continue

    bonus = 0
    recent = hist[-ADAPT_WINDOW:]
    if len(recent) >= ADAPT_MIN_TRADES:
        total = sum(x["r"] for x in recent)
        if total <= -2 * ADAPT_BAD_R:
            bonus = 2
        elif total <= -ADAPT_BAD_R:
            bonus = 1

    same = [x for x in hist if x.get("session") == session]
    if len(same) >= ADAPT_SESSION_MIN:
        avg = sum(x["r"] for x in same) / len(same)
        if avg < -ADAPT_SESSION_AVG_R:
            return bonus, f"{session} session is losing lately ({len(same)} trades, avg {avg:+.2f}R, 30d)"
    return bonus, None


# ============================================================
# EVIDENCE-BASED SELF-LEARNING
# ============================================================

def _bd_session(ts):
    h = (ts.hour + 6) % 24
    if 6 <= h < 12:
        return "BD_MORNING"
    if 12 <= h < 18:
        return "BD_AFTERNOON"
    if 18 <= h < 24:
        return "BD_EVENING"
    return "BD_NIGHT"


def learning_features(signal, candle_dt):
    """Compact, explainable features saved with every completed trade."""
    vr = float(signal.get("vol_ratio", 1.0) or 1.0)
    vol_regime = "LOW" if vr < 0.75 else "HIGH" if vr > 1.50 else "NORMAL"
    sweep = signal.get("sweep") or {}
    side = signal.get("side", "?")
    htf = "BOTH" if signal.get("t15") == signal.get("h1") == ("BULLISH" if side == "BUY" else "BEARISH") else ("ONE" if (signal.get("t15") == ("BULLISH" if side == "BUY" else "BEARISH") or signal.get("h1") == ("BULLISH" if side == "BUY" else "BEARISH")) else "NONE")
    return {
        "utc_session": session_name(candle_dt),
        "bd_session": _bd_session(candle_dt),
        "weekday": candle_dt.strftime("%a"),
        "side": side,
        "score_band": "HIGH" if signal.get("score", 0) >= 10 else "MID" if signal.get("score", 0) >= 8 else "LOW",
        "entry_type": signal.get("entry_type", "MARKET"),
        "volatility": vol_regime,
        "htf_alignment": htf,
        "pd_zone": (signal.get("pd") or {}).get("zone", "UNKNOWN"),
        "zone_quality": _zone_of(signal).get("quality", "NO_ZONE"),
        "zone_touch": ("NO_ZONE" if not _zone_of(signal)
                       else "FRESH" if _zone_of(signal).get("touches", 1) == 0 else "TESTED"),
        "sweep_age": "FRESH" if sweep.get("age", 99) <= 1 else "OLDER" if sweep else "NO_SWEEP",
    }


def _learning_buckets(state, name, ts):
    since = ts - dt.timedelta(days=LEARN_WINDOW_DAYS)
    buckets = {}
    dimensions = ("utc_session", "bd_session", "weekday", "side", "score_band", "entry_type", "volatility", "htf_alignment", "pd_zone", "sweep_age", "zone_quality", "zone_touch")
    for trade in state.get("closed_trades", []):
        if trade.get("name") != name or not isinstance(trade.get("features"), dict):
            continue
        try:
            if _from_iso(trade["closed"]) < since:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        r = float(trade.get("r", 0.0))
        for dim in dimensions:
            value = trade["features"].get(dim)
            if value is not None:
                key = (dim, str(value))
                buckets.setdefault(key, []).append(r)
    return buckets


def learning_decision(state, name, features, ts):
    """Block only repeatedly losing conditions; never tune from tiny samples."""
    if not LEARN_ON:
        return None, []
    buckets = _learning_buckets(state, name, ts)
    bad, evidence = [], []
    for dim, value in features.items():
        vals = buckets.get((dim, str(value)), [])
        if len(vals) < LEARN_MIN_TRADES:
            continue
        avg = sum(vals) / len(vals)
        wins = sum(1 for x in vals if x > 0) / len(vals)
        evidence.append((dim, value, len(vals), avg, wins))
        if avg <= -LEARN_BAD_AVG_R and wins < LEARN_BAD_WINRATE and sum(vals) < 0:
            bad.append((dim, value, len(vals), avg, wins))
    if bad:
        # Most reliable negative bucket first; one strong reason is enough to stand aside.
        bad.sort(key=lambda x: (x[2], -x[3]), reverse=True)
        d, v, n, avg, wr = bad[0]
        return (f"learning filter: {d}={v} has {n} trades, avg {avg:+.2f}R, win {wr*100:.0f}%"), evidence
    return None, evidence


def daily_realised_r(state, name, ts):
    day = ts.strftime("%Y-%m-%d")
    total = 0.0
    for trade in state.get("closed_trades", []):
        if trade.get("name") == name and str(trade.get("closed", "")).startswith(day):
            total += float(trade.get("r", 0.0))
    return total


def learning_report(state, name, ts, limit=4):
    buckets = _learning_buckets(state, name, ts)
    rows = []
    for (dim, value), vals in buckets.items():
        if len(vals) < max(5, LEARN_MIN_TRADES // 2):
            continue
        avg = sum(vals) / len(vals)
        wr = sum(1 for x in vals if x > 0) / len(vals)
        rows.append((avg, dim, value, len(vals), wr))
    if not rows:
        return ["Learning: collecting labelled trades; no reliable pattern yet."]
    rows.sort(key=lambda x: x[0])
    out = ["<b>Learning — weakest conditions</b>"]
    for avg, dim, value, n, wr in rows[:limit]:
        out.append(f"• {dim}={value}: {n} trades, avg {avg:+.2f}R, win {wr*100:.0f}%")
    good = sorted(rows, key=lambda x: x[0], reverse=True)
    out.append("<b>Learning — strongest conditions</b>")
    for avg, dim, value, n, wr in good[:limit]:
        out.append(f"• {dim}={value}: {n} trades, avg {avg:+.2f}R, win {wr*100:.0f}%")
    out.append("Learning filters activate only after the configured minimum sample; correlation is not proof of causation.")
    return out


# ============================================================
# LIQUIDITY TARGETS
# ============================================================

def key_levels(data5, data15, data1h):
    """Liquidity pools price tends to run to. (price, label, strong)"""
    out = []
    last = data5[-1]["dt"]
    today = last.date()

    dates = sorted({c["dt"].date() for c in data5 if c["dt"].date() < today})
    for d in reversed(dates):
        g = [c for c in data5 if c["dt"].date() == d]
        if len(g) >= 100:
            out.append((max(c["high"] for c in g), "previous-day high", True))
            out.append((min(c["low"] for c in g), "previous-day low", True))
            break

    asia = [c for c in data5 if c["dt"].date() == today and c["dt"].hour < 7]
    if len(asia) >= 12:
        out.append((max(c["high"] for c in asia), "Asia high", True))
        out.append((min(c["low"] for c in asia), "Asia low", True))

    for p in find_pivots(data1h[-80:]):
        out.append((p["price"], f"1H swing {p['kind']}", True))
    for p in find_pivots(data15[-160:]):
        out.append((p["price"], f"15M swing {p['kind']}", False))

    lv = liquidity_levels(data5)
    if lv.get("equal_high"):
        out.append((lv["equal_high"], "equal highs", True))
    if lv.get("equal_low"):
        out.append((lv["equal_low"], "equal lows", True))
    return out


# ============================================================
# TRADE PLAN  (market or limit entry, liquidity TP, cost filter)
# ============================================================

def make_scalp_plan(signal, data5, data15, data1h, cfg):
    """Returns (plan, None) or (None, reason)."""
    side = signal["side"]
    sign = 1 if side == "BUY" else -1
    a = signal["atr5"]
    close = data5[-1]["close"]
    recent = data5[-cfg["sl_lookback"]:]

    if side == "BUY":
        sl = min(c["low"] for c in recent) - a * SCALP_SL_BUFFER_ATR
    else:
        sl = max(c["high"] for c in recent) + a * SCALP_SL_BUFFER_ATR
    risk_mkt = sign * (close - sl)
    if risk_mkt <= 0:
        return None, "SL on the wrong side of price"
    if risk_mkt < a * cfg["min_sl_atr"]:               # too tight -> widen (noise would stop it)
        risk_mkt = a * cfg["min_sl_atr"]
        sl = close - sign * risk_mkt
    if risk_mkt / a > cfg["max_sl_atr"]:
        return None, f"SL {risk_mkt / a:.1f} ATR is wider than {cfg['max_sl_atr']:g}"

    entry, risk, entry_type = close, risk_mkt, "MARKET"
    if cfg.get("smart_entry"):
        best = None
        for z in (signal.get("fvg"), signal.get("ob")):
            if not z:
                continue
            mid = (z["low"] + z["high"]) / 2
            dist = sign * (close - mid)              # >0: zone mid is on the pullback side
            risk_l = sign * (mid - sl)
            if (a * SMART_ENTRY_MIN_ATR <= dist <= a * SMART_ENTRY_MAX_ATR
                    and risk_l >= a * SMART_MIN_RISK_ATR):
                if best is None or dist < best[0]:
                    best = (dist, mid, risk_l)
        if best:
            entry, risk, entry_type = best[1], best[2], "LIMIT"

    spread = cfg.get("spread", 0.0) or 0.0
    cost_r = spread / risk if risk else 0.0
    if MAX_COST_R > 0 and cost_r > MAX_COST_R:
        return None, f"spread {spread:g} is {cost_r:.2f}R of the SL (max {MAX_COST_R:g}R)"

    ahead = []
    for price, label, strong in key_levels(data5, data15, data1h) + zone_levels(signal):
        d = sign * (price - entry)
        if d > 0:
            ahead.append((d / risk, price, label, strong))
    ahead.sort()

    if cfg["min_room_r"] > 0:
        for r, price, label, strong in ahead:
            if strong and r < cfg["min_room_r"]:
                return None, f"{label} only {r:.1f}R away (need {cfg['min_room_r']:g}R room)"

    tp1_r = SCALP_TP1_R
    tp2_r, tp2_label = cfg["tp2_r"], "fixed fallback"
    min_target_r = tp1_r + 0.25
    buffer_r = a * SCALP_TP_BUFFER_ATR / risk

    # Prefer the first actionable liquidity level beyond TP1. This prevents
    # a fixed fallback target from sitting beyond a nearby liquidity pool.
    for r, price, label, strong in ahead:
        rr = r - buffer_r
        if rr < min_target_r:
            continue
        if r < cfg["tp2_min_r"] and not strong:
            continue
        if rr > cfg["max_tp2_r"]:
            tp2_r, tp2_label = cfg["max_tp2_r"], f"cap (next: {label})"
        else:
            tp2_r, tp2_label = rr, label
        break
    tp2_r = max(tp2_r, min_target_r)

    plan = dict(signal)
    plan.update({
        "entry": entry,
        "entry_type": entry_type,
        "market_price": close,
        "sl": sl,
        "tp1": entry + sign * risk * tp1_r,
        "tp": entry + sign * risk * tp2_r,
        "tp1_r": tp1_r,
        "tp2_r": tp2_r,
        "tp2_label": tp2_label,
        "risk": risk,
        "risk_atr": risk / a,
        "cost_r": cost_r,
        "chase_limit": close + sign * a * SCALP_MAX_CHASE_ATR,
        "time": data5[-1]["time"],
        "close_dt": data5[-1]["dt"] + dt.timedelta(minutes=5),
        "grade": grade_of(signal["score"]),
        "session": session_name(data5[-1]["dt"]),
        "features": learning_features(signal, data5[-1]["dt"]),
    })
    return plan, None


def lot_size(cfg, risk_price):
    if ACCOUNT_BALANCE <= 0 or risk_price <= 0 or not cfg.get("value_per_lot"):
        return None
    money = ACCOUNT_BALANCE * RISK_PCT / 100.0
    return max(math.floor(money / (risk_price * cfg["value_per_lot"]) * 100) / 100, 0.0)


def signal_still_valid(plan, data5, live_price):
    """False if the setup already played out or price ran away since the signal candle."""
    buy = plan["side"] == "BUY"
    limit = plan.get("entry_type") == "LIMIT"

    def hit_against(hi, lo, lvl):      # price reached lvl on the losing side
        return (lo <= lvl) if buy else (hi >= lvl)

    def hit_profit(hi, lo, lvl):       # price reached lvl on the winning side
        return (hi >= lvl) if buy else (lo <= lvl)

    for c in data5:
        if c["dt"] < plan["close_dt"]:
            continue
        hi, lo = c["high"], c["low"]
        if hit_against(hi, lo, plan["sl"]):
            return False, "SL level already touched"
        if hit_profit(hi, lo, plan["tp1"]):
            return False, "TP1 already reached"
        if limit and hit_against(hi, lo, plan["entry"]):
            return False, "limit level already touched"

    if live_price is not None:
        if hit_against(live_price, live_price, plan["sl"]):
            return False, "live price is beyond SL"
        if hit_profit(live_price, live_price, plan["tp1"]):
            return False, "live price is beyond TP1"
        if limit:
            if hit_against(live_price, live_price, plan["entry"]):
                return False, "live price already at the limit level"
        else:
            chased = live_price > plan["chase_limit"] if buy else live_price < plan["chase_limit"]
            if chased:
                return False, "price already ran past the max-chase limit"
    return True, ""


# ============================================================
# ANALYSIS
# ============================================================

def evaluate(data5, cfg):
    """Score + plan on one closed-candle snapshot. None if not enough candles."""
    d15 = resample(data5, 15)
    d1h = resample(data5, 60)
    if len(data5) < 60 or len(d15) < 30 or len(d1h) < 20:
        return None
    result = scalp_score(d1h, d15, data5, cfg)
    result["max_sl_atr"] = cfg["max_sl_atr"]
    signal = result.get("signal")
    if signal:
        plan, why = make_scalp_plan(signal, data5, d15, d1h, cfg)
        if plan is None:
            result["plan_rejected"] = (signal["side"], why)
        result["signal"] = plan
    return result


def analyze_symbol_scalp(config, cfg, loop_mode=False):
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

    result = None
    caught_up = 0
    for cut in range(0, SCALP_CATCHUP_CANDLES + 1):
        snap = data5[:len(data5) - cut] if cut else data5
        res = evaluate(snap, cfg)
        if res is None:
            if cut == 0:
                raise RuntimeError(f"Not enough candles (5M={len(data5)})")
            break
        if cut == 0:
            result = res
        elif res.get("signal"):
            result["signal"] = res["signal"]      # keep today's diagnostics, use the older setup
            caught_up = cut
        if result.get("signal"):
            break

    result["caught_up"] = caught_up
    return {
        "name": name,
        "price": data5[-1]["close"],
        "digits": digits,
        "result": result,
        "last_candle_time": data5[-1]["time"],
        "data5": data5,
    }


# ============================================================
# MESSAGES
# ============================================================

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

    if s["entry_type"] == "LIMIT":
        entry_block = (
            f"🎯 <b>LIMIT {s['side']} @ {f(s['entry'])}</b> (5M OB/FVG pullback)\n"
            f"Market now: {f(s['market_price'])} — don't chase, wait for the pullback.\n"
            f"Cancel if TP1 is reached first or after {LIMIT_EXPIRY_MIN} min.\n"
        )
    else:
        word = "above" if s["side"] == "BUY" else "below"
        entry_block = (
            f"Entry: <b>{f(s['entry'])}</b> (5M close, market)\n"
            f"Skip if price is already {word} <b>{f(s['chase_limit'])}</b>\n"
        )

    extra = ""
    if s.get("lots") is not None:
        extra += f"Lot ({RISK_PCT:g}% of {ACCOUNT_BALANCE:g}): <b>{s['lots']:.2f}</b>\n"
    if s.get("cost_r"):
        extra += f"Spread cost ≈ {s['cost_r']:.2f}R\n"

    nn = s.get("next_news")
    news_line = f"⚠️ High-impact news at {nn.strftime('%H:%M')} UTC\n" if nn else ""
    late = f" · found {s['caught_up_min']:.0f} min late (catch-up)" if s.get("caught_up_min") else ""

    return (
        f"⚡ {icon} <b>{name} — SCALP {s['side']}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⭐ Grade <b>{s.get('grade', '?')}</b> · Score: <b>{s['score']}/{SCALP_MAX_SCORE}</b> "
        f"(ctx {s['context']}/4 · trig {s['trigger']}/5 · entry {s['confirm']}/3)\n"
        f"🧭 15M: <b>{s['t15']}</b> · 1H: <b>{s['h1']}</b>\n"
        f"⏱ Signal age: {age:.1f} min{late}\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"{entry_block}"
        f"SL: <b>{f(s['sl'])}</b>\n"
        f"TP1 ({s['tp1_r']:g}R): <b>{f(s['tp1'])}</b>\n"
        f"TP2 ({s['tp2_r']:.1f}R · {html.escape(s['tp2_label'])}): <b>{f(s['tp'])}</b>\n"
        f"Risk: {f(s['risk'])} ({s['risk_atr']:.2f} x 5M ATR)\n"
        f"{extra}"
        f"At TP1: close half, SL → breakeven. Past the TP1→TP2 midpoint: SL → TP1.\n"
        f"{news_line}\n"
        f"<b>Why</b>\n{reasons}\n\n"
        f"Liquidity Sweep: {sweep_text}\n"
        f"5M BOS: {s['m5']['bos'] or 'None'} · CHoCH: {s['m5']['choch'] or 'None'}\n"
        f"H1 zone: {html.escape(zone_line(s))}\n"
        f"P/D (15M): {s['pd']['zone']}\n"
        f"Conflicts: {html.escape(conflicts)}\n\n"
        f"⏰ Closed 5M candle: {s['time']} UTC\n"
        f"⚠️ Rule-based alert, not a guarantee. Use your own risk rules."
    )


def build_scalp_no_trade_message(name, result, price, digits, note=None):
    lines = []
    for d in result.get("diag", []):
        why = ", ".join(short_reasons(d["blocked"]))
        need = d.get("min_score", SCALP_MIN_SCORE)
        if not why:
            why = "conflicts" if any(b.startswith("conflicts") for b in d["blocked"]) else "score low"
        lines.append(f"{d['side']} {d['score']}/{need} ❌ {html.escape(why)}")
    if result.get("plan_rejected"):
        side, why = result["plan_rejected"]
        lines.append(f"{side} setup found, plan rejected: {html.escape(why)}")
    if result.get("ambiguous"):
        lines.append("BUY and SELL tied — skipped")
    if note:
        lines.append(f"ℹ️ {html.escape(note)}")
    vr = result.get("vol_ratio")
    vol = f" · vol {vr:.1f}x" if vr else ""
    return (
        f"🟡 <b>{name} — NO TRADE</b>\n"
        f"Price <b>{price:.{digits}f}</b> · 15M {result.get('t15', '?')} · 1H {result.get('h1', '?')}{vol}\n"
        + "\n".join(lines)
        + "\n(Status — bot is alive.)"
    )


def maybe_send_watch(state, name, entry, digits, ts):
    """Heads-up for near-miss setups, one per symbol+side per cooldown."""
    if not SEND_WATCHLIST:
        return
    key = f"watchts:{name}:{entry['side']}"
    last = state.get(key)
    if last:
        try:
            if (ts - _from_iso(last)).total_seconds() < SCALP_WATCH_COOLDOWN_MIN * 60:
                return
        except ValueError:
            pass
    send_telegram(build_scalp_watch_message(name, entry, digits))
    state[key] = _iso(ts)
    save_state(state)
    print(f"[{name}] WATCH {entry['side']} sent score={entry['score']}")


def send_telegram(message):
    require_credentials()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    last_exc = None
    for attempt in range(3):
        try:
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
            if response.status_code == 429:
                try:
                    wait = int(response.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    wait = 3
                time.sleep(min(wait + 1, 30))
                last_exc = RuntimeError("Telegram rate limit (429)")
                continue
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram rejected message: {result}")
            return True
        except Exception as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def send_test_message():
    send_telegram(
        f"✅ <b>ICT SMC Scalp Reader v{VERSION}</b>\n"
        "Telegram connection works.\n"
        f"Time: {now_utc().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    print("Test message sent")

# ============================================================
# TRACKING / REPORTS (extracted, patched below)
# ============================================================

def _iso(d):
    return d.isoformat()


def _from_iso(s):
    return dt.datetime.fromisoformat(s)


def _r_of(t, price):
    sign = 1 if t["side"] == "BUY" else -1
    return sign * (price - t["entry"]) / t["risk"]


def _fmt_r(r):
    return f"{r:+.2f}R"


def _update_streak(state, name, closed, ts):
    """Pause a symbol after SL_STREAK_PAUSE stop-losses in a row."""
    sk = f"slstreak:{name}"
    # Use realised R, not only the exit label. An EXPIRED trade can still
    # be a loss and must not silently bypass the loss-streak protection.
    if len(closed) > 1 and closed[1] < 0:
        state[sk] = state.get(sk, 0) + 1
    elif len(closed) > 1 and closed[1] > 0:
        state[sk] = 0
    if SL_STREAK_PAUSE > 0 and state.get(sk, 0) >= SL_STREAK_PAUSE:
        until = ts + dt.timedelta(hours=SL_PAUSE_HOURS)
        state[f"pause_until:{name}"] = _iso(until)
        state[sk] = 0
        try:
            send_telegram(
                f"⏸ <b>{name}</b> — {SL_STREAK_PAUSE} SL in a row. "
                f"New signals paused until {until.strftime('%H:%M')} UTC "
                f"({SL_PAUSE_HOURS:g}h cooldown). Open trades are still tracked."
            )
        except Exception as exc:
            print(f"[{name}] telegram error (pause notice): {exc}")


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


def _max_drawdown(trades):
    peak = cum = dd = 0.0
    for x in sorted(trades, key=lambda x: x["closed"]):
        cum += x["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _group_lines(trades, key):
    groups = {}
    for x in trades:
        groups.setdefault(x.get(key) or "?", []).append(x)
    out = []
    for k in sorted(groups):
        g = groups[k]
        w = sum(1 for x in g if x["r"] > 0)
        out.append(
            f"{k}: {len(g)} trades · {_fmt_r(sum(x['r'] for x in g))} · win {w / len(g) * 100:.0f}%"
        )
    return out


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
            tp = " (TP1 done)" if t["tp1_hit"] else (" (limit pending)" if t.get("pending") else "")
            lines.append(f"• {t['name']} {t['side']} @ {t['entry']:.3f}{tp}")
    lines.append("")
    lines.append(_legend())
    return "\n".join(lines), len(closed)


def build_long_report(state, ts, days=30):
    since = ts - dt.timedelta(days=days)
    closed = [
        x for x in state.get("closed_trades", [])
        if _from_iso(x["closed"]) >= since
    ]
    lines = [f"📈 <b>{days}-day summary</b>", "━━━━━━━━━━━━━━━━"]
    if not closed:
        lines.append("No closed trades yet.")
        return "\n".join(lines)
    names = sorted({x["name"] for x in closed})
    for n in names:
        g = [x for x in closed if x["name"] == n]
        w = sum(1 for x in g if x["r"] > 0)
        lines.append(
            f"<b>{n}</b>: {len(g)} trades · {_fmt_r(sum(x['r'] for x in g))} · "
            f"win {w / len(g) * 100:.0f}% · max DD {_max_drawdown(g):.2f}R"
        )
    total = sum(x["r"] for x in closed)
    lines.append(
        f"<b>All</b>: {len(closed)} trades · {_fmt_r(total)} · max DD {_max_drawdown(closed):.2f}R"
    )
    lines.append("")
    lines.append("<b>By grade</b>")
    lines += _group_lines(closed, "grade")
    lines.append("")
    lines.append("<b>By session (UTC)</b>")
    lines += _group_lines(closed, "session")
    lines.append("")
    lines.append("<b>By entry type</b>")
    lines += _group_lines(closed, "entry_type")
    lines.append("")
    for n in names:
        lines.append("")
        lines.append(f"<b>{n} learning detail</b>")
        lines += learning_report(state, n, ts)
    ls = state.get("limit_stats") or {}
    if ls.get("filled", 0) + ls.get("cancelled", 0):
        lines.append(f"Limit orders: {ls.get('filled', 0)} filled · {ls.get('cancelled', 0)} cancelled (never filled)")
    if len(closed) < 30:
        lines.append("")
        lines.append("⚠️ Small sample (<30 trades) — do not tune settings from this yet.")
    lines.append("")
    lines.append(_legend())
    return "\n".join(lines)


def maybe_send_reports(state, ts):
    # Daily summary after DAILY_SUMMARY_HOUR_UTC (only if something happened today)
    day = ts.strftime("%Y-%m-%d")
    if ts.hour >= DAILY_SUMMARY_HOUR_UTC and state.get("dailysent") != day:
        text, n = build_report(state, ts, 1, "Daily scalp summary (last 24h)")
        text += f"\n\nTwelve Data credits today: {state.get('api:' + day, 0)}/{TWELVE_DAILY_LIMIT}"
        if n or state.get("open_trades"):
            send_telegram(text)
        state["dailysent"] = day
        save_state(state)

    # Weekly report (+ 30-day summary) on Sunday evening
    iso = ts.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if (
        ts.weekday() == WEEKLY_REPORT_WEEKDAY
        and ts.hour >= WEEKLY_REPORT_HOUR_UTC
        and state.get("weeklysent") != week
    ):
        text, _ = build_report(state, ts, 7, "Weekly scalp report (last 7 days)")
        send_telegram(text)
        send_telegram(build_long_report(state, ts, 30))
        state["weeklysent"] = week
        save_state(state)


def send_report_now():
    state = load_state()
    ts = now_utc()
    text, _ = build_report(state, ts, 7, "Scalp report (last 7 days)")
    send_telegram(text)
    send_telegram(build_long_report(state, ts, 30))
    print("Report sent")


def prune_state(state, ts):
    today = ts.strftime("%Y-%m-%d")
    for k in [k for k in state if k.startswith(("daycount:", "api:", "apiwarn:")) and not k.endswith(today)]:
        del state[k]



# ============================================================
# TRADE TRACKING  (limit fill/cancel, TP1, trail, TP2, SL, BE, timeout)
#   R assumption: 50% closes at TP1, stop moves to breakeven, the rest runs
#   to TP2. Past the TP1->TP2 midpoint the stop moves to TP1. If one candle
#   touches both SL and TP1, the SL is counted first (conservative). A limit
#   order that fills inside a candle is only managed from the next candle.
# ============================================================

def register_trade(state, name, plan):
    trades = state.setdefault("open_trades", [])
    tid = f"{name}:{plan['time']}:{plan['side']}"
    if any(t["id"] == tid for t in trades):
        return
    start = plan["close_dt"]
    limit = plan.get("entry_type") == "LIMIT"
    trades.append({
        "id": tid,
        "name": name,
        "side": plan["side"],
        "score": plan["score"],
        "entry": plan["entry"],
        "entry_type": plan.get("entry_type", "MARKET"),
        "pending": limit,
        "expiry_dt": _iso(start + dt.timedelta(minutes=LIMIT_EXPIRY_MIN)) if limit else None,
        "sl": plan["sl"],
        "sl_cur": plan["sl"],
        "tp1": plan["tp1"],
        "tp2": plan["tp"],
        "risk": plan["risk"],
        "cost_r": round(plan.get("cost_r", 0.0), 4),
        "tp1_r": plan.get("tp1_r", SCALP_TP1_R),
        "tp2_r": plan.get("tp2_r", SCALP_TP2_R),
        "tp1_hit": False,
        "trailed": False,
        "grade": plan.get("grade"),
        "session": plan.get("session"),
        "ctx": plan.get("context"),
        "trig": plan.get("trigger"),
        "conf": plan.get("confirm"),
        "features": plan.get("features", {}),
        "start_dt": _iso(start),
        "checked_until": _iso(start - dt.timedelta(microseconds=1)),
        "last_close": plan["entry"],
    })


def advance_trade(t, data5, ts):
    """Moves one trade forward. Returns (events, closed).
    events: list of (kind, price) with kind FILL / TP1 / TRAIL.
    closed: None or (label, r, price) with label SL / TP2 / BE / TRAIL / EXPIRED / CANCEL."""
    events = []
    closed = None
    buy = t["side"] == "BUY"
    checked = _from_iso(t["checked_until"])
    deadline = _from_iso(t["start_dt"]) + dt.timedelta(hours=TRADE_MAX_AGE_HOURS)
    expiry = _from_iso(t["expiry_dt"]) if t.get("expiry_dt") else None
    part = TRADE_TP1_PORTION
    tp1_r, tp2_r = t["tp1_r"], t["tp2_r"]
    r_tp2_total = part * tp1_r + (1 - part) * tp2_r
    r_trail_total = tp1_r            # both halves are out at the TP1 level

    # reached(c, lvl): price got to lvl on the winning side; against(c, lvl): on the losing side
    reached = (lambda c, v: c["high"] >= v) if buy else (lambda c, v: c["low"] <= v)
    against = (lambda c, v: c["low"] <= v) if buy else (lambda c, v: c["high"] >= v)

    for c in data5:
        if c["dt"] <= checked:
            continue
        if c["dt"] > deadline:
            break
        checked = c["dt"]
        t["last_close"] = c["close"]

        if t.get("pending"):
            if against(c, t["entry"]):
                t["pending"] = False
                t["filled_dt"] = _iso(c["dt"])
                events.append(("FILL", t["entry"]))
                if against(c, t["sl"]):          # filled and stopped in the same candle
                    closed = ("SL", -1.0, t["sl"])
                    break
                continue                         # managed from the next candle
            if reached(c, t["tp1"]) or (expiry and c["dt"] >= expiry):
                closed = ("CANCEL", 0.0, None)
                break
            continue

        sl_cur = t.get("sl_cur", t["sl"])
        if not t["tp1_hit"]:
            if against(c, sl_cur):
                closed = ("SL", -1.0, t["sl"])
                break
            if reached(c, t["tp1"]):
                t["tp1_hit"] = True
                t["sl_cur"] = t["entry"]
                events.append(("TP1", t["tp1"]))
                if reached(c, t["tp2"]):
                    closed = ("TP2", r_tp2_total, t["tp2"])
                    break
        else:
            if against(c, sl_cur):
                if t.get("trailed"):
                    closed = ("TRAIL", r_trail_total, sl_cur)
                else:
                    closed = ("BE", part * tp1_r, t["entry"])
                break
            if reached(c, t["tp2"]):
                closed = ("TP2", r_tp2_total, t["tp2"])
                break
            trigger = t["tp1"] + (t["tp2"] - t["tp1"]) * TRAIL_TRIGGER_FRAC
            if not t.get("trailed") and reached(c, trigger):
                t["trailed"] = True
                t["sl_cur"] = t["tp1"]
                events.append(("TRAIL", t["tp1"]))

    t["checked_until"] = _iso(checked)

    if closed is None and t.get("pending") and expiry and ts >= expiry:
        closed = ("CANCEL", 0.0, None)
    if closed is None and ts > deadline:
        if t.get("pending"):
            closed = ("CANCEL", 0.0, None)
        else:
            price = t["last_close"]
            rest = _r_of(t, price)
            r = part * tp1_r + (1 - part) * rest if t["tp1_hit"] else rest
            closed = ("EXPIRED", r, price)

    return events, closed


def _trade_event_text(t, kind, digits, closed=None):
    head = f"<b>{t['name']} {t['side']}</b>"
    f = lambda v: f"{v:.{digits}f}"
    if kind == "FILL":
        return (
            f"🎯 {head} — <b>limit filled</b> at {f(t['entry'])}\n"
            f"SL {f(t['sl'])} · TP1 {f(t['tp1'])} · TP2 {f(t['tp2'])}"
        )
    if kind == "TP1":
        return (
            f"✅ {head} — <b>TP1 hit</b> ({f(t['tp1'])})\n"
            f"Take partial profit and move SL to breakeven ({f(t['entry'])}).\n"
            f"TP2 still open: {f(t['tp2'])}"
        )
    if kind == "TRAIL":
        return (
            f"🔒 {head} — price passed the TP1→TP2 midpoint.\n"
            f"Move SL to TP1 ({f(t['tp1'])}) to lock profit. TP2: {f(t['tp2'])}"
        )
    label, r, price = closed
    if label == "CANCEL":
        return f"🚫 {head} — limit order <b>not filled</b>, cancel it. No trade, no loss."
    if label == "SL":
        return f"❌ {head} — <b>SL hit</b> at {f(price)}\nResult: <b>{_fmt_r(r - t.get('cost_r', 0) - TRADE_COST_R)}</b>"
    if label == "TP2":
        return (
            f"🏆 {head} — <b>TP2 hit</b> at {f(price)}\n"
            f"Result: <b>{_fmt_r(r - t.get('cost_r', 0) - TRADE_COST_R)}</b> (half at TP1, half at TP2)"
        )
    if label == "TRAIL":
        return (
            f"🔒 {head} — stopped at the TP1 lock ({f(price)})\n"
            f"Result: <b>{_fmt_r(r - t.get('cost_r', 0) - TRADE_COST_R)}</b>"
        )
    if label == "BE":
        return (
            f"➖ {head} — back to breakeven after TP1\n"
            f"Result: <b>{_fmt_r(r - t.get('cost_r', 0) - TRADE_COST_R)}</b>"
        )
    return (
        f"⌛ {head} — closed by timeout ({TRADE_MAX_AGE_HOURS}h) at {f(price)}\n"
        f"Result: <b>{_fmt_r(r - t.get('cost_r', 0) - TRADE_COST_R)}</b>"
    )


def _close_trade(state, t, closed, ts):
    stats = state.setdefault("limit_stats", {"filled": 0, "cancelled": 0})
    if closed[0] == "CANCEL":
        stats["cancelled"] += 1
        return                                   # never filled -> not a trade
    if t.get("entry_type") == "LIMIT":
        stats["filled"] += 1
    hist = state.setdefault("closed_trades", [])
    hist.append({
        "name": t["name"], "side": t["side"], "result": closed[0],
        "r": round(closed[1] - t.get("cost_r", 0.0) - TRADE_COST_R, 3),
        "score": t.get("score"), "grade": t.get("grade"), "session": t.get("session"),
        "entry_type": t.get("entry_type", "MARKET"),
        "features": t.get("features", {}),
        "opened": t["start_dt"], "closed": _iso(ts),
    })
    del hist[:-MAX_CLOSED_HISTORY]
    _update_streak(state, t["name"], closed, ts)


def _legend():
    be = TRADE_TP1_PORTION * SCALP_TP1_R
    text = (
        f"R assumes {TRADE_TP1_PORTION * 100:.0f}% closed at TP1, rest at TP2 (or the TP1 lock). "
        f"BE = {_fmt_r(be)}. Spread cost per trade is deducted."
    )
    if TRADE_COST_R:
        text += f" Extra {TRADE_COST_R:g}R per trade deducted."
    return text


# ============================================================
# MAIN
# ============================================================

def _flush_api(state, day):
    if _API["calls"]:
        key = f"api:{day}"
        state[key] = state.get(key, 0) + _API["calls"]
        _API["calls"] = 0


def _api_exhausted(state, day):
    if TWELVE_DAILY_LIMIT <= 0:
        return False
    used = state.get(f"api:{day}", 0) + _API["calls"]
    return used >= TWELVE_DAILY_LIMIT * 0.97


def run_once(loop_mode=False):
    state = load_state()
    ts0 = now_utc()
    prune_state(state, ts0)
    day0 = ts0.strftime("%Y-%m-%d")
    _API["calls"] = 0

    refresh_news(state, ts0)
    try:
        maybe_send_reports(state, ts0)
    except Exception as exc:
        print(f"[REPORT] ERROR: {exc}")

    for config in SYMBOLS:
        name = config["name"]

        try:
            ts = now_utc()
            day = ts.strftime("%Y-%m-%d")
            cfg = scalp_cfg(config)

            # ---- filters that need NO API call ----
            if config.get("weekend_closed") and market_closed_weekend(ts):
                print(f"[{name}] weekend, market closed")
                maybe_send_status(
                    state, name,
                    f"⚪ <b>{name}</b>\nMarket closed (weekend) — scanning paused. (Bot is alive.)", ts)
                continue

            if _api_exhausted(state, day):
                print(f"[{name}] Twelve Data daily budget reached, scan skipped")
                if not state.get(f"apiwarn:{day}"):
                    state[f"apiwarn:{day}"] = 1
                    try:
                        send_telegram(
                            f"⚠️ Twelve Data daily credit budget ({TWELVE_DAILY_LIMIT}) is almost used up. "
                            "Scanning is paused until tomorrow (UTC). Raise TWELVE_DAILY_LIMIT only if your plan allows it.")
                    except Exception as exc:
                        print(f"[{name}] telegram error (api warn): {exc}")
                continue

            bonus, session_block = adaptive_adjust(state, name, session_name(ts), ts)
            note = None
            if bonus:
                cfg["min_score"] = min(cfg["min_score"] + bonus, SCALP_MAX_SCORE - 1)
                cfg["watch_min"] = max(cfg["min_score"] - 1, 1)
                note = f"min score raised +{bonus} after recent losses"

            pause_reason = None
            if cfg["sessions"] and not in_session(ts, cfg["sessions"]):
                pause_reason = "Outside trading session"
            elif in_news_blackout(ts, state):
                pause_reason = "High-impact news blackout"
            elif session_block:
                pause_reason = session_block

            if pause_reason:
                print(f"[{name}] {pause_reason}, skipped")
                maybe_send_status(
                    state, name, f"⚪ <b>{name}</b>\n{html.escape(pause_reason)} — scanning paused. (Bot is alive.)", ts)
                track_only(state, config, ts)
                continue

            # ---- scan ----
            data = analyze_symbol_scalp(config, cfg, loop_mode=loop_mode)
            state.pop(f"err:{name}", None)
            result = data["result"]
            signal = result.get("signal")
            digits = data["digits"]

            track_trades(state, name, data["data5"], digits, ts)

            if not signal:
                print(f"[{name}] NO TRADE | bias={result.get('bias')} | price={data['price']}")
                for d in result.get("diag", []):
                    print(f"[{name}]   {d['side']} {d['score']}/{SCALP_MAX_SCORE} blocked: {d['blocked']}")

                watch = sorted(result.get("watch", []), key=lambda x: x["score"], reverse=True)
                if watch:
                    maybe_send_watch(state, name, watch[0], digits, ts)

                maybe_send_status(
                    state, name,
                    build_scalp_no_trade_message(name, result, data["price"], digits, note), ts)
                continue

            # Hard safety caps: avoid stacking correlated positions or digging out of a bad day.
            open_for_symbol = sum(1 for t in state.get("open_trades", []) if t.get("name") == name)
            open_total = len(state.get("open_trades", []))
            if open_for_symbol >= MAX_OPEN_PER_SYMBOL:
                print(f"[{name}] open-trade cap reached ({open_for_symbol})")
                continue
            if open_total >= MAX_OPEN_TOTAL:
                print(f"[{name}] global open-trade cap reached ({open_total})")
                continue
            today_r = daily_realised_r(state, name, ts)
            if DAILY_LOSS_LIMIT_R > 0 and today_r <= -DAILY_LOSS_LIMIT_R:
                print(f"[{name}] daily loss limit reached ({today_r:+.2f}R)")
                continue

            features = signal.get("features") or learning_features(signal, signal["close_dt"] - dt.timedelta(minutes=5))
            learn_reason, learn_evidence = learning_decision(state, name, features, ts)
            if learn_reason:
                print(f"[{name}] {learn_reason}")
                maybe_send_status(state, name, f"🧠 <b>{name} learning filter</b>\n{html.escape(learn_reason)}\nNo new trade taken.", ts)
                continue

            delay = (now_utc() - signal["close_dt"]).total_seconds() / 60
            if delay < 0 or delay > SCALP_STALE_MIN:
                print(f"[{name}] stale signal skipped: {delay:.1f}m")
                continue

            key = f"{name}:{signal['time']}:{signal['side']}"
            sent_keys = state.setdefault("sent_keys", [])
            if key in sent_keys:
                print(f"[{name}] duplicate skipped")
                continue

            pu = state.get(f"pause_until:{name}")
            if pu:
                try:
                    if ts < _from_iso(pu):
                        print(f"[{name}] paused after SL streak until {pu}")
                        continue
                except ValueError:
                    pass

            cd_key = f"sigts:{name}:{signal['side']}"
            last_sent = state.get(cd_key)
            if last_sent:
                try:
                    since = (ts - _from_iso(last_sent)).total_seconds() / 60
                    if since < SCALP_COOLDOWN_MIN:
                        print(f"[{name}] cooldown skipped ({since:.0f}m since last {signal['side']})")
                        continue
                except ValueError:
                    pass

            day_key = f"daycount:{name}:{day}"
            if state.get(day_key, 0) >= SCALP_MAX_SIGNALS_PER_DAY:
                print(f"[{name}] daily signal cap reached")
                continue

            # Validity check with a live quote (fails closed if the quote is unavailable).
            try:
                live_price = get_current_price(config["td_symbol"])
            except Exception as quote_exc:
                print(f"[{name}] live quote unavailable; signal skipped: {quote_exc}")
                continue
            ok, why = signal_still_valid(signal, data["data5"], live_price)
            if not ok:
                print(f"[{name}] signal no longer valid: {why}")
                continue

            # Second zone-conflict check (entry + live quote). Fails closed.
            zok, zwhy = final_zone_check(signal, live_price)
            if not zok:
                print(f"[{name}] ZONE CONFLICT, signal blocked: {zwhy}")
                notices = state.setdefault("zone_notices", [])
                if key not in notices:
                    notices.append(key)
                    del notices[:-60]
                    try:
                        send_telegram(f"🛡 <b>{name}</b> — {signal['side']} signal blocked\n{html.escape(zwhy)}")
                    except Exception as tg_exc:
                        print(f"[{name}] telegram error (zone notice): {tg_exc}")
                continue

            signal["lots"] = lot_size(cfg, signal["risk"])
            signal["next_news"] = next_news(ts, state)
            signal["caught_up_min"] = delay if result.get("caught_up") else 0

            send_telegram(build_scalp_signal_message(name, signal, result, digits))

            sent_keys.append(key)
            del sent_keys[:-60]
            state[cd_key] = _iso(ts)
            state[day_key] = state.get(day_key, 0) + 1
            register_trade(state, name, signal)
            save_state(state)

            print(f"[{name}] SENT {signal['side']} {signal['entry_type']} score={signal['score']}/{SCALP_MAX_SCORE}"
                  f"{' (catch-up)' if result.get('caught_up') else ''}")

        except Exception as exc:
            print(f"[{name}] ERROR: {exc}")
            n = state.get(f"err:{name}", 0) + 1
            state[f"err:{name}"] = n
            if n == ERROR_ALERT_AFTER:
                try:
                    send_telegram(
                        f"🛑 <b>{name}</b> — {n} scans failed in a row.\n"
                        f"Last error: {html.escape(str(exc)[:300])}")
                except Exception as tg_exc:
                    print(f"[{name}] telegram error (error alert): {tg_exc}")

    _flush_api(state, day0)
    save_state(state)


def sleep_until_next_5m():
    """Next 5M candle close (+ SCALP_FETCH_DELAY_SEC)."""
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

    run_once(loop_mode=True)

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

    if SCALP_LOOP_MINUTES > 0:
        run_scalp_loop()
        return

    run_once()


if __name__ == "__main__":
    main()
