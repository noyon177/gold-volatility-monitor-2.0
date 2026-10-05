#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ICT + SMC MARKET MOVEMENT READER  (v3 - 24h mode)

Environment variables:
  TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (required)
  ACCOUNT_BALANCE, RISK_PERCENT        (optional, lot size calculator)
  SEND_STATUS_UPDATES=0/1, STATUS_EVERY_HOURS=4, BOT_CONFIG=bot_config.json

Strategy:
  4H bias -> 30M structure / key liquidity -> 15M setup -> 5M confirmation

Scoring (max 14):
  Context      (max 6): HTF bias 2, 30M structure 1, P/D 1, S/R 1, daily-open side 1
  Trigger      (max 6): sweep 2, key-liquidity sweep 1, BOS/CHoCH 1, displacement 1, OB/FVG 1
  Confirmation (max 2): 15M EMA 1, 5M pinbar/EMA 1
  Gates: sweep or structure break, context >= MIN_CONTEXT, < 2 conflicts

24h mode: there is NO session gate. Kill zones (NY time, DST-safe) are only labels
and sources of liquidity levels. Only real "market closed" times (gold weekend /
daily break), high-impact news windows and bad volatility pause new signals.

CLI:
  python ict_smc_market_reader_v3.py                   run once (GitHub Actions)
  python ict_smc_market_reader_v3.py --loop            run forever
  python ict_smc_market_reader_v3.py --test            Telegram test message
  python ict_smc_market_reader_v3.py --backtest [--symbol XAUUSD] [--days 14] [--send]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import html
import json
import logging
import math
import os
import statistics
import sys
import time
import datetime as dt
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

# ============================================================
# CONFIG  (everything can be overridden from bot_config.json)
# ============================================================

@dataclass
class Symbol:
    name: str
    td_symbol: str
    digits: int = 2
    market: str = "FX"            # "FX" = weekend + daily break closed, "CRYPTO" = 24/7
    contract_size: float = 100.0  # units per 1.00 lot (XAU: 100 oz, BTC: 1 coin)
    news_filter: bool = True
    min_lot: float = 0.01
    lot_step: float = 0.01


SYMBOLS: List[Symbol] = [
    Symbol("XAUUSD", "XAU/USD", digits=3, market="FX", contract_size=100.0),
    Symbol("BTCUSD", "BTC/USD", digits=3, market="CRYPTO", contract_size=1.0),
]

# ---------------- DATA ----------------
BIAS_INTERVAL = "4h"
STRUCTURE_INTERVAL = "30min"
SETUP_INTERVAL = "15min"
ENTRY_INTERVAL = "5min"

BIAS_BARS = 180
STRUCTURE_BARS = 400          # ~8 days, needed for PDH/PDL + weekly open
SETUP_BARS = 220
ENTRY_BARS = 180

RATE_LIMIT_WAIT = 20          # base wait (s) after a 429
RETRY_TRIES = 4
RETRY_BASE_SEC = 2.0
CALLS_PER_MIN = 7             # Twelve Data free plan = 8 / minute
DAILY_API_LIMIT = 780         # free plan = 800 / day (UTC)
STALE_DATA_MIN = 45           # newest closed 15M candle older than this = no fresh data

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

# Kill zones in NEW YORK time (DST handled by zoneinfo). Used for labels + session ranges.
KILLZONES_NY = {"ASIA": (20, 24), "LONDON": (2, 5), "NEW_YORK": (7, 10)}

# ---------------- SIGNAL QUALITY ----------------
MIN_SCORE = 9
MAX_SCORE = 14
MIN_CONTEXT = 3
MAX_LOCATION_ATR = 1.50

# ---------------- VOLATILITY FILTER ----------------
VOL_MAX_RATIO = 2.5           # current 15M ATR / median ATR  (spike)
VOL_MIN_RATIO = 0.45          # dead market

# ---------------- ENTRY ----------------
ENTRY_MODE = "AUTO"           # AUTO = limit at OB/FVG midpoint if available, else market
                              # LIMIT = only limit entries, MARKET = always market
MAX_LIMIT_DIST_ATR = 1.50     # zone midpoint must be within this many ATR of price
LIMIT_EXPIRE_MIN = 240        # unfilled limit order is cancelled after this

# ---------------- RISK / TARGETS (15M ATR) ----------------
SL_BUFFER_ATR = 0.12
MIN_SL_ATR = 0.60
MAX_SL_ATR = 3.00
TP_BUFFER_ATR = 0.10          # TP sits just before the target liquidity
MIN_RR = 1.5                  # target closer than this -> skip to next / cancel signal
MAX_RR = 4.0                  # cap
RISK_REWARD = 2.0             # used only when there is no structural target at all
TP1_R = 1.0                   # TP1 = 1R : partial close + SL to break-even
PARTIAL_PCT = 0.5             # fraction closed at TP1
TRADE_MAX_HOURS = 72          # open trade is force-closed after this

# ---------------- ACCOUNT / LIMITS ----------------
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "0") or 0)
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "1") or 1)
MAX_SIGNALS_PER_DAY = 6       # per symbol (NY calendar day)
DAILY_MAX_LOSS_R = 3.0        # stop new signals for the day after -3R
COOLDOWN_MIN = 45             # minutes between signals on the same symbol
MAX_ACTIVE_PER_SYMBOL = 1

# ---------------- NEWS ----------------
AUTO_NEWS = True
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_COUNTRIES = ["USD"]
NEWS_REFRESH_HOURS = 6
NEWS_BEFORE_MIN = 30
NEWS_AFTER_MIN = 30
NEWS_BLACKOUTS_UTC: List[str] = []   # manual extras, e.g. "2026-10-09 12:30"

# ---------------- ALERT / REPORT ----------------
STALE_SIGNAL_MIN = 14         # must be < 15; with cron */15 this is fine
SEND_STATUS_UPDATES = os.environ.get("SEND_STATUS_UPDATES", "1") == "1"
STATUS_EVERY_HOURS = int(os.environ.get("STATUS_EVERY_HOURS", "4") or 4)
SEND_CHART = True
CHART_BARS = 70
ERROR_ALERT_COOLDOWN_MIN = 60
REPORT_TZ = "Asia/Dhaka"
REPORT_HOUR = 22              # daily report after this local hour
WEEKLY_REPORT_WEEKDAY = 6     # 0=Mon ... 6=Sun
COMMAND_MAX_AGE_MIN = 360

RUN_FOREVER = False
LOOP_SECONDS = 300

STATE_FILE = Path("ict_smc_state.json")
CACHE_FILE = Path("candle_cache.json")
TRADE_LOG_FILE = Path("signals_log.csv")
CONFIG_FILE = Path(os.environ.get("BOT_CONFIG", "bot_config.json"))

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ICT-SMC-Market-Reader/3.0"})

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ict_smc")

FILLED_RESULTS = ("TP", "SL", "BE", "TIMEOUT")
CLOSED_STATES = ("CLOSED", "CANCELLED")


def load_overrides(path: Optional[Path] = None) -> None:
    """Override any UPPERCASE constant above from bot_config.json."""
    path = path or CONFIG_FILE
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("config file ignored: %s", exc)
        return
    g = globals()
    for key, val in data.items():
        if key == "SYMBOLS":
            g["SYMBOLS"] = [Symbol(**item) for item in val]
        elif key.isupper() and key in g and not key.endswith("_FILE"):
            g[key] = val
        else:
            log.warning("unknown config key ignored: %s", key)


# ============================================================
# BASIC HELPERS
# ============================================================

class QuotaExceeded(Exception):
    pass


class DataStale(Exception):
    pass


class NotEnoughData(Exception):
    pass


class RetryableError(Exception):
    def __init__(self, msg: str, wait: Optional[float] = None):
        super().__init__(msg)
        self.wait = wait


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(d: dt.datetime) -> str:
    return d.isoformat()


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_ny(ts: dt.datetime) -> dt.datetime:
    return ts.astimezone(NY)


def esc(x: Any) -> str:
    return html.escape(str(x))


def require_credentials(telegram: bool = True) -> None:
    missing = []
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if telegram and not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if telegram and not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def with_retry(fn, tries: int = RETRY_TRIES, base: float = RETRY_BASE_SEC, label: str = "request"):
    """Exponential backoff for network errors and rate limits."""
    for i in range(tries):
        try:
            return fn()
        except (requests.RequestException, RetryableError) as exc:
            if i == tries - 1:
                raise
            wait = getattr(exc, "wait", None) or base * (2 ** i)
            log.warning("%s failed (%s) - retry in %.0fs", label, exc, wait)
            time.sleep(wait)


# ---------------- sessions / market hours ----------------

def session_name(ts: dt.datetime) -> str:
    n = to_ny(ts)
    h = n.hour + n.minute / 60
    labels = {"ASIA": "Asia", "LONDON": "London", "NEW_YORK": "New York"}
    for key, (s, e) in KILLZONES_NY.items():
        if s <= h < e:
            return labels[key] + " KZ"
    return "Off-hours"


def market_open(sym: Symbol, ts: dt.datetime) -> bool:
    """Gold: closed Fri 17:00 NY -> Sun 18:00 NY and daily 17:00-18:00 NY. Crypto: always."""
    if sym.market == "CRYPTO":
        return True
    n = to_ny(ts)
    wd = n.weekday()
    h = n.hour + n.minute / 60
    if wd == 5:
        return False
    if wd == 4 and h >= 17:
        return False
    if wd == 6 and h < 18:
        return False
    if wd in (0, 1, 2, 3) and 17 <= h < 18:
        return False
    return True


# ============================================================
# TWELVE DATA  (cache + rate limit + quota + retry)
# ============================================================

def interval_minutes(interval: str) -> int:
    if interval.endswith("min"):
        return int(interval[:-3])
    if interval.endswith("h"):
        return int(interval[:-1]) * 60
    raise ValueError(interval)


def drop_unclosed(candles: List[dict], interval: str, now: Optional[dt.datetime] = None) -> List[dict]:
    minutes = interval_minutes(interval)
    cutoff = now or now_utc()
    return [c for c in candles if c["dt"] + dt.timedelta(minutes=minutes) <= cutoff]


def align_to(candles: List[dict], interval: str, cutoff: dt.datetime) -> List[dict]:
    """Keep only candles that had already closed at `cutoff`."""
    minutes = interval_minutes(interval)
    return [c for c in candles if c["dt"] + dt.timedelta(minutes=minutes) <= cutoff]


_CACHE: Optional[dict] = None
_CALL_TIMES: deque = deque()


def _cache() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _CACHE = {}
    return _CACHE


def flush_cache() -> None:
    if _CACHE is None:
        return
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CACHE, separators=(",", ":")), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError as exc:
        log.warning("cache not saved: %s", exc)


def api_usage() -> dict:
    c = _cache()
    today = now_utc().strftime("%Y-%m-%d")
    u = c.get("_usage")
    if not u or u.get("date") != today:
        u = {"date": today, "count": 0}
        c["_usage"] = u
    return u


def _throttle() -> None:
    while True:
        t = time.time()
        while _CALL_TIMES and t - _CALL_TIMES[0] > 60:
            _CALL_TIMES.popleft()
        if len(_CALL_TIMES) < CALLS_PER_MIN:
            break
        time.sleep(max(0.5, 60 - (t - _CALL_TIMES[0]) + 0.5))
    _CALL_TIMES.append(time.time())


def _decode(rows: List[dict]) -> List[dict]:
    out = []
    for r in rows:
        out.append({
            "time": r["time"],
            "dt": dt.datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
        })
    return out


def _fetch_td(symbol: str, interval: str, size: int, live: bool) -> List[dict]:
    if live and api_usage()["count"] >= DAILY_API_LIMIT:
        raise QuotaExceeded(f"daily API budget used ({DAILY_API_LIMIT})")

    def call():
        _throttle()
        resp = SESSION.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": symbol, "interval": interval, "outputsize": size,
                    "timezone": "UTC", "apikey": TWELVE_DATA_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        code = data.get("code")
        if code == 429:
            raise RetryableError("rate limited", wait=RATE_LIMIT_WAIT)
        if isinstance(code, int) and code >= 500:
            raise RetryableError(f"server error {code}")
        if "values" not in data:
            raise RuntimeError(f"Twelve Data error: {data}")
        return data

    data = with_retry(call, label=f"TwelveData {symbol} {interval}")
    if live:
        api_usage()["count"] += 1
    return [
        {"time": r["datetime"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
        for r in reversed(data["values"])
    ]


def get_candles(symbol: str, interval: str, size: int, live: bool = True) -> List[dict]:
    """Closed candles only. Cached until a new candle can have closed."""
    require_credentials(telegram=False)
    minutes = interval_minutes(interval)
    key = f"{symbol}|{interval}"
    entry = _cache().get(key) if live else None

    if entry and entry.get("size", 0) >= size:
        cached = drop_unclosed(_decode(entry["rows"]), interval)
        if cached and now_utc() < cached[-1]["dt"] + dt.timedelta(minutes=2 * minutes):
            return cached[-size:]

    rows = _fetch_td(symbol, interval, size, live)
    if live:
        _cache()[key] = {"size": size, "rows": rows}
    return drop_unclosed(_decode(rows), interval)


def fetch_all(sym: Symbol) -> dict:
    d4 = get_candles(sym.td_symbol, BIAS_INTERVAL, BIAS_BARS)
    d30 = get_candles(sym.td_symbol, STRUCTURE_INTERVAL, STRUCTURE_BARS)
    d15 = get_candles(sym.td_symbol, SETUP_INTERVAL, SETUP_BARS)
    d5 = get_candles(sym.td_symbol, ENTRY_INTERVAL, ENTRY_BARS)

    if not d15:
        raise DataStale("no closed 15M candles")
    cutoff = d15[-1]["dt"] + dt.timedelta(minutes=interval_minutes(SETUP_INTERVAL))
    if now_utc() - cutoff > dt.timedelta(minutes=STALE_DATA_MIN):
        raise DataStale(f"latest 15M candle closed {cutoff:%Y-%m-%d %H:%M} UTC (market closed / feed stale)")

    return {
        "cutoff": cutoff,
        "d4h": align_to(d4, BIAS_INTERVAL, cutoff),
        "d30": align_to(d30, STRUCTURE_INTERVAL, cutoff),
        "d15": d15,
        "d5": align_to(d5, ENTRY_INTERVAL, cutoff),
        "d5_all": d5,
    }


# ============================================================
# INDICATORS
# ============================================================

def ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    out[period - 1] = sum(values[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr(candles: List[dict], period: int = 14) -> List[Optional[float]]:
    if len(candles) < period:
        return [None] * len(candles)
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            tr.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out: List[Optional[float]] = [None] * len(candles)
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(candles)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def candle_body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def candle_range(c: dict) -> float:
    return max(c["high"] - c["low"], 1e-12)


def pinbar(c: dict) -> Optional[str]:
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


def volatility_ok(data15: List[dict]) -> Tuple[bool, Optional[str]]:
    series = [x for x in atr(data15, ATR_PERIOD) if x]
    if len(series) < 60:
        return True, None
    cur = series[-1]
    med = statistics.median(series[-100:])
    if med <= 0:
        return True, None
    ratio = cur / med
    if ratio > VOL_MAX_RATIO:
        return False, f"ATR spike x{ratio:.1f}"
    if ratio < VOL_MIN_RATIO:
        return False, f"dead market x{ratio:.2f}"
    return True, None


# ============================================================
# PIVOTS / MARKET STRUCTURE
# ============================================================

def find_pivots(candles: List[dict], left: int = PIVOT_LEFT, right: int = PIVOT_RIGHT) -> List[dict]:
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


def market_structure(candles: List[dict]) -> dict:
    recent = candles[-STRUCTURE_LOOKBACK:]
    pivots = find_pivots(recent)
    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]

    empty = {"bias": "NEUTRAL", "bos": None, "choch": None, "highs": highs, "lows": lows,
             "swing_high": None, "swing_low": None}
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

    return {"bias": structural_bias, "bos": bos, "choch": choch, "highs": highs, "lows": lows,
            "swing_high": h2, "swing_low": l2}


# ============================================================
# LIQUIDITY  (equal highs/lows + KEY LEVELS: PDH/PDL, sessions, opens)
# ============================================================

def liquidity_levels(candles: List[dict]) -> dict:
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


def session_range(candles30: List[dict], now: dt.datetime, start_h: int, end_h: int) -> Optional[Tuple[float, float]]:
    """High/low of the most recent COMPLETED session (hours in New York time)."""
    ny_now = to_ny(now)
    groups: Dict[dt.date, List[dict]] = {}
    for c in candles30:
        n = to_ny(c["dt"])
        h = n.hour + n.minute / 60
        if start_h <= h < end_h:
            groups.setdefault(n.date(), []).append(c)
    for d in sorted(groups, reverse=True):
        end = dt.datetime(d.year, d.month, d.day, tzinfo=NY) + dt.timedelta(hours=end_h)
        if end <= ny_now:
            cs = groups[d]
            return max(c["high"] for c in cs), min(c["low"] for c in cs)
    return None


def key_levels(candles30: List[dict], now: dt.datetime) -> List[dict]:
    """Previous-day H/L, Asia/London/NY session H/L, daily open (00:00 NY), weekly open."""
    out: List[dict] = []
    if not candles30:
        return out
    today = to_ny(now).date()

    by_day: Dict[dt.date, List[dict]] = {}
    for c in candles30:
        by_day.setdefault(to_ny(c["dt"]).date(), []).append(c)

    prev_days = [d for d in by_day if d < today]
    if prev_days:
        cs = by_day[max(prev_days)]
        out.append({"name": "PDH", "price": max(c["high"] for c in cs), "kind": "high"})
        out.append({"name": "PDL", "price": min(c["low"] for c in cs), "kind": "low"})

    if today in by_day:
        out.append({"name": "DAILY_OPEN", "price": by_day[today][0]["open"], "kind": "open"})

    def week_id(ts: dt.datetime):
        return (to_ny(ts) + dt.timedelta(hours=6)).isocalendar()[:2]

    cur = week_id(now)
    wk = [c for c in candles30 if week_id(c["dt"]) == cur]
    if wk:
        first = to_ny(wk[0]["dt"]) + dt.timedelta(hours=6)
        if first.weekday() == 0 and first.hour == 0:      # Sunday 18:00 NY = week start
            out.append({"name": "WEEKLY_OPEN", "price": wk[0]["open"], "kind": "open"})

    tags = {"ASIA": "ASIA", "LONDON": "LONDON", "NEW_YORK": "NY"}
    for key, (s, e) in KILLZONES_NY.items():
        rng = session_range(candles30, now, s, e)
        if rng:
            out.append({"name": f"{tags[key]}_H", "price": rng[0], "kind": "high"})
            out.append({"name": f"{tags[key]}_L", "price": rng[1], "kind": "low"})
    return out


def detect_liquidity_sweep(candles: List[dict], key_lvls: Optional[List[dict]] = None) -> Optional[dict]:
    """
    Sweep of a key level (PDH/PDL/session H-L), equal highs/lows, or the 20-bar range extreme,
    within the last SWEEP_WINDOW candles, price back inside on the latest close.
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

    low_refs: List[Tuple[float, str, bool]] = []
    high_refs: List[Tuple[float, str, bool]] = []
    for k in key_lvls or []:
        if k["kind"] == "low":
            low_refs.append((k["price"], k["name"], True))
        elif k["kind"] == "high":
            high_refs.append((k["price"], k["name"], True))

    levels = liquidity_levels(candles[:-n])
    low_refs.append((ref_low, "RANGE_LOW", False))
    high_refs.append((ref_high, "RANGE_HIGH", False))
    if levels["equal_low"]:
        low_refs.append((levels["equal_low"], "EQUAL_LOW", False))
    if levels["equal_high"]:
        high_refs.append((levels["equal_high"], "EQUAL_HIGH", False))

    last_close = candles[-1]["close"]
    window = candles[-n:]

    # Collect every sweep in the window; a key-level sweep beats a minor range sweep
    # even if the minor one is on a newer candle. Ties: newest candle first.
    found = []
    for age, c in enumerate(reversed(window)):
        for price, label, is_key in low_refs:
            depth = price - c["low"]
            if (a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR
                    and c["close"] > price and last_close > price):
                found.append({"side": "SELL_SIDE", "price": price, "level": label, "key": is_key, "age": age,
                              "strength": "STRONG" if c["close"] > c["open"] else "NORMAL"})
        for price, label, is_key in high_refs:
            depth = c["high"] - price
            if (a * SWEEP_MIN_DEPTH_ATR <= depth <= a * SWEEP_MAX_DEPTH_ATR
                    and c["close"] < price and last_close < price):
                found.append({"side": "BUY_SIDE", "price": price, "level": label, "key": is_key, "age": age,
                              "strength": "STRONG" if c["close"] < c["open"] else "NORMAL"})
    if not found:
        return None
    return min(found, key=lambda s: (not s["key"], s["age"]))


# ============================================================
# DISPLACEMENT / FVG / ORDER BLOCK / PREMIUM-DISCOUNT / S-R / EMA
# ============================================================

def displacement(candles: List[dict]) -> Optional[str]:
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


def detect_fvg(candles: List[dict], side: Optional[str] = None) -> Optional[dict]:
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


def detect_order_block(candles: List[dict], side: Optional[str] = None) -> Optional[dict]:
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
        if (side in (None, "BULLISH") and strong and move["close"] > move["open"]
                and base["close"] < base["open"] and not any(x["close"] < base["low"] for x in later)):
            return {"side": "BULLISH", "low": base["low"], "high": base["high"], "time": base["time"]}
        if (side in (None, "BEARISH") and strong and move["close"] < move["open"]
                and base["close"] > base["open"] and not any(x["close"] > base["high"] for x in later)):
            return {"side": "BEARISH", "low": base["low"], "high": base["high"], "time": base["time"]}
    return None


def premium_discount(candles: List[dict]) -> dict:
    structure = market_structure(candles)
    hi, lo = structure["swing_high"], structure["swing_low"]
    if not hi or not lo or hi["price"] <= lo["price"]:
        return {"zone": "UNKNOWN", "mid": None}
    mid = (hi["price"] + lo["price"]) / 2
    price = candles[-1]["close"]
    zone = "DISCOUNT" if price < mid else "PREMIUM" if price > mid else "EQUILIBRIUM"
    return {"zone": zone, "mid": mid}


def build_sr(candles: List[dict]) -> dict:
    if len(candles) < 40:
        return {"support": [], "resistance": [], "atr": None}
    a = atr(candles, ATR_PERIOD)[-1]
    if not a:
        return {"support": [], "resistance": [], "atr": None}
    pivots = find_pivots(candles)
    tolerance = a * SR_CLUSTER_ATR
    clusters: List[List[dict]] = []
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


def nearest_level(levels: List[dict], price: float) -> Optional[dict]:
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x["price"] - price))


def ema_state(candles: List[dict]) -> dict:
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


def get_bias(data4h: List[dict], data30: List[dict]) -> dict:
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

def in_zone(price: float, zone: Optional[dict], tolerance: float) -> bool:
    if not zone:
        return False
    return zone["low"] - tolerance <= price <= zone["high"] + tolerance


def score_setup(bias_data: dict, data30: List[dict], data15: List[dict], data5: List[dict],
                key_lvls: Optional[List[dict]] = None) -> dict:
    key_lvls = key_lvls or []
    price = data15[-1]["close"]
    a15 = atr(data15, ATR_PERIOD)[-1]
    if not a15:
        return {"signal": None, "bias": bias_data["bias"]}

    m30 = market_structure(data30)
    m15 = market_structure(data15)
    ema15_state = ema_state(data15)
    ema5_state = ema_state(data5)

    sweep = detect_liquidity_sweep(data15, key_lvls)
    disp = displacement(data15)
    pd = premium_discount(data30)
    pin = pinbar(data5[-1])
    sr = build_sr(data30)
    eq = liquidity_levels(data30)
    dopen = next((k for k in key_lvls if k["name"] == "DAILY_OPEN"), None)

    support = nearest_level(sr["support"], price)
    resistance = nearest_level(sr["resistance"], price)

    base = {
        "bias": bias_data["bias"], "m15": m15, "m30": m30, "ema15": ema15_state, "ema5": ema5_state,
        "sweep": sweep, "fvg": detect_fvg(data15), "ob": detect_order_block(data15),
        "pd": pd, "sr": sr, "eq": eq, "key_levels": key_lvls,
    }

    results = []
    for side in ("BUY", "SELL"):
        wanted = "BULLISH" if side == "BUY" else "BEARISH"
        reasons: List[str] = []
        conflicts: List[str] = []
        context = trigger = confirm = 0

        # ---------- CONTEXT (max 6) ----------
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

        if dopen:
            if side == "BUY" and price < dopen["price"]:
                context += 1
                reasons.append("below daily open")
            elif side == "SELL" and price > dopen["price"]:
                context += 1
                reasons.append("above daily open")

        # ---------- TRIGGER (max 6) ----------
        sweep_ok = sweep is not None and (
            (side == "BUY" and sweep["side"] == "SELL_SIDE") or (side == "SELL" and sweep["side"] == "BUY_SIDE"))
        if sweep_ok:
            trigger += 2
            reasons.append(f"{'sell' if side == 'BUY' else 'buy'}-side liquidity sweep ({sweep['level']})")
            if sweep.get("key"):
                trigger += 1
                reasons.append("key liquidity taken")

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
            "side": side, "score": total, "context": context, "trigger": trigger, "confirm": confirm,
            "reasons": reasons, "conflicts": conflicts, "atr": a15, "price": price, "pin": pin,
            "m15_structure": m15, "m30_structure": m30, "ema15": ema15_state, "ema5": ema5_state,
            "sweep": sweep if sweep_ok else None, "fvg": fvg, "ob": ob, "pd": pd, "sr": sr,
            "eq": eq, "key_levels": key_lvls,
        })

    if not results:
        return dict(base, signal=None)
    results.sort(key=lambda x: x["score"], reverse=True)
    if len(results) > 1 and results[0]["score"] == results[1]["score"]:
        return dict(base, signal=None, ambiguous=True, candidates=results)
    return dict(base, signal=results[0])


# ============================================================
# TRADE PLAN  (limit entry at OB/FVG, smart TP, TP1 + BE)
# ============================================================

def choose_entry(signal: dict, price: float, a: float) -> Optional[Tuple[float, str, Optional[dict]]]:
    if ENTRY_MODE == "MARKET":
        return price, "MARKET", None
    side = signal["side"]
    best = None
    for zone, label in ((signal.get("ob"), "OB"), (signal.get("fvg"), "FVG")):
        if not zone:
            continue
        mid = (zone["low"] + zone["high"]) / 2
        if side == "BUY" and mid < price and price - mid <= a * MAX_LIMIT_DIST_ATR:
            pass
        elif side == "SELL" and mid > price and mid - price <= a * MAX_LIMIT_DIST_ATR:
            pass
        else:
            continue
        if best is None or abs(price - mid) < abs(price - best[0]):
            best = (mid, label, zone)
    if best:
        return best[0], "LIMIT", {"label": best[1], "low": best[2]["low"], "high": best[2]["high"]}
    if ENTRY_MODE == "LIMIT":
        return None
    return price, "MARKET", None


def tp_candidates(signal: dict, entry: float) -> List[Tuple[float, str]]:
    side = signal["side"]
    buy = side == "BUY"
    out: List[Tuple[float, str]] = []
    sr = signal.get("sr") or {}
    for lvl in (sr.get("resistance", []) if buy else sr.get("support", [])):
        out.append((lvl["price"], "S/R"))
    want = "high" if buy else "low"
    for k in signal.get("key_levels") or []:
        if k["kind"] == want:
            out.append((k["price"], k["name"]))
    eq = (signal.get("eq") or {}).get("equal_high" if buy else "equal_low")
    if eq:
        out.append((eq, "EQ_HIGH" if buy else "EQ_LOW"))
    if buy:
        return sorted([x for x in out if x[0] > entry], key=lambda x: x[0])
    return sorted([x for x in out if x[0] < entry], key=lambda x: -x[0])


def make_trade_plan(signal: dict, data15: List[dict]) -> Tuple[Optional[dict], Optional[str]]:
    a = signal["atr"]
    price = data15[-1]["close"]
    side = signal["side"]
    buy = side == "BUY"

    chosen = choose_entry(signal, price, a)
    if chosen is None:
        return None, "no OB/FVG zone for limit entry"
    entry, mode, zone = chosen

    if buy:
        structural = min(c["low"] for c in data15[-6:])
        if zone:
            structural = min(structural, zone["low"])
        sl = structural - a * SL_BUFFER_ATR
        risk = entry - sl
    else:
        structural = max(c["high"] for c in data15[-6:])
        if zone:
            structural = max(structural, zone["high"])
        sl = structural + a * SL_BUFFER_ATR
        risk = sl - entry
    if risk <= 0:
        return None, "invalid risk"
    risk_atr = risk / a
    if not (MIN_SL_ATR <= risk_atr <= MAX_SL_ATR):
        return None, f"SL distance {risk_atr:.2f} ATR out of range"

    sgn = 1 if buy else -1
    cands = tp_candidates(signal, entry)
    tp = rr = None
    tp_label = ""
    for lvl, label in cands:
        t = lvl - sgn * a * TP_BUFFER_ATR
        reward = (t - entry) * sgn
        if reward <= 0:
            continue
        r_ = reward / risk
        if r_ >= MIN_RR:
            if r_ > MAX_RR:
                t = entry + sgn * risk * MAX_RR
                r_ = MAX_RR
                label = f"{label} (capped {MAX_RR:g}R)"
            tp, rr, tp_label = t, r_, label
            break
    if tp is None:
        if cands:
            return None, f"nearest liquidity target gives < {MIN_RR}R"
        tp = entry + sgn * risk * RISK_REWARD
        rr = RISK_REWARD
        tp_label = f"fixed {RISK_REWARD:g}R (no target in range)"

    tp1 = entry + sgn * risk * TP1_R

    plan = dict(signal)
    plan.update({
        "entry": entry, "mode": mode, "zone": zone, "sl": sl, "tp1": tp1, "tp": tp,
        "rr": rr, "tp_label": tp_label, "risk": risk, "risk_atr": risk_atr,
        "time": data15[-1]["time"],
        "close_dt": data15[-1]["dt"] + dt.timedelta(minutes=15),
    })
    return plan, None


def position_size(sym: Symbol, risk_price: float) -> Optional[dict]:
    if ACCOUNT_BALANCE <= 0 or risk_price <= 0:
        return None
    risk_cash = ACCOUNT_BALANCE * RISK_PERCENT / 100.0
    raw = risk_cash / (risk_price * sym.contract_size)
    lots = math.floor(raw / sym.lot_step + 1e-9) * sym.lot_step
    warn = None
    if lots < sym.min_lot:
        lots = sym.min_lot
        warn = "min lot risks more than your %"
    return {"lots": round(lots, 4), "risk_cash": risk_cash,
            "real_risk": lots * risk_price * sym.contract_size, "warn": warn}


# ============================================================
# TRADE LIFECYCLE  (pure functions - shared by live bot and backtester)
# ============================================================

def new_trade(plan: dict, sym: Symbol, now: dt.datetime, pos: Optional[dict] = None) -> dict:
    market = plan["mode"] == "MARKET"
    close_dt = plan["close_dt"]
    return {
        "id": f"{sym.name}-{plan['time'].replace(' ', 'T')}-{plan['side']}",
        "symbol": sym.name, "side": plan["side"], "mode": plan["mode"],
        "status": "OPEN" if market else "PENDING",
        "created": iso(now), "cursor": iso(close_dt),
        "expire": iso(close_dt + dt.timedelta(minutes=LIMIT_EXPIRE_MIN)),
        "fill_time": iso(close_dt) if market else None,
        "entry": plan["entry"], "sl": plan["sl"], "sl_cur": plan["sl"],
        "tp1": plan["tp1"], "tp": plan["tp"], "rr": plan["rr"], "risk": plan["risk"],
        "tp1_r": TP1_R, "partial": PARTIAL_PCT, "tp1_hit": False,
        "score": plan["score"], "lots": pos["lots"] if pos else None,
        "digits": sym.digits, "last_price": plan["entry"],
        "result": None, "reason": None, "r": 0.0, "closed": None,
    }


def _r_multiple(trade: dict, exit_price: float) -> float:
    sgn = 1 if trade["side"] == "BUY" else -1
    rest = (exit_price - trade["entry"]) * sgn / trade["risk"]
    if trade["tp1_hit"]:
        return trade["partial"] * trade["tp1_r"] + (1 - trade["partial"]) * rest
    return rest


def _close(trade: dict, events: list, result: str, price: float, t_iso: str) -> None:
    if result == "TP":
        trade["tp1_hit"] = True
    trade["status"] = "CLOSED"
    trade["result"] = result
    trade["r"] = round(_r_multiple(trade, price), 3)
    trade["closed"] = t_iso
    events.append({"type": result, "price": price, "time": t_iso, "r": trade["r"]})


def _cancel(trade: dict, events: list, reason: str, t_iso: str) -> None:
    trade["status"] = "CANCELLED"
    trade["result"] = "CANCELLED"
    trade["reason"] = reason
    trade["r"] = 0.0
    trade["closed"] = t_iso
    events.append({"type": "CANCELLED", "reason": reason, "time": t_iso})


def advance_trade(trade: dict, candles: List[dict], minutes: int,
                  now: Optional[dt.datetime] = None) -> List[dict]:
    """
    Walk candles forward. Conservative intrabar rule: if SL and TP are both inside one
    candle, SL wins. A limit fill candle is checked for SL only.
    """
    events: List[dict] = []
    buy = trade["side"] == "BUY"
    cursor = parse_iso(trade["cursor"])
    step = dt.timedelta(minutes=minutes)
    last_end = None

    for c in candles:
        if trade["status"] in CLOSED_STATES:
            break
        if c["dt"] < cursor:
            continue
        end_iso = iso(c["dt"] + step)
        last_end = c["dt"] + step
        trade["last_price"] = c["close"]
        filled_now = False

        if trade["status"] == "PENDING":
            if c["dt"] >= parse_iso(trade["expire"]):
                _cancel(trade, events, "EXPIRED", end_iso)
                break
            touched = c["low"] <= trade["entry"] if buy else c["high"] >= trade["entry"]
            if not touched:
                ran = c["high"] >= trade["tp1"] if buy else c["low"] <= trade["tp1"]
                if ran:
                    _cancel(trade, events, "MISSED", end_iso)
                    break
                continue
            trade["status"] = "OPEN"
            trade["fill_time"] = iso(c["dt"])
            events.append({"type": "FILLED", "price": trade["entry"], "time": iso(c["dt"])})
            filled_now = True

        if not filled_now and trade["fill_time"] and \
                c["dt"] - parse_iso(trade["fill_time"]) > dt.timedelta(hours=TRADE_MAX_HOURS):
            _close(trade, events, "TIMEOUT", c["open"], iso(c["dt"]))
            break

        sl = trade["sl_cur"]
        sl_hit = c["low"] <= sl if buy else c["high"] >= sl
        if sl_hit:
            _close(trade, events, "BE" if trade["tp1_hit"] else "SL", sl, end_iso)
            break
        if filled_now:
            continue

        tp_hit = c["high"] >= trade["tp"] if buy else c["low"] <= trade["tp"]
        tp1_hit = c["high"] >= trade["tp1"] if buy else c["low"] <= trade["tp1"]
        if tp_hit:
            _close(trade, events, "TP", trade["tp"], end_iso)
            break
        if tp1_hit and not trade["tp1_hit"]:
            trade["tp1_hit"] = True
            trade["sl_cur"] = trade["entry"]
            events.append({"type": "TP1", "price": trade["tp1"], "time": end_iso})

    if trade["status"] not in CLOSED_STATES:
        if last_end:
            trade["cursor"] = iso(max(cursor, last_end))
        if now is not None:
            if trade["status"] == "PENDING" and now >= parse_iso(trade["expire"]):
                _cancel(trade, events, "EXPIRED", iso(now))
            elif trade["status"] == "OPEN" and trade["fill_time"] and \
                    now - parse_iso(trade["fill_time"]) > dt.timedelta(hours=TRADE_MAX_HOURS):
                _close(trade, events, "TIMEOUT", trade["last_price"], iso(now))
    return events


# ============================================================
# NEWS  (ForexFactory feed + manual blackouts)
# ============================================================

def refresh_news(state: dict, now: dt.datetime) -> List[dict]:
    manual = []
    for s in NEWS_BLACKOUTS_UTC:
        try:
            t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            manual.append({"time": iso(t), "title": "manual event"})
        except ValueError:
            log.warning("bad NEWS_BLACKOUTS_UTC entry: %s", s)

    if not AUTO_NEWS:
        return manual

    cache = state.get("news") or {}
    fetched = cache.get("fetched")
    if fetched and (now - parse_iso(fetched)).total_seconds() < NEWS_REFRESH_HOURS * 3600:
        return cache.get("events", []) + manual

    try:
        resp = SESSION.get(NEWS_URL, timeout=15)
        resp.raise_for_status()
        events = []
        for row in resp.json():
            if row.get("impact") != "High" or row.get("country") not in NEWS_COUNTRIES:
                continue
            t = parse_iso(row["date"]).astimezone(UTC)
            events.append({"time": iso(t), "title": row.get("title", "")})
        state["news"] = {"fetched": iso(now), "events": events}
        log.info("news calendar refreshed: %d high-impact events", len(events))
    except Exception as exc:  # noqa: BLE001
        log.warning("news calendar unavailable: %s", exc)
        retry_after = now - dt.timedelta(hours=NEWS_REFRESH_HOURS) + dt.timedelta(minutes=30)
        state["news"] = {"fetched": iso(retry_after), "events": cache.get("events", [])}
    return state["news"]["events"] + manual


def news_block(now: dt.datetime, events: List[dict]) -> Optional[dict]:
    for ev in events:
        t = parse_iso(ev["time"])
        if -NEWS_AFTER_MIN * 60 <= (t - now).total_seconds() <= NEWS_BEFORE_MIN * 60:
            return ev
    return None


# ============================================================
# TELEGRAM
# ============================================================

def tg_send(message: str) -> bool:
    require_credentials()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    def call():
        r = SESSION.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096],
                                    "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            raise RetryableError("telegram rate limit", wait=wait + 1)
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram rejected message: {r.text}")
        return True

    return with_retry(call, label="telegram")


def tg_photo(png: bytes, caption: str) -> bool:
    require_credentials()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    def call():
        r = SESSION.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000],
                                    "parse_mode": "HTML"},
                         files={"photo": ("chart.png", png, "image/png")}, timeout=40)
        r.raise_for_status()
        return True

    return with_retry(call, label="telegram photo")


# ============================================================
# CHART
# ============================================================

def build_chart(sym: Symbol, plan: dict, data15: List[dict]) -> Optional[bytes]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO
    except Exception:  # noqa: BLE001
        return None

    n = min(CHART_BARS, len(data15))
    view = data15[-n:]
    closes = [c["close"] for c in data15]
    e9 = ema(closes, EMA_FAST)[-n:]
    e15 = ema(closes, EMA_SLOW)[-n:]

    bg, fg, grid = "#0e1117", "#d0d4dc", "#232834"
    fig, ax = plt.subplots(figsize=(9, 5.6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    for i, c in enumerate(view):
        col = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1)
        ax.bar(i, max(candle_body(c), 1e-9), bottom=min(c["open"], c["close"]), width=0.7, color=col)

    xs = list(range(n))
    ax.plot([x for x, v in zip(xs, e9) if v], [v for v in e9 if v], color="#f5c542", linewidth=1, label="EMA9")
    ax.plot([x for x, v in zip(xs, e15) if v], [v for v in e15 if v], color="#4aa3ff", linewidth=1, label="EMA15")

    right = n + 12
    lo = min(min(c["low"] for c in view), plan["sl"])
    hi = max(max(c["high"] for c in view), plan["tp"])
    pad = (hi - lo) * 0.04
    ax.set_ylim(lo - pad, hi + pad)

    for z, label, color in ((plan.get("ob"), "OB", "#ab47bc"), (plan.get("fvg"), "FVG", "#29b6f6")):
        if z and lo - pad <= z["high"] and z["low"] <= hi + pad:
            ax.fill_between([0, right], z["low"], z["high"], color=color, alpha=0.15)
            ax.text(1, z["high"], label, color=color, fontsize=7, va="bottom")

    for k in plan.get("key_levels") or []:
        if k["kind"] in ("high", "low") and lo <= k["price"] <= hi:
            ax.axhline(k["price"], color="#8a93a6", linestyle=":", linewidth=0.7)
            ax.text(right, k["price"], k["name"], color="#8a93a6", fontsize=6.5, va="center", ha="right")

    if plan.get("sweep"):
        sp = plan["sweep"]["price"]
        ax.axhline(sp, color="#ffb74d", linestyle="--", linewidth=0.8)
        ax.text(2, sp, f"sweep {plan['sweep']['level']}", color="#ffb74d", fontsize=7, va="bottom")

    d = sym.digits
    for price, label, color, ls in (
        (plan["entry"], f"ENTRY {plan['entry']:.{d}f}", "#ffffff", "--"),
        (plan["sl"], f"SL {plan['sl']:.{d}f}", "#ef5350", "-"),
        (plan["tp1"], f"TP1 {plan['tp1']:.{d}f}", "#9ccc65", "-."),
        (plan["tp"], f"TP {plan['tp']:.{d}f}", "#26a69a", "-"),
    ):
        ax.axhline(price, color=color, linestyle=ls, linewidth=1)
        ax.text(right, price, label, color=color, fontsize=7.5, va="bottom", ha="right")

    ticks = list(range(0, n, max(1, n // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([view[i]["time"][5:16] for i in ticks], rotation=0, fontsize=7, color=fg)
    ax.tick_params(axis="y", colors=fg, labelsize=8)
    ax.set_xlim(-1, right)
    ax.grid(color=grid, linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.set_title(f"{sym.name} 15M - {plan['side']} ({plan['mode']}) - score {plan['score']}/{MAX_SCORE}",
                 color=fg, fontsize=10)
    ax.legend(loc="upper left", fontsize=7, facecolor=bg, labelcolor=fg, edgecolor=grid)

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=bg, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ============================================================
# MESSAGES
# ============================================================

def fmt(p: float, d: int) -> str:
    return f"{p:.{d}f}"


def level_text(level: Optional[dict], d: int) -> str:
    return "-" if not level else f"{level['price']:.{d}f} ({level['touches']}x)"


def build_signal_message(sym: Symbol, plan: dict, result: dict, pos: Optional[dict], now: dt.datetime) -> str:
    d = sym.digits
    icon = "🟢" if plan["side"] == "BUY" else "🔴"
    sweep = plan.get("sweep")
    fvg, ob = plan.get("fvg"), plan.get("ob")
    sr = plan.get("sr") or {}
    support, resistance = sr.get("support", []), sr.get("resistance", [])

    sweep_text = (f"{sweep['side']} {sweep['level']} @ {fmt(sweep['price'], d)} ({sweep['age']} candle ago)"
                  if sweep else "None")
    reasons = "\n".join(f"✓ {esc(x)}" for x in plan["reasons"][:14])
    conflicts = "\n".join(f"• {esc(x)}" for x in plan["conflicts"]) or "None"

    if plan["mode"] == "LIMIT":
        z = plan["zone"]
        entry_line = (f"📌 <b>LIMIT {plan['side']} @ {fmt(plan['entry'], d)}</b> ({z['label']} midpoint)\n"
                      f"Expires in {LIMIT_EXPIRE_MIN // 60}h. Cancelled if price hits TP1 first.")
    else:
        entry_line = f"⚡ <b>MARKET {plan['side']} @ {fmt(plan['entry'], d)}</b>"

    if pos:
        lot_line = (f"Lot size: <b>{pos['lots']:.2f}</b> (risk ${pos['risk_cash']:.2f} = {RISK_PERCENT:g}% "
                    f"of ${ACCOUNT_BALANCE:,.0f})" + (f"\n⚠️ {pos['warn']}" if pos["warn"] else ""))
    else:
        lot_line = "Lot size: set ACCOUNT_BALANCE + RISK_PERCENT to see it"

    return (
        f"{icon} <b>{sym.name} - {plan['side']} SETUP</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧭 Bias: <b>{result['bias']}</b> | 🕒 {esc(session_name(now))}\n"
        f"⭐ Confluence: <b>{plan['score']}/{MAX_SCORE}</b> "
        f"(ctx {plan['context']}/6 · trig {plan['trigger']}/6 · conf {plan['confirm']}/2)\n\n"
        f"<b>ICT / SMC</b>\n"
        f"Sweep: <b>{esc(sweep_text)}</b>\n"
        f"Order Block: <b>{ob['side'] if ob else 'None'}</b> | FVG: <b>{fvg['side'] if fvg else 'None'}</b>\n"
        f"P/D: <b>{plan['pd']['zone']}</b>\n"
        f"15M BOS: <b>{plan['m15_structure']['bos'] or 'None'}</b> | "
        f"CHoCH: <b>{plan['m15_structure']['choch'] or 'None'}</b>\n\n"
        f"<b>Confirmation</b>\n{reasons}\n\n"
        f"<b>TRADE PLAN</b>\n{entry_line}\n"
        f"SL: <b>{fmt(plan['sl'], d)}</b>\n"
        f"TP1: <b>{fmt(plan['tp1'], d)}</b> (1R: close {PARTIAL_PCT * 100:.0f}% + SL to break-even)\n"
        f"TP: <b>{fmt(plan['tp'], d)}</b> ({plan['rr']:.2f}R - {esc(plan['tp_label'])})\n"
        f"Risk: {fmt(plan['risk'], d)} ({plan['risk_atr']:.2f} x 15M ATR)\n"
        f"{lot_line}\n\n"
        f"<b>30M S/R</b>\n"
        f"Support: {level_text(support[0] if support else None, d)}\n"
        f"Resistance: {level_text(resistance[0] if resistance else None, d)}\n\n"
        f"<b>Conflicts</b>\n{conflicts}\n\n"
        f"⏰ Closed 15M candle: {plan['time']} UTC\n"
        f"⚠️ Rule-based market-reading alert, not a guarantee of price direction."
    )


def build_event_message(trade: dict, ev: dict) -> str:
    d, name, side = trade["digits"], trade["symbol"], trade["side"]
    t = ev["type"]
    head = f"<b>{name} {side}</b>"
    if t == "FILLED":
        return f"📥 {head} limit FILLED @ {fmt(trade['entry'], d)}\nSL {fmt(trade['sl'], d)} | TP1 {fmt(trade['tp1'], d)} | TP {fmt(trade['tp'], d)}"
    if t == "TP1":
        return (f"🎯 {head} TP1 hit @ {fmt(ev['price'], d)}\n"
                f"Close ~{trade['partial'] * 100:.0f}% and move SL to break-even ({fmt(trade['entry'], d)}).")
    if t == "TP":
        return f"✅ {head} TP hit @ {fmt(ev['price'], d)}\nResult: <b>{trade['r']:+.2f}R</b>"
    if t == "SL":
        return f"❌ {head} stop loss hit @ {fmt(ev['price'], d)}\nResult: <b>{trade['r']:+.2f}R</b>"
    if t == "BE":
        return f"➖ {head} stopped at break-even after TP1\nResult: <b>{trade['r']:+.2f}R</b>"
    if t == "TIMEOUT":
        return f"⏱ {head} closed by timeout @ {fmt(ev['price'], d)}\nResult: <b>{trade['r']:+.2f}R</b>"
    if t == "CANCELLED":
        why = {"EXPIRED": "limit order not filled in time",
               "MISSED": "price ran to TP1 without retesting the entry"}.get(ev["reason"], ev["reason"])
        return f"🚫 {head} setup CANCELLED - {why}. No trade."
    return f"{head} {t}"


def build_no_trade_message(sym: Symbol, result: dict, price: float, now: dt.datetime,
                           reason: Optional[str] = None) -> str:
    d = sym.digits
    sweep, fvg, ob, pd = result.get("sweep"), result.get("fvg"), result.get("ob"), result.get("pd")
    m15 = result.get("m15") or {}
    why = reason or result.get("reason") or "confluence threshold not reached"
    return (
        f"🟡 <b>{sym.name} - MARKET READER</b>\n━━━━━━━━━━━━━━━━\n"
        f"Price: <b>{fmt(price, d)}</b> | {esc(session_name(now))}\n"
        f"Bias: <b>{result.get('bias', 'UNKNOWN')}</b>\n"
        f"15M BOS: {m15.get('bos') or 'None'} | CHoCH: {m15.get('choch') or 'None'}\n"
        f"Sweep: {esc(sweep['level']) if sweep else 'None'} | OB: {ob['side'] if ob else 'None'} | "
        f"FVG: {fvg['side'] if fvg else 'None'}\nP/D: {pd['zone'] if pd else 'UNKNOWN'}\n\n"
        f"<b>ACTION: NO TRADE</b> - {esc(why)}\n(Periodic status - bot is alive.)"
    )


# ============================================================
# STATE / TRADE LOG / STATS / REPORTS
# ============================================================

def load_state() -> dict:
    state: dict = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    state.setdefault("trades", [])
    state.setdefault("paused", False)
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


LOG_FIELDS = ["id", "symbol", "side", "mode", "created", "fill_time", "closed", "entry", "sl", "tp1", "tp",
              "rr", "score", "result", "reason", "r", "lots"]


def log_trade(trade: dict, path: Optional[Path] = None) -> None:
    path = path or TRADE_LOG_FILE
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(trade)


def read_log(path: Optional[Path] = None) -> List[dict]:
    path = path or TRADE_LOG_FILE
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        try:
            r["r"] = float(r.get("r") or 0)
        except ValueError:
            r["r"] = 0.0
    return rows


def summarize_r(rs: List[float]) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "be": 0, "winrate": 0.0, "total": 0.0, "avg": 0.0,
                "pf": 0.0, "max_dd": 0.0, "max_losing_streak": 0}
    wins = sum(1 for r in rs if r > 0.05)
    losses = sum(1 for r in rs if r < -0.05)
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    peak = eq = dd = 0.0
    streak = best_streak = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if r < -0.05 else 0
        best_streak = max(best_streak, streak)
    return {"n": n, "wins": wins, "losses": losses, "be": n - wins - losses,
            "winrate": wins / n * 100, "total": sum(rs), "avg": sum(rs) / n,
            "pf": (gw / gl) if gl else float("inf"), "max_dd": dd, "max_losing_streak": best_streak}


def stats_text(title: str, rows: List[dict]) -> str:
    filled = [r for r in rows if r.get("result") in FILLED_RESULTS]
    cancelled = sum(1 for r in rows if r.get("result") == "CANCELLED")
    s = summarize_r([r["r"] for r in filled])
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    lines = [f"📊 <b>{esc(title)}</b>",
             f"Trades: {s['n']} (W {s['wins']} / L {s['losses']} / BE {s['be']}) | Cancelled: {cancelled}",
             f"Win rate: {s['winrate']:.0f}% | Total: <b>{s['total']:+.2f}R</b> | Avg: {s['avg']:+.2f}R",
             f"Profit factor: {pf} | Max DD: {s['max_dd']:.2f}R | Worst streak: {s['max_losing_streak']}"]
    for sym in sorted({r["symbol"] for r in filled}):
        ss = summarize_r([r["r"] for r in filled if r["symbol"] == sym])
        lines.append(f"• {sym}: {ss['n']} trades, {ss['total']:+.2f}R, WR {ss['winrate']:.0f}%")
    return "\n".join(lines)


def maybe_send_reports(state: dict, now: dt.datetime) -> None:
    tz = ZoneInfo(REPORT_TZ)
    local = now.astimezone(tz)
    if local.hour < REPORT_HOUR:
        return
    rows = read_log()

    def closed_local(r):
        try:
            return parse_iso(r["closed"]).astimezone(tz)
        except (ValueError, KeyError, TypeError):
            return None

    day_key = local.strftime("%Y-%m-%d")
    if state.get("report_daily") != day_key:
        today = [r for r in rows if (closed_local(r) and closed_local(r).strftime("%Y-%m-%d") == day_key)]
        if today:
            tg_send(stats_text(f"Daily report {day_key}", today))
        state["report_daily"] = day_key

    if local.weekday() == WEEKLY_REPORT_WEEKDAY:
        wk = local.strftime("%G-W%V")
        if state.get("report_weekly") != wk:
            since = local - dt.timedelta(days=7)
            week_rows = [r for r in rows if (closed_local(r) and closed_local(r) >= since)]
            tg_send(stats_text(f"Weekly report {wk}", week_rows))
            state["report_weekly"] = wk


# ============================================================
# TELEGRAM COMMANDS  (/status /trades /stats /report /pause /resume /help)
# ============================================================

def handle_command(cmd: str, state: dict, now: dt.datetime) -> None:
    if cmd in ("/start", "/help"):
        tg_send("🤖 <b>Commands</b>\n/status - bot status\n/trades - active trades\n/stats - performance\n"
                "/report - today's report\n/pause - stop new signals\n/resume - resume signals\n"
                "<i>Note: the bot checks commands on each scheduled run.</i>")
    elif cmd == "/pause":
        state["paused"] = True
        tg_send("⏸ New signals paused. Open trades are still tracked. /resume to continue.")
    elif cmd == "/resume":
        state["paused"] = False
        tg_send("▶️ Signals resumed.")
    elif cmd == "/trades":
        if not state["trades"]:
            tg_send("No active trades.")
        for t in state["trades"]:
            d = t["digits"]
            tg_send(f"<b>{t['symbol']} {t['side']}</b> [{t['status']}{' TP1✓' if t['tp1_hit'] else ''}]\n"
                    f"Entry {fmt(t['entry'], d)} | SL {fmt(t['sl_cur'], d)} | TP {fmt(t['tp'], d)}\n"
                    f"Last price {fmt(t['last_price'], d)}")
    elif cmd == "/stats":
        rows = read_log()
        tg_send(stats_text("All-time", rows))
        since = now - dt.timedelta(days=7)
        recent = []
        for r in rows:
            try:
                if parse_iso(r["closed"]) >= since:
                    recent.append(r)
            except (ValueError, KeyError, TypeError):
                pass
        tg_send(stats_text("Last 7 days", recent))
    elif cmd == "/report":
        tz = ZoneInfo(REPORT_TZ)
        key = now.astimezone(tz).strftime("%Y-%m-%d")
        today = []
        for r in read_log():
            try:
                if parse_iso(r["closed"]).astimezone(tz).strftime("%Y-%m-%d") == key:
                    today.append(r)
            except (ValueError, KeyError, TypeError):
                pass
        tg_send(stats_text(f"Today {key}", today))
    elif cmd == "/status":
        usage = api_usage()
        counters = state.get("counters", {})
        lines = ["🤖 <b>Bot status</b>",
                 f"Mode: 24h | Paused: {'YES' if state.get('paused') else 'no'}",
                 f"Time: {now:%Y-%m-%d %H:%M} UTC | {esc(session_name(now))}",
                 f"Active trades: {len(state['trades'])}",
                 f"API calls today: {usage['count']}/{DAILY_API_LIMIT}"]
        for sym in SYMBOLS:
            c = counters.get(sym.name, {})
            lines.append(f"• {sym.name}: market {'open' if market_open(sym, now) else 'closed'}, "
                         f"signals today {c.get('signals', 0)}/{MAX_SIGNALS_PER_DAY}, R today {c.get('r', 0):+.2f}")
        tg_send("\n".join(lines))


def process_commands(state: dict, now: dt.datetime) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    params: Dict[str, Any] = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    resp = SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        return
    for upd in data["result"]:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        sent = dt.datetime.fromtimestamp(msg.get("date", 0), UTC)
        if (now - sent).total_seconds() > COMMAND_MAX_AGE_MIN * 60:
            continue
        handle_command(text.split()[0].split("@")[0].lower(), state, now)


# ============================================================
# LIVE RUN
# ============================================================

def get_counters(state: dict, now: dt.datetime) -> dict:
    day = to_ny(now).strftime("%Y-%m-%d")
    c = state.get("counters")
    if not c or c.get("date") != day:
        c = {"date": day}
        state["counters"] = c
    return c


def signal_gate(state: dict, sym: Symbol, now: dt.datetime, news: List[dict]) -> Optional[str]:
    """Reason why NO new signal may be generated right now (None = allowed)."""
    if state.get("paused"):
        return "paused by /pause"
    if sym.news_filter:
        ev = news_block(now, news)
        if ev:
            return f"news blackout: {ev['title']}"
    c = get_counters(state, now).setdefault(sym.name, {"signals": 0, "r": 0.0})
    if c["signals"] >= MAX_SIGNALS_PER_DAY:
        return "daily signal limit reached"
    if c["r"] <= -DAILY_MAX_LOSS_R:
        return f"daily loss limit ({c['r']:+.1f}R)"
    if sum(1 for t in state["trades"] if t["symbol"] == sym.name) >= MAX_ACTIVE_PER_SYMBOL:
        return "active trade"
    last = (state.get("last_signal") or {}).get(sym.name)
    if last and (now - parse_iso(last)).total_seconds() < COOLDOWN_MIN * 60:
        return "cooldown"
    return None


def maybe_send_status(state: dict, name: str, text: str, ts: dt.datetime, every_hours: Optional[int] = None) -> None:
    if not SEND_STATUS_UPDATES:
        return
    n = every_hours or STATUS_EVERY_HOURS
    bucket = f"{ts.strftime('%Y-%m-%d')}-{ts.hour // n}"
    key = f"status:{name}"
    if state.get(key) == bucket:
        return
    tg_send(text)
    state[key] = bucket
    save_state(state)
    log.info("[%s] status message sent", name)


def finalize_trade(state: dict, trade: dict, now: dt.datetime) -> None:
    log_trade(trade)
    state["trades"] = [t for t in state["trades"] if t["id"] != trade["id"]]
    if trade["result"] in FILLED_RESULTS:
        c = get_counters(state, now).setdefault(trade["symbol"], {"signals": 0, "r": 0.0})
        c["r"] = round(c["r"] + trade["r"], 3)


def update_trades(state: dict, sym: Symbol, d5: List[dict], d15: List[dict], now: dt.datetime) -> None:
    for trade in [t for t in state["trades"] if t["symbol"] == sym.name]:
        cur = parse_iso(trade["cursor"])
        candles, minutes = (d5, 5) if d5 and d5[0]["dt"] <= cur else (d15, 15)
        events = advance_trade(trade, candles, minutes, now)
        for ev in events:
            log.info("[%s] trade %s -> %s", sym.name, trade["id"], ev["type"])
            try:
                tg_send(build_event_message(trade, ev))
            except Exception as exc:  # noqa: BLE001
                log.warning("event message failed: %s", exc)
        if trade["status"] in CLOSED_STATES:
            finalize_trade(state, trade, now)
        save_state(state)


def alert_error(state: dict, name: str, exc: Exception, now: dt.datetime) -> None:
    key = f"error:{name}"
    last = state.get(key)
    if last and (now - parse_iso(last)).total_seconds() < ERROR_ALERT_COOLDOWN_MIN * 60:
        return
    try:
        tg_send(f"🛠 <b>{esc(name)} - bot error</b>\n<code>{esc(str(exc)[:400])}</code>")
        state[key] = iso(now)
    except Exception as exc2:  # noqa: BLE001
        log.warning("could not send error alert: %s", exc2)


def analyze_data(sym: Symbol, d4h: List[dict], d30: List[dict], d15: List[dict], d5: List[dict],
                 now: dt.datetime) -> dict:
    if min(len(d4h), len(d30), len(d15), len(d5)) < 60:
        raise NotEnoughData("not enough closed candles")
    bias_data = get_bias(d4h, d30)
    kl = key_levels(d30, now)
    result = score_setup(bias_data, d30, d15, d5, kl)
    result["key_levels"] = kl
    result["plan"] = None
    result["reason"] = None

    signal = result.get("signal")
    if signal:
        ok, why = volatility_ok(d15)
        if not ok:
            result["reason"] = f"volatility filter: {why}"
            return result
        plan, why = make_trade_plan(signal, d15)
        if plan:
            result["plan"] = plan
        else:
            result["reason"] = f"setup rejected: {why}"
    elif result.get("ambiguous"):
        result["reason"] = "BUY and SELL scores tied"
    return result


def process_symbol(sym: Symbol, state: dict, now: dt.datetime, news: List[dict]) -> None:
    name = sym.name

    if not market_open(sym, now):
        log.info("[%s] market closed", name)
        maybe_send_status(state, name, f"⚪ <b>{name}</b>\nMarket closed - scanning paused. (Bot is alive.)",
                          now, every_hours=24)
        return

    active = [t for t in state["trades"] if t["symbol"] == name]
    gate = signal_gate(state, sym, now, news)
    if gate and not active:
        log.info("[%s] gated: %s", name, gate)
        if gate not in ("cooldown", "active trade"):
            maybe_send_status(state, name, f"⚪ <b>{name}</b>\nNew signals paused: {esc(gate)}. (Bot is alive.)", now)
        return

    try:
        data = fetch_all(sym)
    except DataStale as exc:
        log.info("[%s] %s", name, exc)
        maybe_send_status(state, name, f"⚪ <b>{name}</b>\nNo fresh price data ({esc(exc)}).", now, every_hours=12)
        return

    if active:
        update_trades(state, sym, data["d5_all"], data["d15"], now)

    gate = signal_gate(state, sym, now, news)
    if gate:
        log.info("[%s] gated after tracking: %s", name, gate)
        return

    result = analyze_data(sym, data["d4h"], data["d30"], data["d15"], data["d5"], data["cutoff"])
    plan = result.get("plan")
    price = data["d15"][-1]["close"]

    if not plan:
        log.info("[%s] NO TRADE | bias=%s | price=%s | %s", name, result.get("bias"), price, result.get("reason"))
        maybe_send_status(state, name, build_no_trade_message(sym, result, price, now), now)
        return

    delay = (now - plan["close_dt"]).total_seconds() / 60
    if delay < 0 or delay > STALE_SIGNAL_MIN:
        log.info("[%s] stale signal skipped: %.1fm", name, delay)
        return

    key = f"{name}:{plan['time']}:{plan['side']}"
    if state.get("last_key", {}).get(name) == key:
        log.info("[%s] duplicate skipped", name)
        return

    pos = position_size(sym, plan["risk"])
    trade = new_trade(plan, sym, now, pos)

    # was the setup already invalidated during the delay?
    probe = dict(trade)
    advance_trade(probe, data["d5_all"], 5, now)
    if probe["status"] in CLOSED_STATES:
        log.info("[%s] signal already invalidated (%s) - not sent", name, probe.get("result"))
        state.setdefault("last_key", {})[name] = key
        save_state(state)
        return

    chart = build_chart(sym, plan, data["d15"]) if SEND_CHART else None
    if chart:
        try:
            tg_photo(chart, f"{'🟢' if plan['side'] == 'BUY' else '🔴'} <b>{name} {plan['side']}</b> "
                            f"{plan['mode']} | {plan['score']}/{MAX_SCORE} | RR {plan['rr']:.2f}")
        except Exception as exc:  # noqa: BLE001
            log.warning("chart send failed: %s", exc)
    tg_send(build_signal_message(sym, plan, result, pos, now))

    state["trades"].append(trade)
    state.setdefault("last_key", {})[name] = key
    state.setdefault("last_signal", {})[name] = iso(now)
    get_counters(state, now).setdefault(name, {"signals": 0, "r": 0.0})["signals"] += 1
    save_state(state)
    log.info("[%s] SENT %s %s score=%d/%d rr=%.2f", name, plan["mode"], plan["side"], plan["score"], MAX_SCORE, plan["rr"])


def run_once() -> None:
    state = load_state()
    now = now_utc()
    try:
        try:
            process_commands(state, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("command processing failed: %s", exc)

        news = refresh_news(state, now)

        for sym in SYMBOLS:
            try:
                process_symbol(sym, state, now, news)
            except QuotaExceeded as exc:
                log.warning("%s", exc)
                alert_error(state, "API quota", exc, now)
                break
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] ERROR", sym.name)
                alert_error(state, sym.name, exc, now)

        try:
            maybe_send_reports(state, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("report failed: %s", exc)
    finally:
        save_state(state)
        flush_cache()


# ============================================================
# BACKTEST  (same engine as live: scoring, plan, trade lifecycle)
# ============================================================

def backtest_symbol(sym: Symbol, d4: List[dict], d30: List[dict], d15: List[dict], d5: List[dict],
                    days: Optional[int] = None) -> List[dict]:
    m4, m30, m15, m5 = (interval_minutes(x) for x in (BIAS_INTERVAL, STRUCTURE_INTERVAL, SETUP_INTERVAL, ENTRY_INTERVAL))
    ends4 = [c["dt"] + dt.timedelta(minutes=m4) for c in d4]
    ends30 = [c["dt"] + dt.timedelta(minutes=m30) for c in d30]
    starts5 = [c["dt"] for c in d5]
    ends5 = [c["dt"] + dt.timedelta(minutes=m5) for c in d5]

    first = d5[0]["dt"] + dt.timedelta(minutes=m5 * 80)
    last_cut = d15[-1]["dt"] + dt.timedelta(minutes=m15)
    if days:
        first = max(first, last_cut - dt.timedelta(days=days))

    trades: List[dict] = []
    active: Optional[dict] = None
    counters: Dict[str, dict] = {}
    last_sig: Optional[dt.datetime] = None

    for i in range(80, len(d15)):
        cutoff = d15[i]["dt"] + dt.timedelta(minutes=m15)
        if cutoff < first:
            continue

        if active:
            lo = bisect.bisect_left(starts5, parse_iso(active["cursor"]))
            hi = bisect.bisect_right(ends5, cutoff)
            advance_trade(active, d5[lo:hi], m5, cutoff)
            if active["status"] in CLOSED_STATES:
                if active["result"] in FILLED_RESULTS:
                    c = counters.setdefault(to_ny(cutoff).strftime("%Y-%m-%d"), {"signals": 0, "r": 0.0})
                    c["r"] += active["r"]
                trades.append(active)
                active = None

        if active is not None or not market_open(sym, cutoff):
            continue
        day = counters.setdefault(to_ny(cutoff).strftime("%Y-%m-%d"), {"signals": 0, "r": 0.0})
        if day["signals"] >= MAX_SIGNALS_PER_DAY or day["r"] <= -DAILY_MAX_LOSS_R:
            continue
        if last_sig and (cutoff - last_sig).total_seconds() < COOLDOWN_MIN * 60:
            continue

        e4 = bisect.bisect_right(ends4, cutoff)
        e30 = bisect.bisect_right(ends30, cutoff)
        e5 = bisect.bisect_right(ends5, cutoff)
        try:
            result = analyze_data(sym,
                                  d4[max(0, e4 - BIAS_BARS):e4], d30[max(0, e30 - STRUCTURE_BARS):e30],
                                  d15[max(0, i + 1 - SETUP_BARS):i + 1], d5[max(0, e5 - ENTRY_BARS):e5], cutoff)
        except NotEnoughData:
            continue
        plan = result.get("plan")
        if not plan:
            continue
        active = new_trade(plan, sym, cutoff)
        day["signals"] += 1
        last_sig = cutoff

    return trades


def backtest_report(name: str, trades: List[dict], folds: int = 4) -> str:
    filled = [t for t in trades if t["result"] in FILLED_RESULTS]
    cancelled = sum(1 for t in trades if t["result"] == "CANCELLED")
    filled.sort(key=lambda t: t["closed"] or "")
    s = summarize_r([t["r"] for t in filled])
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    lines = [f"BACKTEST {name}",
             f"Trades: {s['n']} (W {s['wins']} / L {s['losses']} / BE {s['be']}), cancelled/unfilled: {cancelled}",
             f"Win rate: {s['winrate']:.1f}% | Expectancy: {s['avg']:+.3f}R | Total: {s['total']:+.2f}R",
             f"Profit factor: {pf} | Max drawdown: {s['max_dd']:.2f}R | Worst losing streak: {s['max_losing_streak']}"]
    if s["n"] >= folds * 3:
        lines.append("Walk-forward (chronological folds - is the edge stable?):")
        size = s["n"] // folds
        for k in range(folds):
            chunk = filled[k * size:(k + 1) * size if k < folds - 1 else s["n"]]
            cs = summarize_r([t["r"] for t in chunk])
            lines.append(f"  fold {k + 1}: {cs['n']:>3} trades | WR {cs['winrate']:>4.0f}% | "
                         f"exp {cs['avg']:+.2f}R | total {cs['total']:+.2f}R")
    else:
        lines.append("Too few trades for walk-forward folds - use more history (--days) before trusting numbers.")
    return "\n".join(lines)


def run_backtest_cli(args: argparse.Namespace) -> None:
    require_credentials(telegram=args.send)
    syms = [s for s in SYMBOLS if not args.symbol or s.name == args.symbol.upper()]
    if not syms:
        raise SystemExit(f"unknown symbol {args.symbol}")
    for sym in syms:
        log.info("[%s] downloading history (4 API calls)...", sym.name)
        d4 = get_candles(sym.td_symbol, BIAS_INTERVAL, 1500, live=False)
        d30 = get_candles(sym.td_symbol, STRUCTURE_INTERVAL, 5000, live=False)
        d15 = get_candles(sym.td_symbol, SETUP_INTERVAL, 5000, live=False)
        d5 = get_candles(sym.td_symbol, ENTRY_INTERVAL, 5000, live=False)
        log.info("[%s] replaying %d x 15M candles (5M history limits the test window)...", sym.name, len(d15))
        trades = backtest_symbol(sym, d4, d30, d15, d5, days=args.days)
        out = Path(f"backtest_{sym.name}.csv")
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)
        text = backtest_report(sym.name, trades)
        print("\n" + text + f"\n(trades saved to {out})\n")
        if args.send:
            tg_send(f"<pre>{esc(text)}</pre>")
    flush_cache()


# ============================================================
# MAIN
# ============================================================

def send_test_message() -> None:
    tg_send("✅ <b>ICT SMC Market Reader v3</b>\nTelegram connection works. Mode: 24h.\n"
            f"Time: {now_utc():%Y-%m-%d %H:%M} UTC")
    log.info("Test message sent")


def main() -> None:
    global MIN_SCORE, RISK_REWARD
    load_overrides()

    parser = argparse.ArgumentParser(description="ICT + SMC market reader v3")
    parser.add_argument("--test", action="store_true", help="send a Telegram test message")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--backtest", action="store_true", help="replay history with the live engine")
    parser.add_argument("--symbol", help="backtest one symbol, e.g. XAUUSD")
    parser.add_argument("--days", type=int, help="limit backtest window")
    parser.add_argument("--send", action="store_true", help="send backtest summary to Telegram")
    parser.add_argument("--min-score", type=int, help="override MIN_SCORE")
    parser.add_argument("--rr", type=float, help="override fixed RISK_REWARD fallback")
    args = parser.parse_args()

    if args.min_score:
        MIN_SCORE = args.min_score
    if args.rr:
        RISK_REWARD = args.rr

    if args.backtest:
        run_backtest_cli(args)
        return

    require_credentials()
    if args.test or os.environ.get("TEST_TELEGRAM") == "1":
        send_test_message()
        return

    if not (args.loop or RUN_FOREVER):
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001
            log.exception("[MAIN] ERROR: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
