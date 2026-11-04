#!/usr/bin/env python3
"""
SMART SCALPER v1.0  -  Supply/Demand + EMA 9/15 scalping signal bot
=====================================================================
Structure (your rules)
  * 15M  : trend  = EMA 9 / EMA 15 (order + slope + gap, flat = no trade)
  * 5M   : structure (HH/HL, LH/LL, BOS/CHoCH) + supply/demand zones
           (base candle before a strong impulse; 15M zones rank higher)
  * 1M   : entry. Only on a RETEST of a fresh zone, after a 1M confirmation
           candle (pinbar / engulfing / 1M structure shift).
  * SL   : swing low (BUY) / swing high (SELL)  (or the zone edge if lower/higher)
  * TP   : next swing high (BUY) / swing low (SELL), min RR enforced
  * No Asian session (default sessions 07-20 UTC = 13:00-02:00 Bangladesh)

Filters
  min RR, session, high-impact USD news blackout, volatility (dead/spike),
  EMA flat filter, fresh-zone filter, opposing-zone conflict + room-to-target,
  spread cost in R, max chase, daily SL / R limit, cooldown after SL,
  max open trades, lot size from balance + risk %.

Memory + learning
  * every open trade is stored in the state file and followed on 1M candles
  * every closed trade is logged with features and automatic loss tags
    (FAST_SL, WEAK_ZONE, NEAR_NEWS, LOW_VOL, HIGH_VOL, COUNTER_5M, SL_HUNTED ...)
  * learning filter: a condition (session / zone grade / confirmation ...) is
    suppressed only after LEARN_MIN_TRADES samples with clearly negative avg R.
    Suppressed signals are still tracked as SHADOW trades, so the bot can see
    whether the filter was right and release it again when it was wrong.

Run
  python smart_scalper.py                 one scan
  LOOP_MINUTES=350 python smart_scalper.py   loop (scan every ~minute)
  python smart_scalper.py --test          Telegram test message
  python smart_scalper.py --report        send performance report now
  python smart_scalper.py --backtest data.csv [XAUUSD|BTCUSD]   offline replay

Secrets: TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Every setting can be overridden with an environment variable of the same name.
"""
import csv
import html
import json
import math
import os
import statistics
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

import requests

VERSION = "1.0"
UTC = dt.timezone.utc


def _num(name, default, cast=float):
    try:
        v = os.environ.get(name, "")
        return cast(v) if v != "" else cast(default)
    except (TypeError, ValueError):
        return cast(default)


def _flag(name, default="1"):
    v = os.environ.get(name, "")
    return (default if v == "" else v) == "1"


def _env(name, default):
    return os.environ.get(name) or default


# ------------------------------------------------------------------ symbols
SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD", "digits": 3, "prefix": "XAU", "weekend_closed": True,
     "spread": _num("XAU_SPREAD", 0.25), "value_per_lot": _num("XAU_VALUE_PER_LOT", 100.0),
     "source": "twelvedata"},
    {"name": "BTCUSD", "td_symbol": "BTC/USD", "digits": 2, "prefix": "BTC", "weekend_closed": False,
     "spread": _num("BTC_SPREAD", 6.5), "value_per_lot": _num("BTC_VALUE_PER_LOT", 1.0),
     "source": _env("BTC_SOURCE", "twelvedata"), "bn_symbol": "BTCUSDT"},
]
ENABLED = [x.strip().upper() for x in _env("SYMBOLS", "XAUUSD,BTCUSD").split(",") if x.strip()]

# ------------------------------------------------------------------ settings
EMA_FAST, EMA_SLOW, ATR_PERIOD = 9, 15, 14
PIVOT_LEFT, PIVOT_RIGHT = 3, 3
STRUCTURE_LOOKBACK = 80
BREAK_WINDOW = 12                       # BOS/CHoCH must be this recent (5M bars)
MIN_EMA_GAP_ATR = _num("MIN_EMA_GAP_ATR", 0.10)   # EMA9-EMA15 gap in 15M ATR; below = flat

# zones
ZONE_IMPULSE_ATR = _num("ZONE_IMPULSE_ATR", 1.2)
ZONE_IMPULSE_BODY = 0.60
ZONE_MAX_HEIGHT_ATR = _num("ZONE_MAX_HEIGHT_ATR", 1.6)
ZONE_LOOKBACK_5M = _num("ZONE_LOOKBACK_5M", 120, int)
ZONE_LOOKBACK_15M = _num("ZONE_LOOKBACK_15M", 80, int)
ZONE_MAX_TOUCHES = _num("ZONE_MAX_TOUCHES", 1, int)   # fresh-zone filter (0 = never touched)
ZONE_RETEST_BARS = 2                    # newest bars of the zone TF are the current retest, not history
ZONE_TOL_ATR = _num("ZONE_TOL_ATR", 0.15)
ZONE_MAX_KEEP = 14
ZONE_REUSE_MIN = _num("ZONE_REUSE_MIN", 90, int)      # one trade per zone per this many minutes

# 1M entry
RETEST_WINDOW_1M = 15
CATCHUP_CANDLES = _num("CATCHUP_CANDLES", 1, int)     # also check the previous N 1M candles (late scans)
MAX_CHASE_ATR = _num("MAX_CHASE_ATR", 0.25)

# SL / TP
SL_BUF_ATR = _num("SL_BUF_ATR", 0.10)
MIN_SL_ATR = _num("MIN_SL_ATR", 0.70)
MAX_SL_ATR = _num("MAX_SL_ATR", 3.0)
TP_BUF_ATR = 0.05
MIN_RR = _num("MIN_RR", 1.5)
MAX_RR = _num("MAX_RR", 4.0)
DEFAULT_RR = max(_num("DEFAULT_RR", 2.0), MIN_RR)
MAX_COST_R = _num("MAX_COST_R", 0.20)
BE_AT_R = _num("BE_AT_R", 1.0)          # move SL to entry after +1R (0 = off)
TRADE_MAX_AGE_MIN = _num("TRADE_MAX_AGE_MIN", 180, int)

# scoring
SCORE_MAX = 12
MIN_SCORE = _num("MIN_SCORE", 7, int)

# volatility
VOL_LOOKBACK = 288
VOL_MIN_RATIO = _num("VOL_MIN_RATIO", 0.50)
VOL_MAX_RATIO = _num("VOL_MAX_RATIO", 3.0)

# risk
DAILY_MAX_SL = _num("DAILY_MAX_SL", 3, int)
DAILY_LOSS_LIMIT_R = _num("DAILY_LOSS_LIMIT_R", 3.0)
COOLDOWN_AFTER_SL_MIN = _num("COOLDOWN_AFTER_SL_MIN", 20, int)
MAX_OPEN_PER_SYMBOL = _num("MAX_OPEN_PER_SYMBOL", 1, int)
MAX_OPEN_TOTAL = _num("MAX_OPEN_TOTAL", 2, int)
MAX_SIGNALS_PER_DAY = _num("MAX_SIGNALS_PER_DAY", 10, int)
ACCOUNT_BALANCE = _num("ACCOUNT_BALANCE", 0.0)
RISK_PCT = _num("RISK_PCT", 1.0)

# news
NEWS_AUTO = _flag("NEWS_AUTO")
NEWS_WINDOW_MIN = _num("NEWS_WINDOW_MIN", 30, int)
NEWS_CURRENCIES = tuple(x.strip().upper() for x in _env("NEWS_CURRENCIES", "USD").split(",") if x.strip())
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_REFRESH_HOURS = 6
NEWS_MANUAL = []                        # e.g. "2026-11-06 13:30"

# learning
LEARN_ON = _flag("LEARN_ON")
LEARN_MODE = _env("LEARN_MODE", "auto")           # auto = block bad conditions, advise = shadow only
LEARN_MIN_TRADES = _num("LEARN_MIN_TRADES", 20, int)
LEARN_BAD_AVG_R = _num("LEARN_BAD_AVG_R", 0.20)
LEARN_BAD_WINRATE = _num("LEARN_BAD_WINRATE", 0.45)
LEARN_WINDOW_DAYS = _num("LEARN_WINDOW_DAYS", 120, int)
STOPHUNT_WINDOW_MIN = _num("STOPHUNT_WINDOW_MIN", 45, int)
MAX_CLOSED_HISTORY = 5000

# data / API / loop
FETCH_BARS = _num("FETCH_BARS", 1500, int)
STALE_DATA_MIN = _num("STALE_DATA_MIN", 4, int)
TWELVE_DAILY_LIMIT = _num("TWELVE_DAILY_LIMIT", 800, int)
SCAN_INTERVAL_SEC = _num("SCAN_INTERVAL_SEC", 60, int)
LOOP_MINUTES = _num("LOOP_MINUTES", 0, int)
RATE_LIMIT_WAIT = 20
DAILY_SUMMARY_HOUR_UTC = _num("DAILY_SUMMARY_HOUR_UTC", 21, int)
WEEKLY_REPORT_HOUR_UTC = _num("WEEKLY_REPORT_HOUR_UTC", 18, int)
SEND_STATUS_UPDATES = _flag("SEND_STATUS_UPDATES", "0")
STATUS_EVERY_HOURS = max(1, _num("STATUS_EVERY_HOURS", 4, int))
SEND_WATCH = _flag("SEND_WATCH", "0")             # "zone retest, waiting for 1M confirmation" heads-up
GIT_PUSH_STATE = _flag("GIT_PUSH_STATE", "0")     # commit the state file after each trade event (GitHub Actions)

STATE_FILE = Path(_env("STATE_FILE", "smart_scalper_state.json"))
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"Smart-Scalper/{VERSION}"})
_API = {"calls": 0}
OFFLINE = False                                   # True in backtests: no network, no Telegram
_PACE = {"sec": SCAN_INTERVAL_SEC}


# ============================================================ small helpers
def now_utc():
    return dt.datetime.now(UTC)


def _iso(d):
    return d.isoformat()


def _from_iso(s):
    return dt.datetime.fromisoformat(s)


def require_credentials():
    missing = [n for n, v in (("TWELVE_DATA_API_KEY", TWELVE_DATA_API_KEY),
                              ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)) if not v]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def _parse_sessions(text):
    text = (text or "").strip().lower()
    if text in ("", "off", "none", "0", "all"):
        return None
    out = []
    try:
        for part in text.split(","):
            a, b = part.split("-")
            out.append((int(a), int(b)))
    except ValueError:
        return None
    return out or None


def in_session(ts, sessions):
    h = ts.hour + ts.minute / 60
    return any(a <= h < b for a, b in sessions)


def session_name(ts):
    h = ts.hour
    if h < 7:
        return "ASIA"
    if h < 12:
        return "LONDON"
    if h < 16:
        return "NY_OVERLAP"
    if h < 21:
        return "NY_LATE"
    return "LATE"


def market_closed_weekend(ts):
    wd = ts.weekday()
    return wd == 5 or (wd == 4 and ts.hour >= 21) or (wd == 6 and ts.hour < 22)


def sym_cfg(name):
    base = next(s for s in SYMBOLS if s["name"] == name)
    cfg = dict(base)
    cfg["sessions"] = _parse_sessions(_env(f"{base['prefix']}_SESSIONS_UTC", _env("SESSIONS_UTC", "7-20")))
    cfg["min_score"] = _num(f"{base['prefix']}_MIN_SCORE", MIN_SCORE, int)
    return cfg


def notify(text):
    if OFFLINE:
        return False
    try:
        return send_telegram(text)
    except Exception as exc:
        print(f"[TELEGRAM] error: {exc}")
        return False


def send_telegram(message):
    require_credentials()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    last = None
    for attempt in range(3):
        try:
            r = SESSION.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                        "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    wait = 3
                time.sleep(min(wait + 1, 30))
                last = RuntimeError("Telegram rate limit")
                continue
            r.raise_for_status()
            if not r.json().get("ok"):
                raise RuntimeError(f"Telegram rejected message: {r.text[:200]}")
            return True
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


# ============================================================ data
def _parse_td_rows(values):
    by_time = {}
    for row in values:
        try:
            ts = dt.datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            o, h, lo, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(x) for x in (o, h, lo, c)) or h < max(o, c, lo) or lo > min(o, c, h):
                continue
            by_time[ts] = _candle(ts, o, h, lo, c)
        except (KeyError, TypeError, ValueError):
            continue
    return [by_time[k] for k in sorted(by_time)]


def _candle(ts, o, h, lo, c):
    return {"time": ts.strftime("%Y-%m-%d %H:%M:%S"), "dt": ts, "open": o, "high": h, "low": lo, "close": c}


def fetch_td(symbol, size):
    require_credentials()
    data = None
    for attempt in range(2):
        _API["calls"] += 1
        r = SESSION.get("https://api.twelvedata.com/time_series",
                        params={"symbol": symbol, "interval": "1min", "outputsize": size,
                                "timezone": "UTC", "apikey": TWELVE_DATA_API_KEY}, timeout=20)
        if r.status_code == 429:
            if attempt == 0:
                time.sleep(RATE_LIMIT_WAIT)
                continue
            raise RuntimeError("Twelve Data HTTP 429")
        r.raise_for_status()
        data = r.json()
        if data.get("code") == 429 and attempt == 0:
            time.sleep(RATE_LIMIT_WAIT)
            continue
        break
    if not isinstance(data, dict) or "values" not in data:
        raise RuntimeError(f"Twelve Data error: {str(data)[:200]}")
    out = _parse_td_rows(data["values"])
    if not out:
        raise RuntimeError(f"No valid candles for {symbol}")
    return out


def fetch_binance(symbol, limit=1000):
    r = SESSION.get("https://data-api.binance.vision/api/v3/klines",
                    params={"symbol": symbol, "interval": "1m", "limit": limit}, timeout=15)
    r.raise_for_status()
    out = []
    for k in r.json():
        ts = dt.datetime.fromtimestamp(k[0] / 1000, UTC)
        out.append(_candle(ts, float(k[1]), float(k[2]), float(k[3]), float(k[4])))
    if not out:
        raise RuntimeError("Binance returned no candles")
    return out


def drop_unclosed(candles):
    cutoff = now_utc()
    return [c for c in candles if c["dt"] + dt.timedelta(minutes=1) <= cutoff]


def get_1m(sym):
    if sym.get("source") == "binance":
        try:
            return drop_unclosed(fetch_binance(sym["bn_symbol"]))
        except Exception as exc:
            print(f"[{sym['name']}] Binance failed, using Twelve Data: {exc}")
    return drop_unclosed(fetch_td(sym["td_symbol"], FETCH_BARS))


def resample(c1m, minutes):
    """Complete higher-timeframe candles from 1M data (tolerates 1-2 missing 1M bars)."""
    if not c1m:
        return []
    last_end = int((c1m[-1]["dt"] + dt.timedelta(minutes=1)).timestamp())
    groups = {}
    for c in c1m:
        e = int(c["dt"].timestamp())
        groups.setdefault(e - e % (minutes * 60), []).append(c)
    need = minutes - (1 if minutes <= 5 else 2)
    out = []
    for b in sorted(groups):
        g = groups[b]
        if b + minutes * 60 > last_end or len(g) < need:
            continue
        start = dt.datetime.fromtimestamp(b, UTC)
        out.append(_candle(start, g[0]["open"], max(x["high"] for x in g), min(x["low"] for x in g), g[-1]["close"]))
    return out


def load_csv(path):
    """CSV with a datetime column (UTC) + open/high/low/close. Names are matched loosely."""
    by_time = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ("\t" if sample.count("\t") > sample.count(",") else ",")
        rd = csv.DictReader(fh, delimiter=delim)
        cols = {k.strip().lower().lstrip("<").rstrip(">"): k for k in rd.fieldnames or []}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            raise RuntimeError(f"CSV column missing, need one of {names}; found {list(cols)}")
        ct = col("datetime", "time", "date", "timestamp")
        co, ch, cl, cc = col("open"), col("high"), col("low"), col("close")
        for row in rd:
            raw = row[ct].strip()
            ts = None
            if raw.replace(".", "", 1).isdigit():
                v = float(raw)
                ts = dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
            else:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
                            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%d.%m.%Y %H:%M"):
                    try:
                        ts = dt.datetime.strptime(raw, fmt).replace(tzinfo=UTC)
                        break
                    except ValueError:
                        continue
            if ts is None:
                continue
            try:
                o, h, lo, c = float(row[co]), float(row[ch]), float(row[cl]), float(row[cc])
            except ValueError:
                continue
            by_time[ts] = _candle(ts, o, h, lo, c)
    out = [by_time[k] for k in sorted(by_time)]
    if not out:
        raise RuntimeError("No candles read from CSV")
    return out


# ============================================================ indicators / structure
def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr(candles, period=14):
    out = [None] * len(candles)
    if len(candles) < period:
        return out
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            tr.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(candles)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def body(c):
    return abs(c["close"] - c["open"])


def crange(c):
    return max(c["high"] - c["low"], 1e-12)


def pinbar(c):
    r = crange(c)
    b = max(body(c), r * 0.01)
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    pos = (c["close"] - c["low"]) / r
    if lower >= 2.0 * b and upper <= 0.4 * lower and pos >= 0.70:
        return "bullish"
    if upper >= 2.0 * b and lower <= 0.4 * upper and pos <= 0.30:
        return "bearish"
    return None


def find_pivots(candles, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    out = []
    for i in range(left, len(candles) - right):
        c = candles[i]
        lefts, rights = candles[i - left:i], candles[i + 1:i + right + 1]
        if all(c["low"] <= x["low"] for x in lefts) and all(c["low"] < x["low"] for x in rights):
            out.append({"kind": "low", "price": c["low"], "idx": i, "time": c["time"]})
        if all(c["high"] >= x["high"] for x in lefts) and all(c["high"] > x["high"] for x in rights):
            out.append({"kind": "high", "price": c["high"], "idx": i, "time": c["time"]})
    return out


def market_structure(candles):
    recent = candles[-STRUCTURE_LOOKBACK:]
    piv = find_pivots(recent)
    highs = [p for p in piv if p["kind"] == "high"]
    lows = [p for p in piv if p["kind"] == "low"]
    res = {"bias": "NEUTRAL", "bos": None, "choch": None}
    if len(highs) < 2 or len(lows) < 2:
        return res
    h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
    if h2["price"] > h1["price"] and l2["price"] > l1["price"]:
        res["bias"] = "BULLISH"
    elif h2["price"] < h1["price"] and l2["price"] < l1["price"]:
        res["bias"] = "BEARISH"
    closes = [c["close"] for c in recent]
    n = len(closes)
    for i in range(n - 1, max(n - 1 - BREAK_WINDOW, 0), -1):
        if closes[i] > h2["price"] and closes[i - 1] <= h2["price"]:
            res["bos"] = "BULLISH"
            break
        if closes[i] < l2["price"] and closes[i - 1] >= l2["price"]:
            res["bos"] = "BEARISH"
            break
    if res["bos"] == "BULLISH" and res["bias"] == "BEARISH":
        res["choch"] = "BULLISH"
    elif res["bos"] == "BEARISH" and res["bias"] == "BULLISH":
        res["choch"] = "BEARISH"
    return res


def trend15(c15):
    closes = [c["close"] for c in c15]
    e9, e15 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    a = atr(c15, ATR_PERIOD)[-1]
    if not a or e9[-1] is None or e15[-1] is None or e9[-3] is None or e15[-3] is None:
        return {"state": "UNKNOWN", "gap_atr": 0.0}
    gap = (e9[-1] - e15[-1]) / a
    state = "NEUTRAL"
    if abs(gap) >= MIN_EMA_GAP_ATR:
        if e9[-1] > e15[-1] and e9[-1] > e9[-3] and e15[-1] > e15[-3] and closes[-1] > e15[-1]:
            state = "BULLISH"
        elif e9[-1] < e15[-1] and e9[-1] < e9[-3] and e15[-1] < e15[-3] and closes[-1] < e15[-1]:
            state = "BEARISH"
    return {"state": state, "gap_atr": abs(gap), "ema9": e9[-1], "ema15": e15[-1]}


# ============================================================ supply / demand zones
def find_zones(candles, tf):
    """Unbroken zones: base candle right before a strong impulse candle."""
    n = len(candles)
    if n < ATR_PERIOD + 5:
        return []
    a = atr(candles, ATR_PERIOD)
    look = ZONE_LOOKBACK_15M if tf == 15 else ZONE_LOOKBACK_5M
    found = []
    for i in range(max(1, n - look), n - 1):
        base, move = candles[i], candles[i + 1]
        av = a[i + 1]
        if not av:
            continue
        b = body(move)
        if b < av * ZONE_IMPULSE_ATR or b / crange(move) < ZONE_IMPULSE_BODY:
            continue
        later = candles[i + 2:]
        if move["close"] > move["open"]:
            side, low, high = "DEMAND", base["low"], max(base["open"], base["close"])
            broken = any(x["close"] < low for x in later)
        elif move["close"] < move["open"]:
            side, low, high = "SUPPLY", min(base["open"], base["close"]), base["high"]
            broken = any(x["close"] > high for x in later)
        else:
            continue
        height = high - low
        if broken or height <= 0 or height > av * ZONE_MAX_HEIGHT_ATR:
            continue
        touches, inside = 0, False
        for x in later[:max(len(later) - ZONE_RETEST_BARS, 0)]:   # visits BEFORE the current retest
            hit = x["low"] <= high and x["high"] >= low
            if hit and not inside:
                touches += 1
            inside = hit
        if touches > ZONE_MAX_TOUCHES:
            continue
        strength = b / av
        pts = int(tf == 15) + int(touches == 0) + int(strength >= 1.8) + int(height <= av * 0.9)
        found.append({"side": side, "low": low, "high": high, "tf": tf, "touches": touches,
                      "strength": round(strength, 2), "pts": pts, "grade": "A" if pts >= 3 else "B" if pts == 2 else "C",
                      "time": base["time"]})
    return found


def dedup_zones(zones):
    zones = sorted(zones, key=lambda z: (z["tf"], z["time"]), reverse=True)   # 15M first, newest first
    out = []
    for z in zones:
        h = z["high"] - z["low"]
        if any(o["side"] == z["side"] and min(o["high"], z["high"]) - max(o["low"], z["low"]) > 0.5 * h for o in out):
            continue
        out.append(z)
        if len(out) >= ZONE_MAX_KEEP:
            break
    return out


def zone_text(z):
    return f"{z['tf']}M {z['side'].lower()} {z['low']:.2f}-{z['high']:.2f} (grade {z['grade']}, touches {z['touches']})"


# ============================================================ context + setup search
def prepare_context(c1m):
    c5, c15 = resample(c1m, 5), resample(c1m, 15)
    if len(c5) < 60 or len(c15) < 30:
        return None
    s5 = atr(c5, ATR_PERIOD)
    a5 = s5[-1]
    if not a5:
        return None
    vals = [x for x in s5[-VOL_LOOKBACK:] if x]
    ratio, vol_block = 1.0, None
    if len(vals) >= 50:
        ratio = a5 / statistics.median(vals)
        if VOL_MIN_RATIO > 0 and ratio < VOL_MIN_RATIO:
            vol_block = f"volatility too low ({ratio:.2f}x)"
        elif VOL_MAX_RATIO > 0 and ratio > VOL_MAX_RATIO:
            vol_block = f"volatility spike ({ratio:.2f}x)"
    return {
        "a5": a5, "vol_ratio": ratio, "vol_block": vol_block,
        "t15": trend15(c15), "m5": market_structure(c5),
        "zones": dedup_zones(find_zones(c5, 5) + find_zones(c15, 15)),
        "p5": find_pivots(c5[-200:]), "p15": find_pivots(c15[-100:]),
    }


def get_ctx(cache, name, c1m):
    key = int((c1m[-1]["dt"] + dt.timedelta(minutes=1)).timestamp()) // 300
    hit = cache.get(name)
    if hit and hit[0] == key:
        return hit[1]
    ctx = prepare_context(c1m)
    cache[name] = (key, ctx)
    return ctx


def retest_info(c1m, z, buy, tol):
    """Price came back to the zone inside the last RETEST_WINDOW_1M candles and has not closed through it."""
    n = len(c1m)
    if n < RETEST_WINDOW_1M + 3:
        return None
    touch = None
    for k in range(n - RETEST_WINDOW_1M, n):
        c = c1m[k]
        if c["low"] <= z["high"] + tol and c["high"] >= z["low"] - tol:
            touch = k
            break
    if touch is None:
        return None
    prev = c1m[touch - 1]
    seg = c1m[touch:]
    if buy:
        if prev["close"] < z["low"] or any(x["close"] < z["low"] - tol for x in seg):
            return None
        extreme = min(x["low"] for x in seg)
        swept = extreme < z["low"]
    else:
        if prev["close"] > z["high"] or any(x["close"] > z["high"] + tol for x in seg):
            return None
        extreme = max(x["high"] for x in seg)
        swept = extreme > z["high"]
    return {"touch": touch, "extreme": extreme, "swept": swept}


def confirm_patterns(c1m, r, buy):
    """1M confirmation on the last closed candle: PIN, ENGULF, CHOCH1M."""
    c, p = c1m[-1], c1m[-2]
    out = []
    if buy:
        if c["close"] <= c["open"]:
            return out
        if pinbar(c) == "bullish":
            out.append("PIN")
        if p["close"] < p["open"] and c["close"] > p["open"] and c["open"] <= p["close"] and body(c) >= body(p):
            out.append("ENGULF")
        leg = c1m[r["touch"]:-1]
        if len(leg) >= 2 and c["close"] > max(x["high"] for x in leg):
            out.append("CHOCH1M")
    else:
        if c["close"] >= c["open"]:
            return out
        if pinbar(c) == "bearish":
            out.append("PIN")
        if p["close"] > p["open"] and c["close"] < p["open"] and c["open"] >= p["close"] and body(c) >= body(p):
            out.append("ENGULF")
        leg = c1m[r["touch"]:-1]
        if len(leg) >= 2 and c["close"] < min(x["low"] for x in leg):
            out.append("CHOCH1M")
    return out


def build_plan(side, entry, z, r, ctx, cfg):
    """SL = swing low/high (or zone edge if further), TP = next swing high/low with min RR."""
    buy = side == "BUY"
    sign = 1 if buy else -1
    a = ctx["a5"]
    sl_kind, tp_kind = ("low", "high") if buy else ("high", "low")
    swings = [p for p in ctx["p5"] if p["kind"] == sl_kind and sign * (entry - p["price"]) > 0]
    cands = [z["low"] if buy else z["high"], r["extreme"]]
    swing_txt = "zone edge"
    if swings:
        sw = max(swings, key=lambda p: p["idx"])["price"]
        cands.append(sw)
        swing_txt = "5M swing " + sl_kind
    base_sl = min(cands) if buy else max(cands)
    sl = base_sl - sign * a * SL_BUF_ATR
    risk = sign * (entry - sl)
    if risk <= 0:
        return None, "SL on the wrong side of price"
    if risk < a * MIN_SL_ATR:
        risk = a * MIN_SL_ATR
        sl = entry - sign * risk
    if risk > a * MAX_SL_ATR:
        return None, f"SL {risk / a:.1f} ATR is wider than {MAX_SL_ATR:g}"
    cost_r = cfg["spread"] / risk
    if MAX_COST_R > 0 and cost_r > MAX_COST_R:
        return None, f"spread is {cost_r:.2f}R of the SL (max {MAX_COST_R:g}R)"

    ahead = []
    for p in ctx["p5"]:
        if p["kind"] == tp_kind and sign * (p["price"] - entry) > 0:
            ahead.append((sign * (p["price"] - entry), p["price"], "5M swing " + tp_kind, False))
    for p in ctx["p15"]:
        if p["kind"] == tp_kind and sign * (p["price"] - entry) > 0:
            ahead.append((sign * (p["price"] - entry), p["price"], "15M swing " + tp_kind, True))
    opp_kind = "SUPPLY" if buy else "DEMAND"
    for zz in ctx["zones"]:
        if zz["side"] == opp_kind:
            edge = zz["low"] if buy else zz["high"]
            if sign * (edge - entry) > 0:
                ahead.append((sign * (edge - entry), edge, f"{zz['tf']}M {opp_kind.lower()} zone", True))
    ahead.sort()
    buf = a * TP_BUF_ATR
    for d, _p, lab, strong in ahead:
        if strong and (d - buf) / risk < MIN_RR:
            return None, f"{lab} only {d / risk:.1f}R away (need {MIN_RR:g}R room)"
    tp_d, label = None, None
    for d, _p, lab, _s in ahead:
        if (d - buf) / risk >= MIN_RR:
            tp_d, label = d - buf, lab
            break
    if tp_d is None:
        tp_d, label = risk * DEFAULT_RR, "fixed RR (no swing ahead)"
    rr = tp_d / risk
    if rr > MAX_RR:
        tp_d, rr, label = risk * MAX_RR, MAX_RR, label + " (capped)"
    return {
        "sl": sl, "tp": entry + sign * tp_d, "risk": risk, "rr": rr, "tp_label": label,
        "sl_label": swing_txt, "cost_r": cost_r,
        "be_level": entry + sign * risk * BE_AT_R if BE_AT_R > 0 else None,
    }, None


def find_setups(c1m, ctx, cfg, ts):
    price = c1m[-1]["close"]
    a5 = ctx["a5"]
    tol = a5 * ZONE_TOL_ATR
    signals, diag, watch, rejected = [], [], [], None
    for side in ("BUY", "SELL"):
        buy = side == "BUY"
        wanted, opp = ("BULLISH", "BEARISH") if buy else ("BEARISH", "BULLISH")
        blocked = []
        if ctx["t15"]["state"] != wanted:
            blocked.append("15M EMA 9/15 trend not " + wanted.lower())
        if ctx["m5"]["bias"] == opp:
            blocked.append("5M structure against")
        if ctx["vol_block"]:
            blocked.append(ctx["vol_block"])
        want_kind, opp_kind = ("DEMAND", "SUPPLY") if buy else ("SUPPLY", "DEMAND")
        for zz in ctx["zones"]:
            if zz["side"] == opp_kind and zz["low"] - tol <= price <= zz["high"] + tol:
                blocked.append("price inside opposing " + opp_kind.lower() + " zone")
                break
        if blocked:
            diag.append({"side": side, "blocked": blocked})
            continue

        best, note = None, "no fresh zone retest"
        for z in sorted((x for x in ctx["zones"] if x["side"] == want_kind), key=lambda x: (-x["pts"], -x["tf"])):
            r = retest_info(c1m, z, buy, tol)
            if not r:
                continue
            conf = confirm_patterns(c1m, r, buy)
            if not conf:
                note = "zone retest, waiting for 1M confirmation"
                watch.append({"side": side, "zone": z})
                continue
            best = (z, r, conf)
            break
        if not best:
            diag.append({"side": side, "blocked": [note]})
            continue

        z, r, conf = best
        plan, why = build_plan(side, price, z, r, ctx, cfg)
        if plan is None:
            rejected = (side, why)
            diag.append({"side": side, "blocked": ["plan rejected: " + why]})
            continue

        score, reasons = 2, ["15M EMA 9/15 trend aligned"]
        if ctx["t15"]["gap_atr"] >= 0.30:
            score += 1
            reasons.append("strong EMA separation")
        if ctx["m5"]["bias"] == wanted:
            score += 2
            reasons.append("5M structure aligned (" + ("HH/HL" if buy else "LH/LL") + ")")
        elif ctx["m5"]["bos"] == wanted or ctx["m5"]["choch"] == wanted:
            score += 1
            reasons.append("recent 5M BOS/CHoCH in trade direction")
        reasons.append(f"retest of {zone_text(z)}")
        if z["tf"] == 15:
            score += 1
        if z["touches"] == 0:
            score += 1
            reasons.append("fresh zone (first retest)")
        if z["strength"] >= 1.8:
            score += 1
            reasons.append("strong impulse left the zone")
        score += 2 if len(conf) >= 2 else 1
        reasons.append("1M confirmation: " + " + ".join(conf))
        if r["swept"]:
            score += 1
            reasons.append("wick swept the zone edge, price closed back")
        if plan["rr"] >= 2.0:
            score += 1
        diag_row = {"side": side, "blocked": []}
        if score < cfg["min_score"]:
            diag_row["blocked"] = [f"score {score}<{cfg['min_score']}"]
            diag.append(diag_row)
            continue

        c = c1m[-1]
        plan.update({
            "side": side, "entry": price, "zone": dict(z), "confirm": conf, "score": score,
            "grade": "A" if score >= 10 else "B" if score >= 8 else "C",
            "reasons": reasons, "atr5": a5, "vol_ratio": ctx["vol_ratio"],
            "t15": ctx["t15"]["state"], "m5_bias": ctx["m5"]["bias"],
            "time": c["time"], "close_dt": c["dt"] + dt.timedelta(minutes=1),
            "session": session_name(c["dt"]), "swept": r["swept"],
            "chase_limit": price + (1 if buy else -1) * a5 * MAX_CHASE_ATR,
            "features": {
                "session": session_name(c["dt"]), "side": side, "zone_tf": f"{z['tf']}M",
                "zone_grade": z["grade"], "zone_touch": "FRESH" if z["touches"] == 0 else "TESTED",
                "confirm": conf[-1] if "CHOCH1M" in conf else conf[0],
                "score_band": "HIGH" if score >= 10 else "MID" if score >= 8 else "LOW",
                "volatility": "LOW" if ctx["vol_ratio"] < 0.75 else "HIGH" if ctx["vol_ratio"] > 1.5 else "NORMAL",
                "rr_band": "RR>=3" if plan["rr"] >= 3 else "RR2-3" if plan["rr"] >= 2 else "RR<2",
                "m5_aligned": "YES" if ctx["m5"]["bias"] == wanted else "NO",
                "weekday": c["dt"].strftime("%a"),
            },
        })
        signals.append(plan)
        diag.append({"side": side, "blocked": []})
    signals.sort(key=lambda s: s["score"], reverse=True)
    return {"signals": signals, "diag": diag, "watch": watch, "rejected": rejected}


def signal_still_valid(plan, c1m):
    """The setup must not have played out / run away since the confirmation candle."""
    buy = plan["side"] == "BUY"
    for c in c1m:
        if c["dt"] < plan["close_dt"]:
            continue
        if (c["low"] <= plan["sl"]) if buy else (c["high"] >= plan["sl"]):
            return False, "SL level already touched"
        if (c["high"] >= plan["tp"]) if buy else (c["low"] <= plan["tp"]):
            return False, "TP level already touched"
    last = c1m[-1]["close"]
    if (last > plan["chase_limit"]) if buy else (last < plan["chase_limit"]):
        return False, "price already ran past the max-chase limit"
    return True, ""


# ============================================================ state
def _empty_state():
    return {"state_version": 1, "open_trades": [], "closed_trades": [], "post_sl": [], "sent_keys": []}


def load_state():
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            raise ValueError("state root must be an object")
        for k, v in _empty_state().items():
            s.setdefault(k, v)
        return s
    except (OSError, ValueError) as exc:
        backup = STATE_FILE.with_suffix(f".corrupt-{int(time.time())}.json")
        try:
            STATE_FILE.replace(backup)
        except OSError:
            pass
        print(f"[STATE] invalid state moved to {backup}: {exc}")
        return _empty_state()


def save_state(state):
    if OFFLINE:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def git_sync(msg="state: update"):
    if OFFLINE or not GIT_PUSH_STATE:
        return
    try:
        run = lambda *a: subprocess.run(a, check=False, capture_output=True, text=True, timeout=60)
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        run("git", "add", str(STATE_FILE))
        if run("git", "diff", "--cached", "--quiet").returncode != 0:
            run("git", "commit", "-m", msg)
            ref = os.environ.get("GITHUB_REF_NAME", "main")
            run("git", "pull", "--rebase", "--autostash", "origin", ref)
            run("git", "push")
    except Exception as exc:
        print(f"[GIT] sync failed: {exc}")


def prune_state(state, ts):
    today = ts.strftime("%Y-%m-%d")
    for k in [k for k in state if k.startswith(("api:", "daycount:", "apiwarn:")) and not k.endswith(today)]:
        del state[k]


# ============================================================ news
def refresh_news(state, ts):
    if OFFLINE or not NEWS_AUTO:
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
        r = SESSION.get(NEWS_CALENDAR_URL, timeout=15)
        r.raise_for_status()
        events = []
        for row in r.json():
            if str(row.get("impact", "")).lower() != "high" or str(row.get("country", "")).upper() not in NEWS_CURRENCIES:
                continue
            d = dt.datetime.fromisoformat(row["date"])
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            events.append(d.astimezone(UTC).strftime("%Y-%m-%d %H:%M"))
        state["news_events"] = sorted(set(events))
        print(f"[NEWS] {len(events)} high-impact events loaded")
    except Exception as exc:
        state["news_fetched"] = _iso(ts - dt.timedelta(hours=NEWS_REFRESH_HOURS - 1))   # retry in ~1h
        print(f"[NEWS] fetch failed (manual list still works): {exc}")


def _news_times(state):
    out = []
    for s in list(NEWS_MANUAL) + list((state or {}).get("news_events", [])):
        try:
            out.append(dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC))
        except ValueError:
            continue
    return out


def news_distance_min(ts, state):
    ds = [abs((ts - e).total_seconds()) / 60 for e in _news_times(state)]
    return min(ds) if ds else None


def in_news_blackout(ts, state):
    d = news_distance_min(ts, state)
    return d is not None and d <= NEWS_WINDOW_MIN


# ============================================================ risk gates
def today_stats(state, ts):
    day = ts.strftime("%Y-%m-%d")
    n_sl, net_r = 0, 0.0
    for t in reversed(state.get("closed_trades", [])):
        if not str(t.get("closed", "")).startswith(day):
            if str(t.get("closed", "")) < day:
                break
            continue
        if t.get("shadow"):
            continue
        net_r += t["r"]
        if t["result"] == "SL":
            n_sl += 1
    return n_sl, net_r


def gate_reason(state, name, cfg, ts):
    if cfg.get("weekend_closed") and market_closed_weekend(ts):
        return "Market closed (weekend)"
    if cfg["sessions"] and not in_session(ts, cfg["sessions"]):
        return "Outside London/New York session (Asian session skipped)"
    if in_news_blackout(ts, state):
        return "High-impact news blackout"
    n_sl, net_r = today_stats(state, ts)
    if DAILY_MAX_SL > 0 and n_sl >= DAILY_MAX_SL:
        return f"Daily stop-loss limit reached ({n_sl} SL today)"
    if DAILY_LOSS_LIMIT_R > 0 and net_r <= -DAILY_LOSS_LIMIT_R:
        return f"Daily loss limit reached ({net_r:+.2f}R today)"
    cu = state.get(f"cool_until:{name}")
    if cu and ts < _from_iso(cu):
        return f"Cooldown after SL until {_from_iso(cu).strftime('%H:%M')} UTC"
    return None


# ============================================================ learning
LEARN_DIMS = ("session", "side", "zone_tf", "zone_grade", "zone_touch", "confirm",
              "score_band", "volatility", "rr_band", "m5_aligned", "weekday")


def _buckets(state, name, ts):
    since = ts - dt.timedelta(days=LEARN_WINDOW_DAYS)
    b = {}
    for t in state.get("closed_trades", []):
        if t.get("name") != name or not isinstance(t.get("features"), dict):
            continue
        try:
            if _from_iso(t["closed"]) < since:
                continue
        except (KeyError, ValueError):
            continue
        for dim in LEARN_DIMS:
            v = t["features"].get(dim)
            if v is not None:
                b.setdefault((dim, str(v)), []).append(float(t["r"]))
    return b


def learning_decision(state, name, features, ts):
    """Block only conditions with a big enough sample and clearly negative expectancy."""
    if not LEARN_ON:
        return None
    b = _buckets(state, name, ts)
    bad = []
    for dim, v in features.items():
        vals = b.get((dim, str(v)), [])
        if len(vals) < LEARN_MIN_TRADES:
            continue
        avg = sum(vals) / len(vals)
        wr = sum(1 for x in vals if x > 0) / len(vals)
        if avg <= -LEARN_BAD_AVG_R and wr < LEARN_BAD_WINRATE:
            bad.append((len(vals), -avg, dim, v, avg, wr))
    if not bad:
        return None
    bad.sort(reverse=True)
    n, _x, dim, v, avg, wr = bad[0]
    return f"{dim}={v}: {n} trades, avg {avg:+.2f}R, win {wr * 100:.0f}%"


def learning_report(state, name, ts, limit=3):
    rows = []
    for (dim, v), vals in _buckets(state, name, ts).items():
        if len(vals) >= max(5, LEARN_MIN_TRADES // 2):
            rows.append((sum(vals) / len(vals), dim, v, len(vals), sum(1 for x in vals if x > 0) / len(vals)))
    if not rows:
        return ["Learning: still collecting trades, no reliable pattern yet."]
    rows.sort()
    out = ["Weakest conditions:"]
    out += [f"  - {d}={v}: {n} trades, avg {a:+.2f}R, win {w * 100:.0f}%" for a, d, v, n, w in rows[:limit]]
    out.append("Strongest conditions:")
    out += [f"  - {d}={v}: {n} trades, avg {a:+.2f}R, win {w * 100:.0f}%" for a, d, v, n, w in rows[::-1][:limit]]
    return out


# ============================================================ trades
def register_trade(state, name, plan, shadow=False, ts=None):
    tid = f"{name}:{plan['time']}:{plan['side']}"
    if any(t["id"] == tid for t in state["open_trades"]):
        return
    near = news_distance_min(ts or now_utc(), state)
    state["open_trades"].append({
        "id": tid, "name": name, "side": plan["side"], "entry": plan["entry"], "sl": plan["sl"], "sl_cur": plan["sl"],
        "tp": plan["tp"], "risk": plan["risk"], "rr": plan["rr"], "cost_r": round(plan["cost_r"], 4),
        "be_level": plan.get("be_level"), "be": False, "score": plan["score"], "grade": plan["grade"],
        "session": plan["session"], "features": plan["features"], "zone": plan["zone"],
        "confirm": plan["confirm"], "vol_ratio": plan["vol_ratio"], "m5_bias": plan["m5_bias"],
        "news_near": bool(near is not None and near <= 60), "shadow": shadow, "bars": 0,
        "start_dt": _iso(plan["close_dt"]), "checked_until": _iso(plan["close_dt"] - dt.timedelta(microseconds=1)),
        "last_close": plan["entry"],
    })


def _r_of(t, price):
    sign = 1 if t["side"] == "BUY" else -1
    return sign * (price - t["entry"]) / t["risk"]


def advance_trade(t, c1m, ts):
    """Follow one trade on 1M candles. SL is counted first if one candle touches both SL and TP."""
    buy = t["side"] == "BUY"
    reached = (lambda c, v: c["high"] >= v) if buy else (lambda c, v: c["low"] <= v)
    against = (lambda c, v: c["low"] <= v) if buy else (lambda c, v: c["high"] >= v)
    checked = _from_iso(t["checked_until"])
    deadline = _from_iso(t["start_dt"]) + dt.timedelta(minutes=TRADE_MAX_AGE_MIN)
    events, closed = [], None
    for c in c1m:
        if c["dt"] <= checked:
            continue
        if c["dt"] > deadline:
            break
        checked = c["dt"]
        t["last_close"] = c["close"]
        t["bars"] += 1
        if against(c, t["sl_cur"]):
            closed = ("BE", 0.0, t["sl_cur"]) if t["be"] else ("SL", -1.0, t["sl"])
            break
        if reached(c, t["tp"]):
            closed = ("TP", t["rr"], t["tp"])
            break
        if t.get("be_level") and not t["be"] and reached(c, t["be_level"]):
            t["be"], t["sl_cur"] = True, t["entry"]
            events.append("BE_MOVE")
    t["checked_until"] = _iso(checked)
    if closed is None and ts > deadline:
        closed = ("EXPIRED", _r_of(t, t["last_close"]), t["last_close"])
    return events, closed


def loss_tags(t, label, r):
    if r >= 0:
        return []
    tags = []
    if t["bars"] <= 5:
        tags.append("FAST_SL")
    z = t.get("zone") or {}
    if z.get("touches", 0) >= 1 or z.get("grade") == "C":
        tags.append("WEAK_ZONE")
    if t.get("news_near"):
        tags.append("NEAR_NEWS")
    if t.get("vol_ratio", 1) < 0.75:
        tags.append("LOW_VOL")
    elif t.get("vol_ratio", 1) > 1.5:
        tags.append("HIGH_VOL")
    if t.get("m5_bias") == "NEUTRAL":
        tags.append("COUNTER_5M")
    if t.get("cost_r", 0) > 0.12:
        tags.append("HIGH_COST")
    return tags


def close_trade(state, t, closed, ts):
    label, r_gross, _price = closed
    r = round(r_gross - t["cost_r"], 3)
    state["closed_trades"].append({
        "id": t["id"], "name": t["name"], "side": t["side"], "result": label, "r": r, "score": t["score"],
        "grade": t["grade"], "session": t["session"], "features": t["features"], "shadow": t["shadow"],
        "tags": loss_tags(t, label, r), "bars": t["bars"], "opened": t["start_dt"], "closed": _iso(ts),
        "zone_tf": t["zone"]["tf"], "zone_grade": t["zone"]["grade"],
    })
    del state["closed_trades"][:-MAX_CLOSED_HISTORY]
    if t["shadow"]:
        return
    if label == "SL":
        state[f"cool_until:{t['name']}"] = _iso(ts + dt.timedelta(minutes=COOLDOWN_AFTER_SL_MIN))
        state["post_sl"].append({"id": t["id"], "name": t["name"], "side": t["side"], "entry": t["entry"],
                                 "risk": t["risk"], "closed": _iso(ts)})


def check_post_sl(state, name, c1m, ts):
    """After an SL: did price go on to reach +1R from entry? Then the stop was probably hunted."""
    keep = []
    for p in state["post_sl"]:
        if p["name"] != name:
            keep.append(p)
            continue
        t0 = _from_iso(p["closed"])
        buy = p["side"] == "BUY"
        level = p["entry"] + (p["risk"] if buy else -p["risk"])
        hunted = False
        for c in c1m:
            if c["dt"] < t0 or c["dt"] > t0 + dt.timedelta(minutes=STOPHUNT_WINDOW_MIN):
                continue
            if (c["high"] >= level) if buy else (c["low"] <= level):
                hunted = True
                break
        if hunted:
            for x in reversed(state["closed_trades"]):
                if x["id"] == p["id"]:
                    x.setdefault("tags", []).append("SL_HUNTED")
                    break
        elif ts <= t0 + dt.timedelta(minutes=STOPHUNT_WINDOW_MIN):
            keep.append(p)
    state["post_sl"] = keep


def lot_size(cfg, risk):
    if ACCOUNT_BALANCE <= 0 or risk <= 0 or not cfg.get("value_per_lot"):
        return None
    return max(math.floor(ACCOUNT_BALANCE * RISK_PCT / 100 / (risk * cfg["value_per_lot"]) * 100) / 100, 0.0)


# ============================================================ messages
def _fmt_r(r):
    return f"{r:+.2f}R"


def build_signal_message(name, s, cfg, digits):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    f = lambda v: f"{v:.{digits}f}"
    lots = lot_size(cfg, s["risk"])
    lot_line = f"Lot ({RISK_PCT:g}% of {ACCOUNT_BALANCE:g}): <b>{lots:.2f}</b>\n" if lots is not None else ""
    be_line = f"After {f(s['be_level'])} (+{BE_AT_R:g}R): move SL to entry.\n" if s.get("be_level") else ""
    late = f" · found {s['late_min']} candle late" if s.get("late_min") else ""
    reasons = "\n".join("✓ " + html.escape(x) for x in s["reasons"])
    return (
        f"⚡ {icon} <b>{name} — SCALP {s['side']}</b>\n━━━━━━━━━━━━━━━━\n"
        f"⭐ Grade <b>{s['grade']}</b> · Score <b>{s['score']}/{SCORE_MAX}</b>{late}\n"
        f"🧭 15M: <b>{s['t15']}</b> · 5M structure: <b>{s['m5_bias']}</b> · {s['session']}\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <b>{f(s['entry'])}</b> (1M close, market) — skip if price is already past <b>{f(s['chase_limit'])}</b>\n"
        f"SL: <b>{f(s['sl'])}</b> ({html.escape(s['sl_label'])})\n"
        f"TP: <b>{f(s['tp'])}</b> (RR <b>{s['rr']:.1f}</b> · {html.escape(s['tp_label'])})\n"
        f"Risk: {f(s['risk'])} ({s['risk'] / s['atr5']:.2f} x 5M ATR) · spread ≈ {s['cost_r']:.2f}R\n"
        f"{lot_line}{be_line}\n"
        f"<b>Why</b>\n{reasons}\n\n"
        f"⏰ Closed 1M candle: {s['time']} UTC\n"
        f"⚠️ Rule-based alert, not a guarantee. Use your own risk rules."
    )


def build_watch_message(name, w, price, digits):
    z = w["zone"]
    return (f"👀 <b>{name} — retesting a {z['side'].lower()} zone ({w['side']} side)</b>\n"
            f"Price {price:.{digits}f} · {html.escape(zone_text(z))}\n"
            f"Waiting for a 1M confirmation candle. Heads-up only, NOT a signal.")


def trade_event_text(t, kind, digits, closed=None):
    f = lambda v: f"{v:.{digits}f}"
    head = f"<b>{t['name']} {t['side']}</b>"
    if kind == "BE_MOVE":
        return f"🔒 {head} — +{BE_AT_R:g}R reached. Move SL to entry ({f(t['entry'])})."
    label, r_gross, price = closed
    r = r_gross - t["cost_r"]
    if label == "TP":
        return f"🏆 {head} — <b>TP hit</b> at {f(price)}\nResult: <b>{_fmt_r(r)}</b>"
    if label == "SL":
        return f"❌ {head} — <b>SL hit</b> at {f(price)}\nResult: <b>{_fmt_r(r)}</b>"
    if label == "BE":
        return f"➖ {head} — stopped at breakeven\nResult: <b>{_fmt_r(r)}</b> (spread cost only)"
    return f"⌛ {head} — closed by timeout ({TRADE_MAX_AGE_MIN} min) at {f(price)}\nResult: <b>{_fmt_r(r)}</b>"


def _stat_lines(trades):
    if not trades:
        return ["No closed trades."]
    n, tot = len(trades), sum(x["r"] for x in trades)
    w = sum(1 for x in trades if x["r"] > 0)
    cnt = {}
    for x in trades:
        cnt[x["result"]] = cnt.get(x["result"], 0) + 1
    return [f"Trades {n} · win {w} ({w / n * 100:.0f}%) · total <b>{_fmt_r(tot)}</b> (avg {_fmt_r(tot / n)})",
            " · ".join(f"{k} {v}" for k, v in sorted(cnt.items()))]


def _max_dd(trades):
    peak = cum = dd = 0.0
    for x in sorted(trades, key=lambda x: x["closed"]):
        cum += x["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _group_lines(trades, key):
    g = {}
    for x in trades:
        g.setdefault(str(x.get(key) or "?"), []).append(x)
    return [f"{k}: {len(v)} trades · {_fmt_r(sum(y['r'] for y in v))} · win {sum(1 for y in v if y['r'] > 0) / len(v) * 100:.0f}%"
            for k, v in sorted(g.items())]


def build_report(state, ts, days, title):
    since = ts - dt.timedelta(days=days)
    allc = [x for x in state["closed_trades"] if _from_iso(x["closed"]) >= since]
    real = [x for x in allc if not x.get("shadow")]
    shadow = [x for x in allc if x.get("shadow")]
    lines = [f"📊 <b>{title}</b>", "━━━━━━━━━━━━━━━━"]
    for n in sorted({x["name"] for x in real}):
        g = [x for x in real if x["name"] == n]
        lines.append(f"<b>{n}</b>")
        lines += _stat_lines(g)
        lines.append(f"Max drawdown {_max_dd(g):.2f}R")
    lines.append("<b>All symbols</b>")
    lines += _stat_lines(real)
    if real and days >= 7:
        lines += ["", "<b>By session</b>"] + _group_lines(real, "session")
        lines += ["", "<b>By grade</b>"] + _group_lines(real, "grade")
        lines += ["", "<b>By zone timeframe</b>"] + _group_lines(real, "zone_tf")
        tags = {}
        for x in real:
            for tg in x.get("tags", []):
                tags[tg] = tags.get(tg, 0) + 1
        losses = sum(1 for x in real if x["r"] < 0)
        if tags and losses:
            lines += ["", f"<b>Loss reasons</b> ({losses} losing trades)"]
            lines += [f"{k}: {v} ({v / losses * 100:.0f}%)" for k, v in sorted(tags.items(), key=lambda kv: -kv[1])]
            if tags.get("SL_HUNTED", 0) / losses >= 0.35:
                lines.append("⚠️ Many stops were hit and then price went on to +1R: SL may be too tight (try SL_BUF_ATR up).")
        for n in sorted({x["name"] for x in real}):
            lines += ["", f"<b>{n} learning</b>"] + learning_report(state, n, ts)
        if shadow:
            lines += ["", f"<b>Shadow trades (blocked by learning)</b>", *_stat_lines(shadow)]
    if len(real) < 30:
        lines.append("\n⚠️ Small sample (<30 trades): do not tune settings from this yet.")
    if state["open_trades"]:
        op = [t for t in state["open_trades"] if not t["shadow"]]
        if op:
            lines.append(f"\nOpen now: {len(op)}")
            lines += [f"• {t['name']} {t['side']} @ {t['entry']:.3f}{' (BE)' if t['be'] else ''}" for t in op]
    lines.append("\nR is net of spread cost. SL counted first when one candle touches both SL and TP.")
    return "\n".join(lines)


def maybe_send_reports(state, ts):
    day = ts.strftime("%Y-%m-%d")
    if ts.hour >= DAILY_SUMMARY_HOUR_UTC and state.get("dailysent") != day:
        has = any(_from_iso(x["closed"]) >= ts - dt.timedelta(days=1) and not x.get("shadow") for x in state["closed_trades"])
        if has or any(not t["shadow"] for t in state["open_trades"]):
            notify(build_report(state, ts, 1, "Daily summary (last 24h)")
                   + f"\nTwelve Data credits today: {state.get('api:' + day, 0)}/{TWELVE_DAILY_LIMIT}")
        state["dailysent"] = day
        save_state(state)
    iso = ts.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if ts.weekday() == 6 and ts.hour >= WEEKLY_REPORT_HOUR_UTC and state.get("weeklysent") != week:
        notify(build_report(state, ts, 7, "Weekly report (last 7 days)"))
        notify(build_report(state, ts, 30, "30-day report"))
        state["weeklysent"] = week
        save_state(state)


# ============================================================ one scan of one symbol
def track_trades(state, name, c1m, digits, ts):
    keep, changed = [], False
    for t in state["open_trades"]:
        if t["name"] != name:
            keep.append(t)
            continue
        events, closed = advance_trade(t, c1m, ts)
        if not t["shadow"]:
            for ev in events:
                notify(trade_event_text(t, ev, digits))
                changed = True
        if closed:
            if not t["shadow"]:
                notify(trade_event_text(t, "END", digits, closed))
                changed = True
            close_trade(state, t, closed, ts)
            print(f"[{name}] {'shadow ' if t['shadow'] else ''}trade closed {closed[0]} {closed[1] - t['cost_r']:+.2f}R")
        else:
            keep.append(t)
    state["open_trades"] = keep
    return changed


def scan_symbol(state, name, cfg, c1m, ts, cache):
    """Search a setup and apply every remaining filter. Returns a dict describing what happened."""
    ctx = get_ctx(cache, name, c1m)
    res = {"status": "none", "diag": [], "watch": [], "ctx": ctx}
    if ctx is None:
        res["reason"] = "not enough candles"
        return res
    plan = None
    for cut in range(0, CATCHUP_CANDLES + 1):
        snap = c1m[:len(c1m) - cut] if cut else c1m
        out = find_setups(snap, ctx, cfg, ts)
        if cut == 0:
            res["diag"], res["watch"] = out["diag"], out["watch"]
        if out["signals"]:
            cand = out["signals"][0]
            ok, why = signal_still_valid(cand, c1m)
            if ok:
                plan, cand["late_min"] = cand, cut
                break
            res["reason"] = "signal no longer valid: " + why
    if plan is None:
        return res

    key = f"{name}:{plan['time']}:{plan['side']}"
    if key in state["sent_keys"]:
        res["reason"] = "duplicate"
        return res
    real_open = [t for t in state["open_trades"] if not t["shadow"]]
    if sum(1 for t in real_open if t["name"] == name) >= MAX_OPEN_PER_SYMBOL:
        res["reason"] = "open-trade cap for this symbol"
        return res
    if len(real_open) >= MAX_OPEN_TOTAL:
        res["reason"] = "open-trade cap reached"
        return res
    day = ts.strftime("%Y-%m-%d")
    if sum(v for k, v in state.items() if k.startswith("daycount:") and k.endswith(day)) >= MAX_SIGNALS_PER_DAY:
        res["reason"] = "daily signal cap"
        return res
    zkey = f"zone:{name}:{plan['side']}:{plan['zone']['tf']}:{plan['zone']['time']}"
    last_z = state.get(zkey)
    if last_z and (ts - _from_iso(last_z)).total_seconds() < ZONE_REUSE_MIN * 60:
        res["reason"] = "this zone was already traded recently"
        return res

    why_block = learning_decision(state, name, plan["features"], ts)
    shadow = bool(why_block) and LEARN_MODE == "auto"
    if why_block and LEARN_MODE != "auto":
        res["advice"] = why_block                  # advise mode: trade is sent, but the bot warns
    state["sent_keys"].append(key)
    del state["sent_keys"][:-100]
    state[zkey] = _iso(ts)
    register_trade(state, name, plan, shadow=shadow, ts=ts)
    if shadow:
        res.update(status="shadow", plan=plan, reason="learning filter: " + why_block)
        return res
    state[f"daycount:{name}:{day}"] = state.get(f"daycount:{name}:{day}", 0) + 1
    res.update(status="signal", plan=plan)
    return res


# ============================================================ live loop
def _api_used(state, day):
    return state.get(f"api:{day}", 0) + _API["calls"]


def _flush_api(state, day):
    if _API["calls"]:
        state[f"api:{day}"] = state.get(f"api:{day}", 0) + _API["calls"]
        _API["calls"] = 0


def run_once(cache):
    state = load_state()
    ts0 = now_utc()
    day0 = ts0.strftime("%Y-%m-%d")
    prune_state(state, ts0)
    _API["calls"] = 0
    refresh_news(state, ts0)
    try:
        maybe_send_reports(state, ts0)
    except Exception as exc:
        print(f"[REPORT] error: {exc}")

    td_in_session = 0
    secs_left = 0
    for sym in SYMBOLS:
        name = sym["name"]
        if name not in ENABLED:
            continue
        digits = sym["digits"]
        try:
            ts = now_utc()
            day = ts.strftime("%Y-%m-%d")
            cfg = sym_cfg(name)
            has_open = any(t["name"] == name for t in state["open_trades"])
            has_post = any(p["name"] == name for p in state["post_sl"])
            reason = gate_reason(state, name, cfg, ts)
            if reason and not has_open and not has_post:
                print(f"[{name}] paused: {reason}")
                _maybe_status(state, name, f"⚪ <b>{name}</b>\n{html.escape(reason)} — scanning paused. (Bot is alive.)", ts)
                continue
            if (sym["source"] != "binance") and TWELVE_DAILY_LIMIT > 0 and _api_used(state, day) >= TWELVE_DAILY_LIMIT * 0.95:
                print(f"[{name}] Twelve Data daily budget reached, scan skipped")
                if not state.get(f"apiwarn:{day}"):
                    state[f"apiwarn:{day}"] = 1
                    notify(f"⚠️ Twelve Data daily credit budget ({TWELVE_DAILY_LIMIT}) is almost used. Scanning paused until tomorrow (UTC).")
                continue
            if not reason and sym["source"] != "binance":
                td_in_session += 1
                ends = [b for a, b in (cfg["sessions"] or [(0, 24)]) if a <= ts.hour < b]
                secs_left = max(secs_left, (max(ends) * 3600 - (ts.hour * 3600 + ts.minute * 60 + ts.second)) if ends else 0)

            c1m = get_1m(sym)
            age_min = (ts - (c1m[-1]["dt"] + dt.timedelta(minutes=1))).total_seconds() / 60
            if track_trades(state, name, c1m, digits, ts):
                save_state(state)
                git_sync("state: trade update")
            check_post_sl(state, name, c1m, ts)
            state.pop(f"err:{name}", None)
            if reason:
                continue
            if age_min > STALE_DATA_MIN:
                print(f"[{name}] data is {age_min:.1f} min old, scan skipped")
                continue

            res = scan_symbol(state, name, cfg, c1m, ts, cache)
            plan = res.get("plan")
            if res["status"] == "signal":
                notify(build_signal_message(name, plan, cfg, digits)
                       + (f"\n\n🧠 Learning note: {html.escape(res['advice'])}" if res.get("advice") else ""))
                save_state(state)
                git_sync("state: new signal")
                print(f"[{name}] SENT {plan['side']} score={plan['score']} rr={plan['rr']:.1f}")
            elif res["status"] == "shadow":
                save_state(state)
                print(f"[{name}] {plan['side']} signal suppressed ({res['reason']}) - tracked as shadow trade")
            else:
                why = "; ".join(f"{d['side']}: {', '.join(d['blocked'])}" for d in res["diag"] if d["blocked"])
                print(f"[{name}] no trade | {res.get('reason', '')} {why}")
                if SEND_WATCH and res["watch"]:
                    w = res["watch"][0]
                    wk = f"watch:{name}:{w['side']}:{w['zone']['time']}"
                    if not state.get(wk) or (ts - _from_iso(state[wk])).total_seconds() > 1800:
                        notify(build_watch_message(name, w, c1m[-1]["close"], digits))
                        state[wk] = _iso(ts)
                ctx = res.get("ctx")
                if ctx:
                    _maybe_status(state, name,
                                  f"🟡 <b>{name} — NO TRADE</b>\nPrice {c1m[-1]['close']:.{digits}f} · 15M {ctx['t15']['state']} · 5M {ctx['m5']['bias']}\n"
                                  + html.escape(why[:300]) + "\n(Status — bot is alive.)", ts)
        except Exception as exc:
            print(f"[{name}] ERROR: {exc}")
            n = state.get(f"err:{name}", 0) + 1
            state[f"err:{name}"] = n
            if n == 3:
                notify(f"🛑 <b>{name}</b> — 3 scans failed in a row.\nLast error: {html.escape(str(exc)[:300])}")

    _flush_api(state, day0)
    # credit pacing: spread the remaining daily credits over the rest of the session
    left = TWELVE_DAILY_LIMIT * 0.95 - state.get(f"api:{day0}", 0)
    if TWELVE_DAILY_LIMIT > 0 and td_in_session and secs_left > 0:
        scans_possible = max(left / td_in_session, 1)
        _PACE["sec"] = int(min(max(SCAN_INTERVAL_SEC, secs_left / scans_possible), 600))
    else:
        _PACE["sec"] = SCAN_INTERVAL_SEC
    save_state(state)


def _maybe_status(state, name, text, ts):
    if not SEND_STATUS_UPDATES:
        return
    k = f"status:{name}"
    v = ts.strftime("%Y-%m-%d") + f" b{ts.hour // STATUS_EVERY_HOURS}"
    if state.get(k) != v:
        notify(text)
        state[k] = v


def run_loop():
    cache = {}
    deadline = now_utc() + dt.timedelta(minutes=LOOP_MINUTES)
    print(f"[LOOP] running until {deadline.strftime('%H:%M:%S')} UTC")
    while True:
        try:
            run_once(cache)
        except Exception as exc:
            print(f"[LOOP] error: {exc}")
        ts = now_utc()
        if ts >= deadline:
            break
        if ts.hour >= 21 and ts.hour < 24 and not any(not t["shadow"] for t in load_state()["open_trades"]):
            print("[LOOP] trading day finished")
            break
        nxt = (int((ts.timestamp() + _PACE["sec"]) // 60) + 1) * 60 + 8     # just after a 1M candle closed
        time.sleep(max(nxt - time.time(), 5))
    print("[LOOP] finished")


# ============================================================ backtest
def run_backtest(path, name="XAUUSD"):
    global OFFLINE
    OFFLINE = True
    cfg = sym_cfg(name)
    candles = load_csv(path)
    W = FETCH_BARS
    if len(candles) < W + 100:
        raise RuntimeError(f"Need at least {W + 100} one-minute candles, got {len(candles)}")
    state = _empty_state()
    cache = {}
    t_start = time.time()
    print(f"[BACKTEST] {name}: {len(candles)} candles {candles[0]['time']} -> {candles[-1]['time']}")
    for i in range(W, len(candles)):
        c = candles[i]
        ts = c["dt"] + dt.timedelta(minutes=1)
        if candles[i]["dt"] - candles[i - 1]["dt"] > dt.timedelta(minutes=30):     # market gap: drop stale trades
            cache.pop(name, None)
        if state["open_trades"]:
            track_trades(state, name, [c], cfg["digits"], ts)
        if state["post_sl"]:
            check_post_sl(state, name, [c], ts)
        if gate_reason(state, name, cfg, ts):
            continue
        window = candles[i - W + 1:i + 1]
        if (window[-1]["dt"] - window[0]["dt"]) > dt.timedelta(minutes=W * 3):     # too many gaps in the window
            continue
        res = scan_symbol(state, name, cfg, window, ts, cache)
        if res["status"] == "shadow":
            pass
    # close what is still open at the end
    for t in list(state["open_trades"]):
        close_trade(state, t, ("EXPIRED", _r_of(t, t["last_close"]), t["last_close"]), candles[-1]["dt"])
    real = [x for x in state["closed_trades"] if not x.get("shadow")]
    shadow = [x for x in state["closed_trades"] if x.get("shadow")]
    plain = lambda s: s.replace("<b>", "").replace("</b>", "")
    print(f"\n[BACKTEST] done in {time.time() - t_start:.0f}s  (spread {cfg['spread']:g}, MIN_RR {MIN_RR:g}, MIN_SCORE {cfg['min_score']})")
    print("\n".join(plain(x) for x in _stat_lines(real)))
    if real:
        print(f"Max drawdown {_max_dd(real):.2f}R")
        for title, key in (("Session", "session"), ("Grade", "grade"), ("Zone TF", "zone_tf"), ("Side", "side")):
            print(f"\nBy {title}:")
            print("\n".join("  " + x for x in _group_lines(real, key)))
        tags = {}
        for x in real:
            for tg in x.get("tags", []):
                tags[tg] = tags.get(tg, 0) + 1
        if tags:
            print("\nLoss tags: " + ", ".join(f"{k} {v}" for k, v in sorted(tags.items(), key=lambda kv: -kv[1])))
    if shadow:
        print("\nShadow (blocked by learning): " + plain(_stat_lines(shadow)[0]))
    out = Path(f"backtest_trades_{name}.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["opened", "closed", "side", "result", "r", "score", "grade", "session", "zone_tf", "zone_grade", "tags", "shadow"])
        for x in state["closed_trades"]:
            w.writerow([x["opened"], x["closed"], x["side"], x["result"], x["r"], x["score"], x["grade"],
                        x["session"], x["zone_tf"], x["zone_grade"], "|".join(x.get("tags", [])), int(bool(x.get("shadow")))])
    print(f"\nTrade list saved to {out}")
    return state


# ============================================================ main
def main():
    args = sys.argv[1:]
    if "--backtest" in args:
        i = args.index("--backtest")
        path = args[i + 1]
        name = args[i + 2].upper() if len(args) > i + 2 else "XAUUSD"
        run_backtest(path, name)
        return
    require_credentials()
    if "--test" in args or os.environ.get("TEST_TELEGRAM") == "1":
        send_telegram(f"✅ <b>Smart Scalper v{VERSION}</b>\nTelegram connection works.\nTime: {now_utc().strftime('%Y-%m-%d %H:%M')} UTC")
        print("Test message sent")
        return
    if "--report" in args or os.environ.get("SEND_REPORT_NOW") == "1":
        st, ts = load_state(), now_utc()
        send_telegram(build_report(st, ts, 7, "Report (last 7 days)"))
        send_telegram(build_report(st, ts, 30, "30-day report"))
        print("Report sent")
        return
    if LOOP_MINUTES > 0:
        run_loop()
    else:
        run_once({})


if __name__ == "__main__":
    main()
