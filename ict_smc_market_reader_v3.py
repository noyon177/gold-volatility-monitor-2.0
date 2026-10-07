import os
import sys
import json
import time
import datetime as dt
from pathlib import Path
import requests

# ============================================================
# ICT + SMC DAY-TRADING ASSISTANT  (v3)
#
# Environment variables:
#   TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   SEND_STATUS_UPDATES (1/0)   TEST_TELEGRAM (1/0)
#
# Flow:   4H bias -> 30M structure / S-R -> 15M setup -> 5M confirmation
#
# What is new in v3
#   * Tight, structure-based SL (sweep wick / order-block edge)
#   * Limit entry at OB/FVG midpoint when price has run away from the zone
#   * TP1 (1.5R, take partial, move SL to BE) + TP2 (next liquidity, >= 2R)
#   * Grade A / B signals, daily cap (target 3-4/day), cooldown, one live
#     trade per symbol
#   * Trade tracker: Telegram updates when entry fills / TP1 / TP2 / SL
#   * Daily report with win-rate and total R
#   * Dead-market filter, weekend filter, API throttling (free quota safe)
#   * 24h scanning (no session filter) - Gold pauses only when market is closed
#   * High-impact news: auto calendar, Telegram warning 60 + 15 min before,
#     no new signals from 30 min before to 15 min after the release
#   * 4H / 30M candle cache (ict_smc_cache.json) to save API quota
#
# Scoring (max 12):
#   Context      (max 5): HTF bias 2, 30M structure 1, P/D 1, S/R 1
#   Trigger      (max 5): sweep 2, BOS/CHoCH 1, displacement 1, OB/FVG 1
#   Confirmation (max 2): 15M EMA 1, 5M pinbar/EMA 1
#   Gates: sweep or structure break, context >= MIN_CONTEXT, < 2 conflicts
# ============================================================

SYMBOLS = [
    # session_filter False = 24h scan (Gold still sleeps on weekends: market closed)
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 2, "session_filter": False, "weekdays_only": True, "news_ccy": ["USD"]},
    {"name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 1, "session_filter": False, "weekdays_only": False, "news_ccy": ["USD"]},
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
TRACK_BARS = 120

API_MIN_GAP = 7.8          # seconds between API calls (free plan = 8 calls/min)
RATE_LIMIT_WAIT = 20
API_RETRIES = 3

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
DISPLACEMENT_WINDOW = 3

FVG_MIN_ATR = 0.10

SR_CLUSTER_ATR = 0.35
SR_MIN_TOUCHES = 2
SR_MAX_LEVELS = 3

SWEEP_WINDOW = 3
SWEEP_REF_BARS = 20
SWEEP_MIN_DEPTH_ATR = 0.02
SWEEP_MAX_DEPTH_ATR = 1.50

BREAK_WINDOW = 3

# ---------------- SIGNAL QUALITY ----------------
MIN_SCORE = 7              # Grade B starts here
GRADE_A_SCORE = 9          # Grade A starts here
MAX_SCORE = 12
MIN_CONTEXT = 3
MAX_LOCATION_ATR = 1.50
MIN_ATR_RATIO = 0.60       # skip dead market: ATR < 60% of its 50-bar average

# ---------------- DAILY LIMITS (day = Dhaka day) ----------------
MAX_SIGNALS_PER_DAY = 4
MAX_SIGNALS_PER_SYMBOL = 3
COOLDOWN_MIN = 45
ONE_LIVE_TRADE_PER_SYMBOL = True

# ---------------- RISK / TARGETS (all in 15M ATR) ----------------
SL_BUFFER_ATR = 0.10
MIN_SL_ATR = 0.50
MAX_SL_ATR = 2.00          # tight SL: wider than this = skip
TP1_RR = 1.5
MIN_RR = 2.0               # TP2 minimum
MAX_RR = 5.0
DEFAULT_TP2_RR = 3.0
TP_FRONTRUN_ATR = 0.05     # place TP just before the liquidity level
PARTIAL_AT_TP1 = 0.5       # share of position closed at TP1 (for R stats)

LIMIT_MIN_DIST_ATR = 0.15
LIMIT_MAX_DIST_ATR = 1.20
LIMIT_EXPIRY_MIN = 90
TRADE_TIMEOUT_H = 10       # day trade: close manually after this

# ---------------- TIME FILTERS ----------------
SESSIONS_UTC = [(6, 10), (12, 16)]   # only used when a symbol has session_filter True

# ---------------- NEWS ----------------
# High-impact news is pulled automatically from the free ForexFactory weekly
# calendar feed. If the feed is unreachable the bot keeps running and only
# the manual list below is used.
NEWS_AUTO = os.environ.get("NEWS_AUTO", "1") == "1"
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_IMPACTS = {"High"}
NEWS_REFRESH_H = 6                 # re-download the calendar at most every N hours
NEWS_WARN_MIN = (60, 15)           # alert this many minutes before the news (each once)
NEWS_BLOCK_BEFORE_MIN = 30         # no new signals from 30 min before ...
NEWS_BLOCK_AFTER_MIN = 15          # ... until 15 min after the release
NEWS_BLACKOUTS_UTC = []            # manual extras, e.g. "2026-10-09 12:30"
STALE_SIGNAL_MIN = 14

STATUS_INTERVAL_H = 4      # "bot alive / no trade" message every N hours
SEND_STATUS_UPDATES = os.environ.get("SEND_STATUS_UPDATES", "1") == "1"

DAILY_REPORT_UTC_HOUR = 17  # = 23:00 Dhaka

RUN_FOREVER = False
LOOP_SECONDS = 60

STATE_FILE = Path("ict_smc_state.json")
CACHE_FILE = Path("ict_smc_cache.json")   # saves 4H / 30M candles -> far fewer API calls

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
LOCAL = dt.timezone(dt.timedelta(hours=6))  # Asia/Dhaka
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/3.0"})


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():
    return dt.datetime.now(UTC)


def day_key(ts):
    return ts.astimezone(LOCAL).strftime("%Y-%m-%d")


def fmt_local(ts):
    return ts.astimezone(LOCAL).strftime("%H:%M")


def parse_iso(s):
    return dt.datetime.fromisoformat(s)


def require_credentials():
    missing = [
        k for k, v in (
            ("TWELVE_DATA_API_KEY", TWELVE_DATA_API_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ) if not v
    ]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def in_session(ts):
    h = ts.hour + ts.minute / 60
    return any(start <= h < end for start, end in SESSIONS_UTC)


def market_open(config, ts):
    if not config.get("weekdays_only"):
        return True
    wd = ts.weekday()
    if wd == 5:
        return False
    if wd == 4 and ts.hour >= 21:
        return False
    if wd == 6 and ts.hour < 22:
        return False
    return True


# ============================================================
# NEWS CALENDAR + ALERTS
# ============================================================

def manual_news_events():
    events = []
    for s in NEWS_BLACKOUTS_UTC:
        when = dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        events.append({"title": "Manual news event", "ccy": "ALL", "time": when.isoformat(), "impact": "High"})
    return events


def refresh_news(state):
    """Download the weekly calendar (cached in state). Never raises."""
    if not NEWS_AUTO:
        return
    news = state["news"]
    fetched = news.get("fetched")
    if fetched and (now_utc() - parse_iso(fetched)).total_seconds() < NEWS_REFRESH_H * 3600:
        return
    try:
        response = SESSION.get(NEWS_FEED_URL, timeout=20)
        response.raise_for_status()
        rows = response.json()
        events = []
        for row in rows:
            if row.get("impact") not in NEWS_IMPACTS:
                continue
            when = parse_iso(row["date"]).astimezone(UTC)
            events.append({
                "title": row.get("title", "News"), "ccy": row.get("country", ""),
                "time": when.isoformat(), "impact": row.get("impact"),
                "forecast": row.get("forecast", ""), "previous": row.get("previous", ""),
            })
        news["events"] = events
        news["fetched"] = now_utc().isoformat()
        save_state(state)
        print(f"[NEWS] calendar updated: {len(events)} high-impact events this week")
    except Exception as exc:
        # keep old events, retry in ~30 min instead of hammering the feed
        news["fetched"] = (now_utc() - dt.timedelta(hours=NEWS_REFRESH_H) + dt.timedelta(minutes=30)).isoformat()
        print(f"[NEWS] calendar fetch failed: {exc}")


def all_news_events(state):
    return state["news"].get("events", []) + manual_news_events()


def relevant_events(state, config):
    ccys = set(config.get("news_ccy", ["USD"])) | {"ALL"}
    return [e for e in all_news_events(state) if e["ccy"] in ccys]


def in_news_blackout(state, config, ts):
    for e in relevant_events(state, config):
        delta = (parse_iso(e["time"]) - ts).total_seconds() / 60   # +ve = news in future
        if -NEWS_BLOCK_AFTER_MIN <= delta <= NEWS_BLOCK_BEFORE_MIN:
            return e
    return None


def maybe_send_news_alerts(state, ts):
    """One alert per event per warning level (60 min, 15 min before)."""
    sent = state["news"].setdefault("alerted", {})
    for e in all_news_events(state):
        delta = (parse_iso(e["time"]) - ts).total_seconds() / 60
        if delta < 0:
            continue
        due = [w for w in NEWS_WARN_MIN if delta <= w]
        if not due:
            continue
        level = min(due)
        key = f"{e['time']}|{e['title']}|{e['ccy']}|{level}"
        if key in sent:
            continue
        # if a tighter warning is already due, don't send the older one late
        for w in NEWS_WARN_MIN:
            if w > level:
                sent[f"{e['time']}|{e['title']}|{e['ccy']}|{w}"] = True
        when = parse_iso(e["time"])
        extra = ""
        if e.get("forecast") or e.get("previous"):
            extra = f"\nForecast: {e.get('forecast') or '-'} · Previous: {e.get('previous') or '-'}"
        send_telegram(
            f"📰🔔 <b>HIGH IMPACT NEWS — {max(1, round(delta))} মিনিট পর</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>{e['ccy']} · {e['title']}</b>\n"
            f"⏰ {when.strftime('%H:%M')} UTC ({fmt_local(when)} Dhaka){extra}\n\n"
            f"⚠️ Gold / BTC-তে হঠাৎ বড় স্পাইক ও স্লিপেজ হতে পারে। "
            f"নতুন এন্ট্রি এড়িয়ে চলুন, খোলা ট্রেডে SL ঠিক রাখুন বা BE-তে আনুন। "
            f"বট নিউজের {NEWS_BLOCK_BEFORE_MIN} মিনিট আগে থেকে {NEWS_BLOCK_AFTER_MIN} মিনিট পর পর্যন্ত নতুন সিগন্যাল দেবে না।"
        )
        sent[key] = True
        save_state(state)
        print(f"[NEWS] alert sent: {e['title']} ({level}m)")
    # forget old alert keys
    for k in list(sent):
        if k.split("|")[0] < (ts - dt.timedelta(days=2)).isoformat():
            del sent[k]


# ============================================================
# TWELVE DATA
# ============================================================

_last_call = [0.0]


def throttle():
    wait = API_MIN_GAP - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def interval_minutes(interval):
    if interval.endswith("min"):
        return int(interval[:-3])
    if interval.endswith("h"):
        return int(interval[:-1]) * 60
    raise ValueError(interval)


def align_to(candles, interval, cutoff):
    minutes = interval_minutes(interval)
    return [c for c in candles if c["dt"] + dt.timedelta(minutes=minutes) <= cutoff]


def drop_unclosed(candles, interval):
    return align_to(candles, interval, now_utc())


_cache = {}
_cache_loaded = [False]
CACHEABLE = ("4h", "30min")


def _cache_load():
    if _cache_loaded[0]:
        return
    _cache_loaded[0] = True
    try:
        _cache.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass


def _cache_save():
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _cache_get(symbol, interval, outputsize):
    """Reuse higher-timeframe candles while no newer bar can have closed."""
    if interval not in CACHEABLE:
        return None
    _cache_load()
    entry = _cache.get(f"{symbol}|{interval}")
    if not entry or entry.get("size") != outputsize or not entry.get("rows"):
        return None
    candles = [dict(r, dt=parse_iso(r["time"].replace(" ", "T") + "+00:00")) for r in entry["rows"]]
    candles = drop_unclosed(candles, interval)
    if not candles:
        return None
    minutes = interval_minutes(interval)
    # last closed bar starts at T; the next bar closes at T + 2*interval
    if now_utc() >= candles[-1]["dt"] + dt.timedelta(minutes=2 * minutes):
        return None
    return candles


def _cache_put(symbol, interval, outputsize, candles):
    if interval not in CACHEABLE or not candles:
        return
    rows = [{k: c[k] for k in ("time", "open", "high", "low", "close")} for c in candles]
    _cache[f"{symbol}|{interval}"] = {"size": outputsize, "rows": rows}
    _cache_save()


def get_candles(symbol, interval, outputsize):
    require_credentials()

    cached = _cache_get(symbol, interval, outputsize)
    if cached:
        return cached

    data = {}
    for attempt in range(API_RETRIES):
        throttle()
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
        if data.get("code") == 429 and attempt < API_RETRIES - 1:
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

    candles = drop_unclosed(candles, interval)
    _cache_put(symbol, interval, outputsize, candles)
    return candles


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
            tr.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))

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

        swing_low = all(c["low"] <= x["low"] for x in lefts) and all(c["low"] < x["low"] for x in rights)
        swing_high = all(c["high"] >= x["high"] for x in lefts) and all(c["high"] > x["high"] for x in rights)

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
        "highs": highs, "lows": lows, "swing_high": None, "swing_low": None,
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
        "bias": structural_bias, "bos": bos, "choch": choch,
        "highs": highs, "lows": lows, "swing_high": h2, "swing_low": l2,
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
    """Sweep of range extreme / equal highs-lows in the last SWEEP_WINDOW
    candles, with price back inside the level on the latest close.
    Returns the wick extreme so the SL can be placed right behind it."""
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
    win_low = min(x["low"] for x in window)
    win_high = max(x["high"] for x in window)

    for age, c in enumerate(reversed(window)):
        for price, label in low_refs:
            depth = price - c["low"]
            if a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR and c["close"] > price and last_close > price:
                return {
                    "side": "SELL_SIDE", "price": price, "level": label, "age": age,
                    "wick": win_low,
                    "strength": "STRONG" if c["close"] > c["open"] else "NORMAL",
                }
        for price, label in high_refs:
            depth = c["high"] - price
            if a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR and c["close"] < price and last_close < price:
                return {
                    "side": "BUY_SIDE", "price": price, "level": label, "age": age,
                    "wick": win_high,
                    "strength": "STRONG" if c["close"] < c["open"] else "NORMAL",
                }
    return None


# ============================================================
# DISPLACEMENT / FVG / ORDER BLOCK
# ============================================================

def displacement(candles):
    if len(candles) < ATR_PERIOD + DISPLACEMENT_WINDOW:
        return None

    a_series = atr(candles, ATR_PERIOD)
    for k in range(1, DISPLACEMENT_WINDOW + 1):
        c = candles[-k]
        a = a_series[-k]
        if not a:
            continue
        body = candle_body(c)
        if body / candle_range(c) < DISPLACEMENT_BODY_RATIO or body < a * DISPLACEMENT_ATR_MULT:
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
            zl, zh = a1["high"], a3["low"]
            if gap >= a * FVG_MIN_ATR and not any(x["close"] < zl for x in later):
                return {"side": "BULLISH", "low": zl, "high": zh, "index": i, "time": a3["time"], "size": gap}

        if a3["high"] < a1["low"] and side in (None, "BEARISH"):
            gap = a1["low"] - a3["high"]
            zl, zh = a3["high"], a1["low"]
            if gap >= a * FVG_MIN_ATR and not any(x["close"] > zh for x in later):
                return {"side": "BEARISH", "low": zl, "high": zh, "index": i, "time": a3["time"], "size": gap}
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
        strong = (move_body / candle_range(move) >= DISPLACEMENT_BODY_RATIO) and move_body >= a[i + 1] * 0.90

        if (side in (None, "BULLISH") and strong and move["close"] > move["open"]
                and base["close"] < base["open"] and not any(x["close"] < base["low"] for x in later)):
            return {"side": "BULLISH", "low": base["low"], "high": base["high"], "time": base["time"]}

        if (side in (None, "BEARISH") and strong and move["close"] < move["open"]
                and base["close"] > base["open"] and not any(x["close"] > base["high"] for x in later)):
            return {"side": "BEARISH", "low": base["low"], "high": base["high"], "time": base["time"]}
    return None


# ============================================================
# PREMIUM / DISCOUNT, S/R, EMA
# ============================================================

def premium_discount(candles):
    structure = market_structure(candles)
    hi, lo = structure["swing_high"], structure["swing_low"]
    if not hi or not lo or hi["price"] <= lo["price"]:
        return {"zone": "UNKNOWN", "mid": None}

    mid = (hi["price"] + lo["price"]) / 2
    price = candles[-1]["close"]
    zone = "DISCOUNT" if price < mid else "PREMIUM" if price > mid else "EQUILIBRIUM"
    return {"zone": zone, "mid": mid}


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
    return {"support": supports[:SR_MAX_LEVELS], "resistance": resistances[:SR_MAX_LEVELS], "atr": a}


def nearest_level(levels, price):
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x["price"] - price))


def ema_state(candles):
    closes = [c["close"] for c in candles]
    e9 = ema(closes, EMA_FAST)
    e15 = ema(closes, EMA_SLOW)

    if len(closes) < EMA_SLOW + 4 or None in (e9[-1], e15[-1], e9[-4], e15[-4]):
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

    bull = bear = 0
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
        "bias": bias_data["bias"], "m15": m15, "m30": m30,
        "ema15": ema15_state, "ema5": ema5_state, "sweep": sweep,
        "fvg": detect_fvg(data15), "ob": detect_order_block(data15), "pd": pd, "sr": sr,
    }

    results = []

    for side in ("BUY", "SELL"):
        wanted = "BULLISH" if side == "BUY" else "BEARISH"
        reasons, conflicts = [], []
        context = trigger = confirm = 0

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
            reasons.append("discount zone")
        elif side == "SELL" and pd["zone"] == "PREMIUM":
            context += 1
            reasons.append("premium zone")
        elif pd["zone"] in ("PREMIUM", "DISCOUNT"):
            conflicts.append("poor P/D location")

        if side == "BUY" and support and abs(price - support["price"]) <= a15 * MAX_LOCATION_ATR:
            context += 1
            reasons.append("near support")
        if side == "SELL" and resistance and abs(price - resistance["price"]) <= a15 * MAX_LOCATION_ATR:
            context += 1
            reasons.append("near resistance")

        # ---------- TRIGGER (max 5) ----------
        sweep_ok = sweep is not None and (
            (side == "BUY" and sweep["side"] == "SELL_SIDE")
            or (side == "SELL" and sweep["side"] == "BUY_SIDE")
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
        if context < MIN_CONTEXT or len(conflicts) >= 2:
            continue

        total = context + trigger + confirm
        if total < MIN_SCORE:
            continue

        results.append({
            "side": side, "score": total, "grade": "A" if total >= GRADE_A_SCORE else "B",
            "context": context, "trigger": trigger, "confirm": confirm,
            "reasons": reasons, "conflicts": conflicts, "atr": a15, "price": price,
            "pin": pin, "m15_structure": m15, "m30_structure": m30,
            "ema15": ema15_state, "ema5": ema5_state,
            "sweep": sweep if sweep_ok else None,
            "fvg": fvg, "ob": ob, "pd": pd, "sr": sr,
        })

    if not results:
        return dict(base, signal=None)

    results.sort(key=lambda x: x["score"], reverse=True)
    if len(results) > 1 and results[0]["score"] == results[1]["score"]:
        return dict(base, signal=None, ambiguous=True, candidates=results)
    return dict(base, signal=results[0])


# ============================================================
# TRADE PLAN  (tight SL, limit entry, TP1 + TP2)
# ============================================================

def pick_entry_zone(side, price, a, ob, fvg):
    """Return the nearest OB/FVG that sits on the retrace side of price."""
    best = None
    for z in (ob, fvg):
        if not z:
            continue
        mid = (z["low"] + z["high"]) / 2
        dist = (price - mid) if side == "BUY" else (mid - price)
        if LIMIT_MIN_DIST_ATR * a <= dist <= LIMIT_MAX_DIST_ATR * a:
            if best is None or dist < best[0]:
                best = (dist, z, mid)
    return (best[1], best[2]) if best else (None, None)


def liquidity_targets(signal, entry):
    side = signal["side"]
    m30, m15, sr = signal["m30_structure"], signal["m15_structure"], signal["sr"]
    if side == "BUY":
        prices = [p["price"] for p in m30["highs"]] + [p["price"] for p in m15["highs"]]
        prices += [r["price"] for r in sr.get("resistance", [])]
        return sorted(p for p in prices if p > entry)
    prices = [p["price"] for p in m30["lows"]] + [p["price"] for p in m15["lows"]]
    prices += [s["price"] for s in sr.get("support", [])]
    return sorted((p for p in prices if p < entry), reverse=True)


def make_trade_plan(signal, data15, digits):
    side = signal["side"]
    buy = side == "BUY"
    a = signal["atr"]
    price = data15[-1]["close"]
    close_dt = data15[-1]["dt"] + dt.timedelta(minutes=15)

    # ---- entry ----
    zone, zone_mid = pick_entry_zone(side, price, a, signal.get("ob"), signal.get("fvg"))
    if zone:
        entry, mode = zone_mid, "LIMIT"
    else:
        entry, mode = price, "MARKET"

    # ---- stop loss: right behind the structure that must hold ----
    sweep = signal.get("sweep")
    if sweep:
        anchor = sweep["wick"]
    elif zone:
        anchor = zone["low"] if buy else zone["high"]
    else:
        last6 = data15[-6:]
        anchor = min(c["low"] for c in last6) if buy else max(c["high"] for c in last6)

    if zone and sweep:
        anchor = min(anchor, zone["low"]) if buy else max(anchor, zone["high"])

    sl = anchor - a * SL_BUFFER_ATR if buy else anchor + a * SL_BUFFER_ATR
    risk = (entry - sl) if buy else (sl - entry)
    if risk <= 0:
        return None
    if risk < a * MIN_SL_ATR:
        risk = a * MIN_SL_ATR
        sl = entry - risk if buy else entry + risk
    if risk > a * MAX_SL_ATR:
        return None

    # ---- targets ----
    sign = 1 if buy else -1
    tp1 = entry + sign * risk * TP1_RR

    tp2, tp2_label = None, "3R"
    for lvl in liquidity_targets(signal, entry):
        target = lvl - sign * a * TP_FRONTRUN_ATR
        rr = abs(target - entry) / risk
        if MIN_RR <= rr <= MAX_RR:
            tp2, tp2_label = target, "next liquidity"
            break
    if tp2 is None:
        tp2 = entry + sign * risk * DEFAULT_TP2_RR

    tp1_r = abs(tp1 - entry) / risk
    tp2_r = abs(tp2 - entry) / risk
    if tp2_r < MIN_RR:
        return None

    plan = dict(signal)
    plan.update({
        "mode": mode, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "tp1_r": tp1_r, "tp2_r": tp2_r, "tp2_label": tp2_label,
        "risk": risk, "risk_atr": risk / a,
        "time": data15[-1]["time"], "close_dt": close_dt,
    })
    return plan


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    require_credentials()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    last_exc = None
    for attempt in range(2):
        try:
            response = SESSION.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID, "text": message,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                },
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram rejected message: {result}")
            return True
        except Exception as exc:  # retry once on network / API hiccup
            last_exc = exc
            time.sleep(3)
    raise last_exc


# ============================================================
# STATE
# ============================================================

def load_state():
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    for key, default in (("status", {}), ("last_key", {}), ("last_signal", {}),
                         ("daily", {}), ("stats", {}), ("trades", []), ("report_day", ""),
                         ("news", {})):
        state.setdefault(key, default)
    return state


def save_state(state):
    cutoff = (now_utc() - dt.timedelta(days=14)).astimezone(LOCAL).strftime("%Y-%m-%d")
    for section in ("daily", "stats"):
        state[section] = {k: v for k, v in state[section].items() if k >= cutoff}
    keep_after = now_utc() - dt.timedelta(days=3)
    state["trades"] = [
        t for t in state["trades"]
        if t["status"] != "CLOSED" or parse_iso(t.get("closed_at", t["created"])) > keep_after
    ]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def live_trades(state, name):
    return [t for t in state["trades"] if t["name"] == name and t["status"] in ("PENDING", "OPEN")]


def daily_bucket(state, ts):
    return state["daily"].setdefault(day_key(ts), {"count": 0, "by": {}})


def stats_bucket(state, ts):
    return state["stats"].setdefault(
        day_key(ts), {"wins": 0, "losses": 0, "be": 0, "timeout": 0, "cancelled": 0, "r": 0.0}
    )


# ============================================================
# TRADE TRACKER
# ============================================================

def _close(t, result, r, ts_iso, events, note=""):
    t["status"] = "CLOSED"
    t["result"] = result
    t["r"] = r
    t["closed_at"] = ts_iso
    events.append((result, note))


def advance_trade(t, candles):
    """Walk 5M candles after the signal. Conservative: if SL and TP are both
    inside one candle, SL is assumed to be hit first."""
    events = []
    start = parse_iso(t["start"])
    last = parse_iso(t["checked_to"]) if t["checked_to"] else None
    buy = t["side"] == "BUY"
    expires = parse_iso(t["expires"]) if t.get("expires") else None
    timeout = parse_iso(t["timeout"])

    def adverse(c, level):
        return c["low"] <= level if buy else c["high"] >= level

    def favorable(c, level):
        return c["high"] >= level if buy else c["low"] <= level

    for c in candles:
        if c["dt"] < start or (last and c["dt"] <= last):
            continue
        t["checked_to"] = c["dt"].isoformat()
        end = c["dt"] + dt.timedelta(minutes=5)
        end_iso = end.isoformat()

        if t["status"] == "PENDING":
            if adverse(c, t["entry"]):
                if adverse(c, t["sl"]):
                    _close(t, "CANCELLED", 0.0, end_iso, events, "Price broke SL zone before entry fill.")
                else:
                    t["status"] = "OPEN"
                    events.append(("FILLED", ""))
            elif favorable(c, t["tp1"]):
                _close(t, "CANCELLED", 0.0, end_iso, events, "TP1 reached without entry fill - missed.")
            elif expires and end >= expires:
                _close(t, "CANCELLED", 0.0, end_iso, events, "Limit order expired.")
            if t["status"] == "CLOSED":
                break
            continue

        if t["status"] == "OPEN":
            if adverse(c, t["sl_cur"]):
                if t["tp1_hit"]:
                    _close(t, "BE", PARTIAL_AT_TP1 * t["tp1_r"], end_iso, events)
                else:
                    _close(t, "SL", -1.0, end_iso, events)
                break

            if not t["tp1_hit"] and favorable(c, t["tp1"]):
                t["tp1_hit"] = True
                t["sl_cur"] = t["entry"]
                events.append(("TP1", ""))
                # same candle also came back to entry: order unknown -> assume BE (conservative)
                if adverse(c, t["entry"]) and not favorable(c, t["tp2"]):
                    _close(t, "BE", PARTIAL_AT_TP1 * t["tp1_r"], end_iso, events)
                    break

            if t["tp1_hit"] and favorable(c, t["tp2"]):
                r = PARTIAL_AT_TP1 * t["tp1_r"] + (1 - PARTIAL_AT_TP1) * t["tp2_r"]
                _close(t, "TP2", r, end_iso, events)
                break

            if end >= timeout:
                move = (c["close"] - t["entry"]) if buy else (t["entry"] - c["close"])
                floating = move / t["risk"]
                r = (PARTIAL_AT_TP1 * t["tp1_r"] + (1 - PARTIAL_AT_TP1) * floating) if t["tp1_hit"] else floating
                _close(t, "TIMEOUT", r, end_iso, events)
                break

    return events


def apply_stats(state, t):
    s = stats_bucket(state, parse_iso(t["closed_at"]))
    res = t["result"]
    if res == "TP2":
        s["wins"] += 1
    elif res == "SL":
        s["losses"] += 1
    elif res == "BE":
        s["be"] += 1
    elif res == "TIMEOUT":
        s["timeout"] += 1
    elif res == "CANCELLED":
        s["cancelled"] += 1
    s["r"] = round(s["r"] + t["r"], 3)


def build_event_message(t, kind, note):
    d = t["digits"]
    head = f"{t['name']} {t['side']} (#{t['id']})"
    if kind == "FILLED":
        return f"📥 <b>{head}</b>\nLimit entry filled @ {t['entry']:.{d}f}\nSL {t['sl']:.{d}f} · TP1 {t['tp1']:.{d}f}"
    if kind == "TP1":
        return (f"🎯 <b>{head} — TP1 HIT</b>\nTP1 {t['tp1']:.{d}f} touched.\n"
                f"👉 Close partial, move SL to entry ({t['entry']:.{d}f}). Runner target: TP2 {t['tp2']:.{d}f}")
    if kind == "TP2":
        return f"✅ <b>{head} — TP2 HIT</b>\nFull target reached. Result ≈ <b>{t['r']:+.2f}R</b>"
    if kind == "SL":
        return f"❌ <b>{head} — STOP LOSS</b>\nSL {t['sl']:.{d}f} hit. Result: <b>-1.00R</b>"
    if kind == "BE":
        return f"➖ <b>{head} — BREAKEVEN EXIT</b>\nTP1 was banked, runner stopped at entry. Result ≈ <b>{t['r']:+.2f}R</b>"
    if kind == "TIMEOUT":
        return (f"⏱ <b>{head} — TIME EXIT</b>\nTrade is {TRADE_TIMEOUT_H}h old. Close/manage manually. "
                f"Approx result {t['r']:+.2f}R")
    if kind == "CANCELLED":
        return f"🚫 <b>{head} — SETUP CANCELLED</b>\n{note}"
    return f"{head}: {kind}"


def track_trades(state, config):
    name = config["name"]
    trades = live_trades(state, name)
    if not trades:
        return

    candles = get_candles(config["td_symbol"], ENTRY_INTERVAL, TRACK_BARS)
    for t in trades:
        events = advance_trade(t, candles)
        for kind, note in events:
            send_telegram(build_event_message(t, kind, note))
            print(f"[{name}] #{t['id']} {kind}")
        if t["status"] == "CLOSED":
            apply_stats(state, t)
        save_state(state)


# ============================================================
# MESSAGES
# ============================================================

def level_text(level, digits):
    if not level:
        return "—"
    return f"{level['price']:.{digits}f} ({level['touches']}x)"


def build_signal_message(name, signal, result, digits, trade_id):
    icon = "🟢" if signal["side"] == "BUY" else "🔴"
    grade_icon = "🏆" if signal["grade"] == "A" else "⚠️"
    sr = result.get("sr", {})
    reasons = "\n".join(f"✓ {x}" for x in signal["reasons"][:12])
    conflicts = "\n".join(f"• {x}" for x in signal["conflicts"]) or "None"

    sweep = signal.get("sweep")
    sweep_text = f"{sweep['side']} @ {sweep['price']:.{digits}f}" if sweep else "None"

    if signal["mode"] == "LIMIT":
        entry_line = f"Entry (LIMIT, ~{LIMIT_EXPIRY_MIN} মিনিট valid): <b>{signal['entry']:.{digits}f}</b>"
    else:
        entry_line = f"Entry (MARKET / এখনকার দাম): <b>{signal['entry']:.{digits}f}</b>"

    support = sr.get("support", [])
    resistance = sr.get("resistance", [])

    return (
        f"{icon} <b>{name} — {signal['side']} #{trade_id}</b>   {grade_icon} Grade {signal['grade']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧭 Bias: <b>{result['bias']}</b>\n"
        f"⭐ Score: <b>{signal['score']}/{MAX_SCORE}</b> "
        f"(ctx {signal['context']}/5 · trig {signal['trigger']}/5 · conf {signal['confirm']}/2)\n\n"

        f"<b>TRADE PLAN</b>\n"
        f"{entry_line}\n"
        f"🛑 SL: <b>{signal['sl']:.{digits}f}</b>  (risk {signal['risk']:.{digits}f} = {signal['risk_atr']:.2f} ATR)\n"
        f"🎯 TP1: <b>{signal['tp1']:.{digits}f}</b>  ({signal['tp1_r']:.1f}R) → partial close + SL to BE\n"
        f"🎯 TP2: <b>{signal['tp2']:.{digits}f}</b>  ({signal['tp2_r']:.1f}R, {signal['tp2_label']})\n\n"

        f"<b>ICT / SMC</b>\n"
        f"Sweep: {sweep_text}\n"
        f"Order Block: {signal['ob']['side'] if signal.get('ob') else 'None'} · "
        f"FVG: {signal['fvg']['side'] if signal.get('fvg') else 'None'}\n"
        f"P/D: {signal['pd']['zone']} · 15M BOS: {signal['m15_structure']['bos'] or '-'} · "
        f"CHoCH: {signal['m15_structure']['choch'] or '-'}\n\n"

        f"<b>Why</b>\n{reasons}\n\n"
        f"<b>30M S/R</b>  S: {level_text(support[0], digits) if support else '—'} · "
        f"R: {level_text(resistance[0], digits) if resistance else '—'}\n"
        f"<b>Conflicts</b>: {conflicts}\n\n"
        f"⏰ 15M close: {signal['time']} UTC ({fmt_local(signal['close_dt'])} Dhaka)\n"
        f"⚠️ Rule-based alert, not a guarantee. নিজে চার্ট দেখে সিদ্ধান্ত নিন; risk ছোট রাখুন।"
    )


def build_no_trade_message(name, result, price, digits):
    sweep, fvg, ob, pd = result.get("sweep"), result.get("fvg"), result.get("ob"), result.get("pd")
    m15 = result.get("m15") or {}
    return (
        f"🟡 <b>{name} — MARKET READER</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{price:.{digits}f}</b> · Bias: <b>{result.get('bias', 'UNKNOWN')}</b>\n"
        f"15M BOS: {m15.get('bos') or 'None'} · CHoCH: {m15.get('choch') or 'None'}\n"
        f"Sweep: {sweep['side'] if sweep else 'None'} · OB: {ob['side'] if ob else 'None'} · "
        f"FVG: {fvg['side'] if fvg else 'None'} · P/D: {pd['zone'] if pd else 'UNKNOWN'}\n\n"
        f"<b>ACTION: NO TRADE</b> — setup মেলেনি। (Bot alive ✅)"
    )


def build_daily_report(state, ts):
    key = day_key(ts)
    s = state["stats"].get(key, {"wins": 0, "losses": 0, "be": 0, "timeout": 0, "cancelled": 0, "r": 0.0})
    sent = state["daily"].get(key, {"count": 0, "by": {}})
    still_live = [t for t in state["trades"] if t["status"] in ("PENDING", "OPEN")]
    decided = s["wins"] + s["losses"]
    wr = f"{s['wins'] / decided * 100:.0f}%" if decided else "—"

    lines = [
        f"📊 <b>Daily Report — {key}</b>",
        "━━━━━━━━━━━━━━━━",
        f"Signals sent: <b>{sent['count']}</b> " + " · ".join(f"{k}: {v}" for k, v in sent["by"].items()),
        f"✅ TP2: {s['wins']}   ❌ SL: {s['losses']}   ➖ BE: {s['be']}   ⏱ Time: {s['timeout']}   🚫 Cancelled: {s['cancelled']}",
        f"Win-rate (TP2 vs SL): <b>{wr}</b>",
        f"Net result: <b>{s['r']:+.2f}R</b>",
    ]
    if still_live:
        live_txt = ", ".join(t["name"] + " #" + t["id"] for t in still_live)
        lines.append(f"Still live: {live_txt}")
    if sent["count"] == 0:
        lines.append("আজ কোনো quality setup আসেনি — না আসাও ঠিক আছে, জোর করে ট্রেড নয়।")
    return "\n".join(lines)


# ============================================================
# ANALYSIS
# ============================================================

def analyze_symbol(config):
    name, digits = config["name"], config["digits"]

    data4h = get_candles(config["td_symbol"], BIAS_INTERVAL, BIAS_BARS)
    data30 = get_candles(config["td_symbol"], STRUCTURE_INTERVAL, STRUCTURE_BARS)
    data15 = get_candles(config["td_symbol"], SETUP_INTERVAL, SETUP_BARS)
    data5 = get_candles(config["td_symbol"], ENTRY_INTERVAL, ENTRY_BARS)

    if not data15:
        raise RuntimeError("No closed 15M candles")

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
        series = [x for x in atr(data15, ATR_PERIOD)[-50:] if x]
        if series and signal["atr"] < MIN_ATR_RATIO * (sum(series) / len(series)):
            print(f"[{name}] dead market (low ATR), signal dropped")
            result["signal"] = None
        else:
            result["signal"] = make_trade_plan(signal, data15, digits)

    return {
        "name": name, "price": data15[-1]["close"], "digits": digits,
        "result": result, "last_candle_time": data15[-1]["time"],
    }


# ============================================================
# STATUS / TEST / REPORT
# ============================================================

def maybe_send_status(state, name, text, ts):
    if not SEND_STATUS_UPDATES:
        return
    block = f"{ts.strftime('%Y-%m-%d')} {ts.hour // STATUS_INTERVAL_H}"
    state_key = f"status:{name}"
    if state["status"].get(state_key) == block:
        return
    send_telegram(text)
    state["status"][state_key] = block
    save_state(state)
    print(f"[{name}] status message sent")


def maybe_send_daily_report(state, ts):
    key = day_key(ts)
    if ts.hour < DAILY_REPORT_UTC_HOUR or state["report_day"] == key:
        return
    if ts.weekday() >= 5 and state["daily"].get(key, {"count": 0})["count"] == 0:
        return
    send_telegram(build_daily_report(state, ts))
    state["report_day"] = key
    save_state(state)
    print("daily report sent")


def send_test_message():
    refresh_news(state := load_state())
    upcoming = sorted(
        (e for e in all_news_events(state) if parse_iso(e["time"]) > now_utc()),
        key=lambda e: e["time"],
    )[:3]
    news_txt = "\n".join(
        f"• {fmt_local(parse_iso(e['time']))} Dhaka — {e['ccy']} {e['title']}" for e in upcoming
    ) or "• (এই সপ্তাহের আর কোনো High-impact নিউজ পাওয়া যায়নি / ফিড কাজ করেনি)"
    send_telegram(
        "✅ <b>ICT SMC Market Reader v3</b>\n"
        "Telegram connection works.\n"
        f"Next high-impact news:\n{news_txt}\n"
        f"Time: {now_utc().strftime('%Y-%m-%d %H:%M')} UTC ({fmt_local(now_utc())} Dhaka)"
    )
    print("Test message sent")


# ============================================================
# MAIN
# ============================================================

def can_signal(state, name, ts):
    """Cheap checks that run BEFORE any analysis API calls."""
    day = daily_bucket(state, ts)
    if day["count"] >= MAX_SIGNALS_PER_DAY:
        return False, "daily cap reached"
    if day["by"].get(name, 0) >= MAX_SIGNALS_PER_SYMBOL:
        return False, "symbol cap reached"
    if ONE_LIVE_TRADE_PER_SYMBOL and live_trades(state, name):
        return False, "trade already live"
    last = state["last_signal"].get(name)
    if last and (ts - parse_iso(last)).total_seconds() < COOLDOWN_MIN * 60:
        return False, "cooldown"
    return True, ""


def register_trade(state, name, signal, digits, ts):
    day = daily_bucket(state, ts)
    day["count"] += 1
    day["by"][name] = day["by"].get(name, 0) + 1
    trade_id = f"{day_key(ts)[5:].replace('-', '')}-{day['count']}"

    created = ts
    start = signal["close_dt"]
    trade = {
        "id": trade_id, "name": name, "side": signal["side"], "grade": signal["grade"],
        "digits": digits, "score": signal["score"], "mode": signal["mode"],
        "entry": signal["entry"], "sl": signal["sl"], "sl_cur": signal["sl"],
        "tp1": signal["tp1"], "tp2": signal["tp2"],
        "tp1_r": signal["tp1_r"], "tp2_r": signal["tp2_r"], "risk": signal["risk"],
        "status": "PENDING" if signal["mode"] == "LIMIT" else "OPEN",
        "tp1_hit": False, "checked_to": None,
        "created": created.isoformat(), "start": start.isoformat(),
        "expires": (start + dt.timedelta(minutes=LIMIT_EXPIRY_MIN)).isoformat() if signal["mode"] == "LIMIT" else None,
        "timeout": (start + dt.timedelta(hours=TRADE_TIMEOUT_H)).isoformat(),
    }
    state["trades"].append(trade)
    state["last_signal"][name] = created.isoformat()
    return trade_id


def run_once():
    state = load_state()

    # news first: refresh calendar (cheap, cached) and warn before big events
    try:
        refresh_news(state)
        maybe_send_news_alerts(state, now_utc())
    except Exception as exc:
        print(f"[NEWS] ERROR: {exc}")

    for config in SYMBOLS:
        name = config["name"]
        try:
            ts = now_utc()

            # 1) always follow live trades first (results, TP1 -> BE alerts)
            track_trades(state, config)

            # 2) cheap filters before any analysis call
            if not market_open(config, ts):
                print(f"[{name}] market closed, skipped")
                continue
            if config.get("session_filter") and not in_session(ts):
                print(f"[{name}] outside kill zone, skipped")
                maybe_send_status(state, name,
                                  f"⚪ <b>{name}</b>\nOutside kill zone — scanning paused. (Bot alive ✅)", ts)
                continue
            blocking = in_news_blackout(state, config, ts)
            if blocking:
                print(f"[{name}] news blackout ({blocking['title']}), skipped")
                maybe_send_status(state, name,
                                  f"⚪ <b>{name}</b>\nNews ({blocking['ccy']} {blocking['title']}) — "
                                  f"নতুন সিগন্যাল সাময়িক বন্ধ। (Bot alive ✅)", ts)
                continue

            ok, why = can_signal(state, name, ts)
            if not ok:
                print(f"[{name}] no scan: {why}")
                continue

            # 3) analysis
            data = analyze_symbol(config)
            result = data["result"]
            signal = result.get("signal")

            if not signal:
                print(f"[{name}] NO TRADE | bias={result.get('bias')} | price={data['price']}")
                maybe_send_status(state, name,
                                  build_no_trade_message(name, result, data["price"], data["digits"]), ts)
                continue

            delay = (now_utc() - signal["close_dt"]).total_seconds() / 60
            if delay < 0 or delay > STALE_SIGNAL_MIN:
                print(f"[{name}] stale signal skipped: {delay:.1f}m")
                continue

            key = f"{name}:{signal['time']}:{signal['side']}"
            if state["last_key"].get(name) == key:
                print(f"[{name}] duplicate skipped")
                continue

            trade_id = register_trade(state, name, signal, data["digits"], now_utc())
            try:
                send_telegram(build_signal_message(name, signal, result, data["digits"], trade_id))
            except Exception:
                # message not delivered -> do not keep the trade / quota
                state["trades"].pop()
                day = daily_bucket(state, ts)
                day["count"] -= 1
                day["by"][name] -= 1
                state["last_signal"].pop(name, None)
                raise

            state["last_key"][name] = key
            save_state(state)
            print(f"[{name}] SENT {signal['side']} grade={signal['grade']} score={signal['score']}/{MAX_SCORE}")

        except Exception as exc:
            print(f"[{name}] ERROR: {exc}")

    try:
        maybe_send_daily_report(state, now_utc())
    except Exception as exc:
        print(f"[REPORT] ERROR: {exc}")
    save_state(state)


def main():
    require_credentials()

    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        send_test_message()
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
