#!/usr/bin/env python3
"""
SMC DAY-TRADING ASSISTANT v1.0
================================
Signals only: the bot NEVER places broker orders.

Architecture
------------
4H/1H context -> 30M structure/liquidity -> 15M SMC setup
-> 5M confirmation -> risk/target engine -> Telegram -> outcome tracker

Built around the user's previous ICT/SMC readers, but rewritten as one
coherent engine instead of an enhancement layer around a legacy signal.

Requirements
------------
pip install requests

Environment
-----------
TWELVE_DATA_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

Optional
--------
SMC_SCAN_SECONDS=60
SMC_MAX_SIGNALS_DAY=4
SMC_MAX_SYMBOL_SIGNALS_DAY=2
SMC_MIN_SCORE=8
SMC_SESSION_FILTER=1
SMC_NEWS_FILE=news_events.json
SMC_STATE_FILE=smc_day_trading_state.json
SMC_TRADE_LOG=smc_day_trading_trades.jsonl
SMC_RUN_FOREVER=1

The bot uses closed candles only. A setup must be fresh enough to be useful;
old signals are rejected rather than sent late.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============================================================================
# CONFIG
# ============================================================================

TD_URL = "https://api.twelvedata.com/time_series"
TG_URL = "https://api.telegram.org/bot{token}/sendMessage"

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SYMBOLS = {
    "XAUUSD": {
        "td_symbol": "XAU/USD",
        "digits": 3,
        "spread": 0.30,
        "slippage": 0.10,
        "session_filter": True,
    },
    "BTCUSD": {
        "td_symbol": "BTC/USD",
        "digits": 3,
        "spread": 8.0,
        "slippage": 3.0,
        "session_filter": False,
    },
}

SCAN_SECONDS = int(os.getenv("SMC_SCAN_SECONDS", "60"))
MAX_SIGNALS_PER_DAY = int(os.getenv("SMC_MAX_SIGNALS_DAY", "4"))
MAX_SIGNALS_PER_SYMBOL_DAY = int(os.getenv("SMC_MAX_SYMBOL_SIGNALS_DAY", "2"))
MIN_SCORE = int(os.getenv("SMC_MIN_SCORE", "8"))
MAX_SCORE = 12

# Session times are UTC. XAU uses the filter; BTC can run all day by default.
LONDON = (7, 11)
NEW_YORK = (12, 17)

# Higher-level news file: JSON list of {"time":"2026-10-06T12:30:00+00:00",
# "name":"US CPI", "impact":"high"}. Unknown/invalid events are ignored.
NEWS_FILE = Path(os.getenv("SMC_NEWS_FILE", "news_events.json"))
NEWS_WINDOW_MIN = int(os.getenv("SMC_NEWS_WINDOW_MIN", "30"))
NEWS_FILTER = os.getenv("SMC_NEWS_FILTER", "1") == "1"

# Market structure / SMC
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
ATR_PERIOD = 14
MSS_MAX_BARS = 8
SWEEP_MAX_BARS = 8
DISPLACEMENT_BODY_RATIO = 0.65
DISPLACEMENT_ATR = 1.05
FVG_MIN_ATR = 0.08

# Risk: tight but not absurdly tight.
SL_BUFFER_ATR = 0.12
MIN_SL_ATR = 0.45
MAX_SL_ATR = 2.60
MIN_RR_TP1 = 1.80
MIN_RR_TP2 = 2.80
MAX_TARGET_R = 8.0
MAX_ENTRY_DISTANCE_ATR = 1.50

# Signal freshness / execution assumptions.
MAX_SIGNAL_AGE_MIN = 9
ENTRY_RETEST_MAX_MIN = 45
MIN_RISK_TO_SPREAD = 2.0

# Confirmation.
EMA_FAST = 9
EMA_SLOW = 15

# Outcome management. This is tracking/advice only, not order execution.
TP1_R = 1.0
TP1_CLOSE = 0.35
TP2_CLOSE = 0.40
TRAIL_ATR = 1.0
SIGNAL_EXPIRY_HOURS = 6

STATE_FILE = Path(os.getenv("SMC_STATE_FILE", "smc_day_trading_state.json"))
TRADE_LOG = Path(os.getenv("SMC_TRADE_LOG", "smc_day_trading_trades.jsonl"))

RUN_FOREVER = os.getenv("SMC_RUN_FOREVER", "1") == "1"

# API behavior
REQUEST_TIMEOUT = 20
API_RETRIES = 4
API_BACKOFF = 1.6
MIN_BARS = {"4h": 80, "1h": 120, "30min": 180, "15min": 220, "5min": 220}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("smc-assistant")


# ============================================================================
# BASIC HELPERS
# ============================================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(s: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def env_ok() -> None:
    missing = []
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def fmt(p: float, symbol: str) -> str:
    return f"{p:.{SYMBOLS[symbol]['digits']}f}"


def side_sign(side: str) -> int:
    return 1 if side == "BUY" else -1


def mid(a: float, b: float) -> float:
    return (a + b) / 2.0


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ============================================================================
# TELEGRAM
# ============================================================================

def send_telegram(text: str) -> bool:
    try:
        r = requests.post(
            TG_URL.format(token=TELEGRAM_BOT_TOKEN),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:
        log.error("Telegram error: %s", exc)
        return False


# ============================================================================
# TWELVE DATA
# ============================================================================

def get_candles(symbol: str, interval: str, outputsize: int) -> List[Dict[str, Any]]:
    """Return oldest -> newest closed candles only."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    }
    last_error = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            r = requests.get(TD_URL, params=params, timeout=REQUEST_TIMEOUT)
            data = r.json()
            if r.status_code == 429 or data.get("code") == 429:
                wait = 20 if attempt == API_RETRIES else API_BACKOFF ** attempt * 5
                log.warning("Twelve Data rate limit; sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if "values" not in data:
                raise RuntimeError(data.get("message", "Twelve Data returned no values"))
            rows = []
            for x in data["values"]:
                try:
                    rows.append({
                        "time": parse_time(x["datetime"]),
                        "open": float(x["open"]),
                        "high": float(x["high"]),
                        "low": float(x["low"]),
                        "close": float(x["close"]),
                    })
                except Exception:
                    continue
            rows = [x for x in rows if x["time"] is not None]
            rows.sort(key=lambda x: x["time"])
            # Twelve Data can return a currently forming bar. Closed-candle rule.
            if rows:
                now = utcnow()
                interval_min = {
                    "5min": 5,
                    "15min": 15,
                    "30min": 30,
                    "1h": 60,
                    "4h": 240,
                }.get(interval, 15)
                last = rows[-1]["time"]
                if (now - last).total_seconds() < interval_min * 60:
                    rows.pop()
            return rows
        except Exception as exc:
            last_error = exc
            if attempt < API_RETRIES:
                time.sleep(API_BACKOFF ** attempt)
    raise RuntimeError(f"get_candles failed: {symbol} {interval}: {last_error}")


def load_market(symbol: str) -> Dict[str, List[Dict[str, Any]]]:
    td_symbol = SYMBOLS[symbol]["td_symbol"]
    out = {}
    for tf, n in MIN_BARS.items():
        out[tf] = get_candles(td_symbol, tf, n)
        if len(out[tf]) < 50:
            raise RuntimeError(f"Not enough {tf} candles for {symbol}")
    return out


# ============================================================================
# INDICATORS / STRUCTURE
# ============================================================================

def true_range(candles: List[Dict[str, Any]]) -> List[float]:
    out = []
    prev = None
    for c in candles:
        if prev is None:
            tr = c["high"] - c["low"]
        else:
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev),
                abs(c["low"] - prev),
            )
        out.append(tr)
        prev = c["close"]
    return out


def atr(candles: List[Dict[str, Any]], n: int = ATR_PERIOD) -> float:
    tr = true_range(candles)
    if len(tr) < n:
        return 0.0
    return sum(tr[-n:]) / n


def ema(values: List[float], n: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def body(c: Dict[str, Any]) -> float:
    return abs(c["close"] - c["open"])


def avg_body(candles: List[Dict[str, Any]], n: int = 20) -> float:
    xs = [body(c) for c in candles[-n:]]
    return sum(xs) / len(xs) if xs else 0.0


def bullish(c: Dict[str, Any]) -> bool:
    return c["close"] > c["open"]


def bearish(c: Dict[str, Any]) -> bool:
    return c["close"] < c["open"]


def swings(candles: List[Dict[str, Any]], left: int = PIVOT_LEFT,
           right: int = PIVOT_RIGHT) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if h > max(c["high"] for c in candles[i-left:i]) and h >= max(c["high"] for c in candles[i+1:i+right+1]):
            highs.append((i, h))
        if l < min(c["low"] for c in candles[i-left:i]) and l <= min(c["low"] for c in candles[i+1:i+right+1]):
            lows.append((i, l))
    return highs, lows


def structure_bias(candles: List[Dict[str, Any]]) -> int:
    sh, sl = swings(candles)
    if len(sh) < 2 or len(sl) < 2:
        return 0
    hh = sh[-1][1] > sh[-2][1]
    hl = sl[-1][1] > sl[-2][1]
    lh = sh[-1][1] < sh[-2][1]
    ll = sl[-1][1] < sl[-2][1]
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


def bias_name(b: int) -> str:
    return {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[b]


def premium_discount(h1: List[Dict[str, Any]]) -> Tuple[str, float, float, float]:
    look = h1[-72:]
    hi = max(c["high"] for c in look)
    lo = min(c["low"] for c in look)
    eq = mid(hi, lo)
    price = h1[-1]["close"]
    return ("DISCOUNT" if price < eq else "PREMIUM", lo, hi, eq)


# ============================================================================
# LIQUIDITY / SMC
# ============================================================================

def previous_day_levels(h1: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    now = h1[-1]["time"]
    today = now.date()
    prev = [c for c in h1 if c["time"].date() < today]
    if not prev:
        return None, None
    d = prev[-1]["time"].date()
    xs = [c for c in prev if c["time"].date() == d]
    if not xs:
        return None, None
    return max(c["high"] for c in xs), min(c["low"] for c in xs)


def equal_levels(values: List[float], tolerance: float) -> List[float]:
    vals = sorted(values)
    groups: List[List[float]] = []
    for v in vals:
        if not groups or abs(v - sum(groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([v])
        else:
            groups[-1].append(v)
    return [sum(g) / len(g) for g in groups if len(g) >= 2]


def liquidity_levels(m30: List[Dict[str, Any]], atr30: float,
                     pdh: Optional[float], pdl: Optional[float]) -> Dict[str, List[float]]:
    sh, sl = swings(m30)
    highs = [p for _, p in sh[-40:]]
    lows = [p for _, p in sl[-40:]]
    tol = max(atr30 * 0.15, 1e-9)
    out = {
        "buy_side": highs + ([pdh] if pdh is not None else []) + equal_levels(highs, tol),
        "sell_side": lows + ([pdl] if pdl is not None else []) + equal_levels(lows, tol),
    }
    return out


def detect_sweep(m15: List[Dict[str, Any]], levels: Dict[str, List[float]],
                 side: str, atr15: float) -> Optional[Dict[str, Any]]:
    """BUY needs sell-side sweep; SELL needs buy-side sweep."""
    candidates = levels["sell_side"] if side == "BUY" else levels["buy_side"]
    if not candidates:
        return None
    start = max(1, len(m15) - SWEEP_MAX_BARS)
    for i in range(len(m15) - 1, start - 1, -1):
        c = m15[i]
        for level in candidates:
            depth = (level - c["low"]) if side == "BUY" else (c["high"] - level)
            if depth < 0.02 * atr15 or depth > 1.50 * atr15:
                continue
            if side == "BUY" and c["low"] < level and c["close"] > level:
                return {"index": i, "time": c["time"], "level": level, "wick": c["low"], "kind": "sell-side"}
            if side == "SELL" and c["high"] > level and c["close"] < level:
                return {"index": i, "time": c["time"], "level": level, "wick": c["high"], "kind": "buy-side"}
    return None


def displacement(c: Dict[str, Any], atr15: float, side: str,
                  candles: List[Dict[str, Any]]) -> bool:
    if body(c) < DISPLACEMENT_ATR * atr15:
        return False
    ab = avg_body(candles[:-1], 20) if len(candles) > 1 else 0
    if ab and body(c) < DISPLACEMENT_BODY_RATIO * 1.5 * ab:
        return False
    return bullish(c) if side == "BUY" else bearish(c)


def find_mss(m15: List[Dict[str, Any]], sweep: Dict[str, Any],
             side: str, atr15: float) -> Optional[Dict[str, Any]]:
    k = sweep["index"]
    if k >= len(m15) - 1:
        return None
    before = m15[:k]
    sh, sl = swings(before)
    if side == "BUY":
        if not sl or not sh:
            return None
        ref_idx, ref = sh[-1]
        for j in range(k + 1, min(len(m15), k + MSS_MAX_BARS + 1)):
            c = m15[j]
            if c["close"] > ref and displacement(c, atr15, side, m15[max(0, j-30):j+1]):
                return {"index": j, "time": c["time"], "level": ref, "type": "MSS/BOS"}
    else:
        if not sh or not sl:
            return None
        ref_idx, ref = sl[-1]
        for j in range(k + 1, min(len(m15), k + MSS_MAX_BARS + 1)):
            c = m15[j]
            if c["close"] < ref and displacement(c, atr15, side, m15[max(0, j-30):j+1]):
                return {"index": j, "time": c["time"], "level": ref, "type": "MSS/BOS"}
    return None


def find_fvg(m15: List[Dict[str, Any]], mss_idx: int, side: str,
             atr15: float) -> Optional[Dict[str, Any]]:
    # Standard 3-candle imbalance around the displacement candle.
    for i in range(max(2, mss_idx - 2), min(len(m15) - 2, mss_idx + 2) + 1):
        a, b, c = m15[i-1], m15[i], m15[i+1]
        if side == "BUY" and c["low"] > a["high"]:
            lo, hi = a["high"], c["low"]
            if hi - lo >= FVG_MIN_ATR * atr15:
                return {"low": lo, "high": hi, "mid": mid(lo, hi), "time": b["time"], "type": "FVG"}
        if side == "SELL" and c["high"] < a["low"]:
            lo, hi = c["high"], a["low"]
            if hi - lo >= FVG_MIN_ATR * atr15:
                return {"low": lo, "high": hi, "mid": mid(lo, hi), "time": b["time"], "type": "FVG"}
    return None


def find_ob(m15: List[Dict[str, Any]], mss_idx: int, side: str,
            atr15: float) -> Optional[Dict[str, Any]]:
    start = max(0, mss_idx - 6)
    for i in range(mss_idx - 1, start - 1, -1):
        c = m15[i]
        if side == "BUY" and bearish(c):
            return {"low": c["low"], "high": c["high"], "mid": mid(c["low"], c["high"]), "time": c["time"], "type": "Bullish OB"}
        if side == "SELL" and bullish(c):
            return {"low": c["low"], "high": c["high"], "mid": mid(c["low"], c["high"]), "time": c["time"], "type": "Bearish OB"}
    return None


def zone_entry(fvg: Optional[Dict[str, Any]], ob: Optional[Dict[str, Any]],
               side: str, current: float) -> Tuple[float, str]:
    zones = []
    if fvg:
        zones.append((fvg["mid"], "FVG 50%"))
    if ob:
        zones.append((ob["mid"], "Order Block 50%"))
    if not zones:
        return current, "market"
    # Prefer the deepest valid retracement, but avoid an entry too far from price.
    if side == "BUY":
        zones = sorted(zones, key=lambda x: x[0], reverse=True)
    else:
        zones = sorted(zones, key=lambda x: x[0])
    return zones[0]


# ============================================================================
# 5M CONFIRMATION
# ============================================================================

def five_min_confirmation(m5: List[Dict[str, Any]], side: str) -> Dict[str, Any]:
    if len(m5) < 30:
        return {"ok": False, "score": 0, "reason": "insufficient 5M data"}
    closes = [c["close"] for c in m5]
    e9 = ema(closes, EMA_FAST)
    e15 = ema(closes, EMA_SLOW)
    a = atr(m5)
    last = m5[-1]
    recent = m5[-4:]
    disp = body(last) >= 0.9 * a and (bullish(last) if side == "BUY" else bearish(last))
    trend = e9 > e15 if side == "BUY" else e9 < e15
    # Rejection of the intended direction: long lower wick for BUY, upper wick for SELL.
    if side == "BUY":
        wick = min(last["open"], last["close"]) - last["low"]
        rejection = wick >= body(last) * 0.7
    else:
        wick = last["high"] - max(last["open"], last["close"])
        rejection = wick >= body(last) * 0.7
    score = int(trend) + int(disp) + int(rejection)
    return {
        "ok": score >= 2,
        "score": score,
        "ema9": e9,
        "ema15": e15,
        "displacement": disp,
        "rejection": rejection,
        "time": last["time"],
    }


# ============================================================================
# TARGET / RISK ENGINE
# ============================================================================

def candidate_targets(m30: List[Dict[str, Any]], h1: List[Dict[str, Any]],
                       side: str, entry: float, risk: float,
                       pdh: Optional[float], pdl: Optional[float]) -> List[float]:
    sh30, sl30 = swings(m30)
    sh1, sl1 = swings(h1)
    raw = []
    if side == "BUY":
        raw += [p for _, p in sh30[-30:] if p > entry]
        raw += [p for _, p in sh1[-20:] if p > entry]
        if pdh and pdh > entry: raw.append(pdh)
        raw.append(max(c["high"] for c in h1[-72:]))
        vals = sorted(set(raw))
    else:
        raw += [p for _, p in sl30[-30:] if p < entry]
        raw += [p for _, p in sl1[-20:] if p < entry]
        if pdl and pdl < entry: raw.append(pdl)
        raw.append(min(c["low"] for c in h1[-72:]))
        vals = sorted(set(raw), reverse=True)
    return [p for p in vals if MIN_RR_TP2 <= abs(p-entry)/risk <= MAX_TARGET_R]


def make_plan(symbol: str, side: str, current: float, m15: List[Dict[str, Any]],
              m30: List[Dict[str, Any]], h1: List[Dict[str, Any]],
              sweep: Dict[str, Any], fvg: Optional[Dict[str, Any]],
              ob: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    a = atr(m15)
    if a <= 0:
        return None
    entry, entry_type = zone_entry(fvg, ob, side, current)
    if abs(entry-current) > MAX_ENTRY_DISTANCE_ATR * a:
        return None

    pdh, pdl = previous_day_levels(h1)
    if side == "BUY":
        structure_sl = min(sweep["wick"], ob["low"] if ob else sweep["wick"]) - SL_BUFFER_ATR*a
        risk = entry - structure_sl
    else:
        structure_sl = max(sweep["wick"], ob["high"] if ob else sweep["wick"]) + SL_BUFFER_ATR*a
        risk = structure_sl - entry
    if risk <= 0:
        return None
    risk_atr = risk / a
    if not (MIN_SL_ATR <= risk_atr <= MAX_SL_ATR):
        return None
    spread = SYMBOLS[symbol]["spread"]
    if risk < spread * MIN_RISK_TO_SPREAD:
        return None

    targets = candidate_targets(m30, h1, side, entry, risk, pdh, pdl)
    if not targets:
        return None
    tp2 = targets[-1]
    # TP1 is the first opposing liquidity that is at least 1.8R.
    tp1_candidates = [x for x in targets if abs(x-entry)/risk >= MIN_RR_TP1]
    if not tp1_candidates:
        return None
    tp1 = tp1_candidates[0]
    rr1 = abs(tp1-entry)/risk
    rr2 = abs(tp2-entry)/risk
    return {
        "entry": entry,
        "entry_type": entry_type,
        "sl": structure_sl,
        "risk": risk,
        "risk_atr": risk_atr,
        "tp1": tp1,
        "tp2": tp2,
        "rr1": rr1,
        "rr2": rr2,
        "atr15": a,
        "pdh": pdh,
        "pdl": pdl,
    }


# ============================================================================
# NEWS / SESSION
# ============================================================================

def session_name(now: datetime) -> str:
    h = now.hour + now.minute / 60
    if LONDON[0] <= h < LONDON[1]:
        return "LONDON"
    if NEW_YORK[0] <= h < NEW_YORK[1]:
        return "NEW YORK"
    return "OUTSIDE KILLZONE"


def news_blocked(now: datetime) -> Optional[str]:
    if not NEWS_FILTER or not NEWS_FILE.exists():
        return None
    try:
        events = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Cannot read news file: %s", exc)
        return None
    for e in events if isinstance(events, list) else []:
        if str(e.get("impact", "")).lower() != "high":
            continue
        t = parse_time(str(e.get("time", "")))
        if not t:
            continue
        if abs((now-t).total_seconds()) <= NEWS_WINDOW_MIN*60:
            return str(e.get("name", "high-impact news"))
    return None


# ============================================================================
# SCORING / SETUP DETECTION
# ============================================================================

def detect_setup(symbol: str, d: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    h4, h1, m30, m15, m5 = d["4h"], d["1h"], d["30min"], d["15min"], d["5min"]
    a15 = atr(m15)
    a30 = atr(m30)
    if not a15 or not a30:
        return []
    b4 = structure_bias(h4)
    b1 = structure_bias(h1)
    b30 = structure_bias(m30)
    pd_zone, pd_lo, pd_hi, eq = premium_discount(h1)
    pdh, pdl = previous_day_levels(h1)
    levels = liquidity_levels(m30, a30, pdh, pdl)
    current = m15[-1]["close"]
    out = []

    for side in ("BUY", "SELL"):
        directional = 1 if side == "BUY" else -1
        if b4 not in (0, directional):
            continue
        if b1 not in (0, directional):
            continue
        # Location filter: BUY in discount, SELL in premium.
        if side == "BUY" and pd_zone != "DISCOUNT":
            continue
        if side == "SELL" and pd_zone != "PREMIUM":
            continue

        sweep = detect_sweep(m15, levels, side, a15)
        if not sweep:
            continue
        mss = find_mss(m15, sweep, side, a15)
        if not mss:
            continue
        fvg = find_fvg(m15, mss["index"], side, a15)
        ob = find_ob(m15, mss["index"], side, a15)
        if not fvg and not ob:
            continue

        plan = make_plan(symbol, side, current, m15, m30, h1, sweep, fvg, ob)
        if not plan:
            continue
        conf = five_min_confirmation(m5, side)
        if not conf["ok"]:
            continue

        score = 0
        reasons = []
        # Context: max 5
        if b4 == directional: score += 2; reasons.append(f"4H {bias_name(b4)} bias")
        elif b4 == 0: score += 1; reasons.append("4H neutral / compatible")
        if b30 == directional: score += 1; reasons.append(f"30M {bias_name(b30)} structure")
        if (side == "BUY" and pd_zone == "DISCOUNT") or (side == "SELL" and pd_zone == "PREMIUM"):
            score += 1; reasons.append(pd_zone.title() + " location")
        if (side == "BUY" and pdl and abs(sweep["level"]-pdl) <= 0.20*a30) or (side == "SELL" and pdh and abs(sweep["level"]-pdh) <= 0.20*a30):
            score += 1; reasons.append("Previous-day liquidity interaction")
        # Trigger: max 5
        score += 2; reasons.append(f"{sweep['kind']} liquidity sweep")
        score += 1; reasons.append("MSS/BOS with displacement")
        if fvg and ob: score += 1; reasons.append("FVG + Order Block confluence")
        elif fvg: score += 1; reasons.append("Fair Value Gap entry zone")
        elif ob: score += 1; reasons.append("Order Block entry zone")
        # Confirmation: max 2
        if conf["ema9"] > conf["ema15"] if side == "BUY" else conf["ema9"] < conf["ema15"]:
            score += 1; reasons.append("5M EMA alignment")
        if conf["displacement"] or conf["rejection"]:
            score += 1; reasons.append("5M trigger confirmation")

        if plan["rr2"] >= 4.0:
            reasons.append(f"Liquidity target offers {plan['rr2']:.1f}R")

        if score < MIN_SCORE:
            continue

        mss_age = (utcnow() - mss["time"]).total_seconds() / 60
        if mss_age < 0 or mss_age > MAX_SIGNAL_AGE_MIN:
            continue

        out.append({
            "symbol": symbol,
            "side": side,
            "score": score,
            "reasons": reasons,
            "bias4h": b4,
            "bias1h": b1,
            "bias30m": b30,
            "pd_zone": pd_zone,
            "sweep": sweep,
            "mss": mss,
            "fvg": fvg,
            "ob": ob,
            "confirmation": conf,
            "plan": plan,
            "signal_time": mss["time"],
            "session": session_name(utcnow()),
        })
    return out


# ============================================================================
# STATE / OUTCOME TRACKING
# ============================================================================

def default_state() -> Dict[str, Any]:
    return {"signals": [], "last_candle": {}, "last_daily_report": ""}


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else default_state()
    except Exception:
        return default_state()


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def log_trade(event: Dict[str, Any]) -> None:
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def today_signals(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    today = utcnow().date().isoformat()
    return [x for x in state["signals"] if str(x.get("day")) == today]


def signal_key(s: Dict[str, Any]) -> str:
    return f"{s['symbol']}|{s['side']}|{s['signal_time'].isoformat()}"


def update_outcomes(state: Dict[str, Any], market_cache: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
    now = utcnow()
    changed = False
    for t in state["signals"]:
        if t.get("status") in ("CLOSED", "CANCELLED"):
            continue
        sym = t["symbol"]
        if sym not in market_cache:
            continue
        m5 = market_cache[sym]["5min"]
        if not m5:
            continue
        side = t["side"]
        entry = t["entry"]
        sl = t["sl"]
        tp1 = t["tp1"]
        tp2 = t["tp2"]
        status = t.get("status", "PENDING")
        filled = t.get("filled", False)
        entry_time = parse_time(t["sent_at"])

        for c in m5:
            ct = c["time"]
            if entry_time and ct <= entry_time:
                continue
            if not filled:
                touched = c["low"] <= entry <= c["high"]
                if touched:
                    filled = True
                    t["filled"] = True
                    t["filled_at"] = ct.isoformat()
                    t["status"] = "OPEN"
                    status = "OPEN"
                    changed = True
                elif entry_time and now - entry_time > timedelta(hours=SIGNAL_EXPIRY_HOURS):
                    t["status"] = "CANCELLED"
                    t["closed_at"] = now.isoformat()
                    changed = True
                    break
            if not filled:
                continue

            # Conservative intrabar ordering: if both SL and TP are touched in
            # one candle, assume SL first. This avoids optimistic backfill.
            if side == "BUY":
                hit_sl = c["low"] <= sl
                hit_tp1 = c["high"] >= tp1
                hit_tp2 = c["high"] >= tp2
            else:
                hit_sl = c["high"] >= sl
                hit_tp1 = c["low"] <= tp1
                hit_tp2 = c["low"] <= tp2

            if hit_sl and status != "TP1":
                t["status"] = "CLOSED"
                t["result_r"] = -1.0
                t["closed_at"] = ct.isoformat()
                t["outcome"] = "SL"
                log_trade({"event":"CLOSE", **t})
                changed = True
                break
            if status == "OPEN" and hit_tp1:
                t["status"] = "TP1"
                t["tp1_at"] = ct.isoformat()
                t["partial_r"] = TP1_R * TP1_CLOSE
                t["sl_after_tp1"] = entry
                status = "TP1"
                changed = True
                log_trade({"event":"TP1", **t})
                # Do not break: later candles can reach TP2.
            if status == "TP1":
                if hit_tp2:
                    t["status"] = "CLOSED"
                    t["result_r"] = TP1_R * TP1_CLOSE + t.get("runner_r", 0.0)
                    t["runner_r"] = (abs(tp2-entry)/abs(entry-sl)) * (1-TP1_CLOSE)
                    t["result_r"] = TP1_R*TP1_CLOSE + t["runner_r"]
                    t["closed_at"] = ct.isoformat()
                    t["outcome"] = "TP2"
                    log_trade({"event":"CLOSE", **t})
                    changed = True
                    break
                # After TP1 the advised SL is breakeven; conservative tracker.
                be_hit = c["low"] <= entry if side == "BUY" else c["high"] >= entry
                if be_hit and not hit_tp2:
                    t["status"] = "CLOSED"
                    t["result_r"] = TP1_R * TP1_CLOSE
                    t["closed_at"] = ct.isoformat()
                    t["outcome"] = "BE_AFTER_TP1"
                    log_trade({"event":"CLOSE", **t})
                    changed = True
                    break
    if changed:
        save_state(state)


# ============================================================================
# TELEGRAM MESSAGES
# ============================================================================

def signal_message(s: Dict[str, Any], number: int) -> str:
    p = s["plan"]
    emoji = "🟢" if s["side"] == "BUY" else "🔴"
    why = "\n".join("• " + x for x in s["reasons"])
    return (
        f"{emoji} <b>SMC {s['side']} — {s['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Quality:</b> {s['score']}/{MAX_SCORE} | Signal {number}/{MAX_SIGNALS_PER_DAY}\n"
        f"<b>Session:</b> {s['session']}\n"
        f"<b>4H / 1H / 30M:</b> {bias_name(s['bias4h'])} / {bias_name(s['bias1h'])} / {bias_name(s['bias30m'])}\n"
        f"<b>Location:</b> {s['pd_zone']}\n\n"
        f"<b>Entry zone:</b> {fmt(p['entry'], s['symbol'])} ({p['entry_type']})\n"
        f"<b>SL:</b> {fmt(p['sl'], s['symbol'])} | risk {p['risk_atr']:.2f} ATR\n"
        f"<b>TP1:</b> {fmt(p['tp1'], s['symbol'])} | RR 1:{p['rr1']:.1f}\n"
        f"<b>TP2:</b> {fmt(p['tp2'], s['symbol'])} | RR 1:{p['rr2']:.1f}\n\n"
        f"<b>SMC trigger</b>\n{why}\n\n"
        f"<b>5M:</b> EMA {'✓' if ((s['side']=='BUY' and s['confirmation']['ema9'] > s['confirmation']['ema15']) or (s['side']=='SELL' and s['confirmation']['ema9'] < s['confirmation']['ema15'])) else '✗'} | "
        f"Displacement {'✓' if s['confirmation']['displacement'] else '✗'} | "
        f"Rejection {'✓' if s['confirmation']['rejection'] else '✗'}\n\n"
        f"<b>MANUAL ACTION:</b> Check the setup yourself. This bot does not place orders.\n"
        f"TP1 plan: partial profit + move SL to entry. Let runner seek TP2."
    )


def daily_report(state: Dict[str, Any], cache: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> str:
    today = utcnow().date().isoformat()
    rows = [x for x in state["signals"] if x.get("day") == today]
    closed = [x for x in state["signals"] if x.get("status") == "CLOSED" and x.get("closed_at", "").startswith(today)]
    rsum = sum(float(x.get("result_r", 0)) for x in closed)
    lines = [f"📊 <b>SMC DAILY REPORT</b> — {today}", f"Signals: {len(rows)}/{MAX_SIGNALS_PER_DAY}", f"Closed R: {rsum:+.2f}R"]
    for sym, d in cache.items():
        lines.append(f"• {sym}: 4H {bias_name(structure_bias(d['4h']))} | 1H {bias_name(structure_bias(d['1h']))}")
    for x in rows:
        extra = f" {x.get('result_r', 0):+.2f}R" if 'result_r' in x else ''
        lines.append(f"• {x['symbol']} {x['side']} — {x['status']}{extra}")
    return "\n".join(lines)


# ============================================================================
# MAIN CYCLE
# ============================================================================

def scan_once(state: Dict[str, Any]) -> None:
    now = utcnow()
    cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for symbol in SYMBOLS:
        try:
            cache[symbol] = load_market(symbol)
        except Exception as exc:
            log.error("%s data error: %s", symbol, exc)

    if cache:
        update_outcomes(state, cache)

    # Only evaluate a symbol when a new closed 5M candle is available.
    for symbol, d in cache.items():
        last5 = d["5min"][-1]["time"].isoformat()
        if state["last_candle"].get(symbol) == last5:
            continue
        state["last_candle"][symbol] = last5

        cfg = SYMBOLS[symbol]
        if cfg["session_filter"]:
            h = now.hour + now.minute/60
            if not (LONDON[0] <= h < LONDON[1] or NEW_YORK[0] <= h < NEW_YORK[1]):
                continue
        nb = news_blocked(now)
        if nb:
            log.info("%s blocked by news: %s", symbol, nb)
            continue

        todays = today_signals(state)
        if len(todays) >= MAX_SIGNALS_PER_DAY:
            continue
        if sum(1 for x in todays if x["symbol"] == symbol) >= MAX_SIGNALS_PER_SYMBOL_DAY:
            continue
        active = {x["symbol"] for x in state["signals"] if x.get("status") in ("PENDING", "OPEN", "TP1")}
        if symbol in active:
            continue

        try:
            setups = detect_setup(symbol, d)
        except Exception:
            log.error("%s setup error\n%s", symbol, traceback.format_exc())
            continue
        if not setups:
            continue
        setups.sort(key=lambda x: (x["score"], x["plan"]["rr2"]), reverse=True)
        s = setups[0]

        # Execution freshness: the 5M trigger is checked at the latest closed candle,
        # and the MSS must still be recent.
        age = (now - s["signal_time"]).total_seconds()/60
        if age < 0 or age > MAX_SIGNAL_AGE_MIN:
            continue
        key = signal_key(s)
        if any(x.get("id") == key for x in state["signals"]):
            continue

        number = len(todays) + 1
        msg = signal_message(s, number)
        if send_telegram(msg):
            rec = {
                "id": key,
                "day": now.date().isoformat(),
                "symbol": symbol,
                "side": s["side"],
                "score": s["score"],
                "entry": s["plan"]["entry"],
                "sl": s["plan"]["sl"],
                "tp1": s["plan"]["tp1"],
                "tp2": s["plan"]["tp2"],
                "rr1": s["plan"]["rr1"],
                "rr2": s["plan"]["rr2"],
                "risk_atr": s["plan"]["risk_atr"],
                "signal_time": s["signal_time"].isoformat(),
                "sent_at": now.isoformat(),
                "status": "PENDING",
                "filled": False,
            }
            state["signals"].append(rec)
            log_trade({"event":"SIGNAL_SENT", **rec})
            save_state(state)
            log.info("%s SENT %s score=%s RR2=%.1f", symbol, s["side"], s["score"], s["plan"]["rr2"])

    # Daily report once after 17:00 UTC.
    if now.hour >= 17 and state.get("last_daily_report") != now.date().isoformat():
        send_telegram(daily_report(state, cache))
        state["last_daily_report"] = now.date().isoformat()
        save_state(state)


def main() -> None:
    env_ok()
    state = load_state()
    send_telegram("🟢 <b>SMC Day-Trading Assistant started</b>\nSignals only — no broker orders.")
    while True:
        try:
            scan_once(state)
        except Exception:
            log.error("MAIN ERROR\n%s", traceback.format_exc())
        if not RUN_FOREVER:
            break
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
