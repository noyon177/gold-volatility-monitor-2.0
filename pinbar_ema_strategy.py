"""
Pinbar + EMA(9/15) Trend Rejection Strategy — Signal Scanner (fixed)
====================================================================

Fixes vs. the old version:
  1. Only fully CLOSED candles are used (the still-forming candle is dropped).
     Old bug: Twelve Data returns the live candle too, so signals were sent
     mid-candle (Close == High) and the "pinbar" could change shape afterwards.
  2. Timezone is forced to UTC and candle close time is calculated properly.
  3. SL has an ATR-based buffer beyond the wick, and the minimum SL distance is
     now 1.0 x ATR (was 0.5 x ATR) -> avoids noise-sized stops.
  4. Trend filter is stricter: EMA15 slope must agree with the trade direction.
  5. Pullback check: previous candle must have closed on the trend side of EMA15.
  6. Stale-signal guard: if the cron run is late (> MAX_SIGNAL_DELAY_MIN after
     candle close), the signal is skipped because the entry price is outdated.

Required env vars / GitHub secrets:
  TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import datetime as dt
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD"},
    {"name": "BTCUSD", "td_symbol": "BTC/USD"},
]

INTERVAL = "15min"
INTERVAL_MINUTES = 15
EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14
CANDLES_NEEDED = 100
PINBAR_WICK_RATIO = 2.0
PINBAR_NOSE_MAX_RATIO = 0.4
EMA_TOUCH_TOLERANCE_PCT = 0.15
RISK_REWARD = 2.0

SL_BUFFER_ATR = 0.1          # SL goes this much (x ATR) beyond the wick
MIN_SL_ATR_MULTIPLIER = 1.0  # SL distance must be >= 1.0 x ATR, else skip
EMA_SLOPE_LOOKBACK = 3       # EMA15 must be rising (buy) / falling (sell) over N candles
MAX_SIGNAL_DELAY_MIN = 10    # skip if the candle closed more than this many minutes ago

HEARTBEAT_INTERVAL_MINUTES = 60
STATE_FILE = "last_signal_state.json"

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc


def now_utc():
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_candles(td_symbol: str):
    """Fetch candles from Twelve Data, oldest -> newest (UTC times)."""
    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": td_symbol,
            "interval": INTERVAL,
            "outputsize": CANDLES_NEEDED,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=20,
    )
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {td_symbol}: {data}")

    candles = []
    for row in reversed(data["values"]):  # API returns newest first
        t = dt.datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        candles.append({
            "time": row["datetime"],
            "dt": t,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })
    return candles


def drop_unclosed(candles):
    """Remove the last candle if it is still forming."""
    if candles:
        close_time = candles[-1]["dt"] + dt.timedelta(minutes=INTERVAL_MINUTES)
        if close_time > now_utc():
            return candles[:-1]
    return candles


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema_series(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def atr_series(candles, period):
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    atr = [None] * len(candles)
    if len(candles) < period:
        return atr
    atr[period - 1] = sum(trs[:period]) / period
    for i in range(period, len(candles)):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


# ---------------------------------------------------------------------------
# Pinbar detection
# ---------------------------------------------------------------------------

def classify_pinbar(candle):
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o) or 1e-9
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if lower_wick >= PINBAR_WICK_RATIO * body and upper_wick <= PINBAR_NOSE_MAX_RATIO * lower_wick:
        return "bullish"
    if upper_wick >= PINBAR_WICK_RATIO * body and lower_wick <= PINBAR_NOSE_MAX_RATIO * upper_wick:
        return "bearish"
    return None


def touches_ema(candle, ema_value):
    tol = ema_value * (EMA_TOUCH_TOLERANCE_PCT / 100)
    return candle["low"] - tol <= ema_value <= candle["high"] + tol


# ---------------------------------------------------------------------------
# Signal logic (candles must contain CLOSED candles only)
# ---------------------------------------------------------------------------

def check_signal(candles):
    if len(candles) < max(ATR_PERIOD, EMA_SLOW) + EMA_SLOPE_LOOKBACK + 2:
        return None

    closes = [c["close"] for c in candles]
    ema9 = ema_series(closes, EMA_FAST)
    ema15 = ema_series(closes, EMA_SLOW)
    atr = atr_series(candles, ATR_PERIOD)

    i = len(candles) - 1  # last CLOSED candle
    p = i - 1
    j = i - EMA_SLOPE_LOOKBACK
    if None in (ema9[i], ema15[i], atr[i], ema15[p], ema15[j]):
        return None

    candle = candles[i]
    prev = candles[p]

    pinbar_type = classify_pinbar(candle)
    if pinbar_type is None or not touches_ema(candle, ema15[i]):
        return None

    buffer_ = SL_BUFFER_ATR * atr[i]
    min_sl = MIN_SL_ATR_MULTIPLIER * atr[i]

    trend_up = ema9[i] > ema15[i] and ema15[i] > ema15[j]
    trend_down = ema9[i] < ema15[i] and ema15[i] < ema15[j]

    if pinbar_type == "bullish" and trend_up:
        if prev["close"] <= ema15[p]:      # previous candle must be above EMA15 (real pullback)
            return None
        entry = candle["close"]
        sl = candle["low"] - buffer_
        risk = entry - sl
        if risk <= 0 or risk < min_sl:
            return None
        side, tp = "BUY", entry + RISK_REWARD * risk

    elif pinbar_type == "bearish" and trend_down:
        if prev["close"] >= ema15[p]:      # previous candle must be below EMA15
            return None
        entry = candle["close"]
        sl = candle["high"] + buffer_
        risk = sl - entry
        if risk <= 0 or risk < min_sl:
            return None
        side, tp = "SELL", entry - RISK_REWARD * risk
    else:
        return None

    return {"side": side, "entry": entry, "sl": sl, "tp": tp,
            "time": candle["time"], "close_dt": candle["dt"] + dt.timedelta(minutes=INTERVAL_MINUTES),
            "candle": candle, "ema9": ema9[i], "ema15": ema15[i], "atr": atr[i]}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    resp = requests.post(url, data=payload, timeout=20)
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def should_send_heartbeat(state):
    last = state.get("_last_heartbeat")
    if not last:
        return True
    last_time = dt.datetime.fromisoformat(last)
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=UTC)
    return (now_utc() - last_time).total_seconds() / 60 >= HEARTBEAT_INTERVAL_MINUTES


def send_heartbeat(prices):
    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"✅ <b>বট চালু আছে</b> ({stamp})"]
    for name, price in prices.items():
        lines.append(f"{name}: {price:.3f}" if price is not None else f"{name}: ডেটা আনতে ব্যর্থ")
    send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    last_prices = {}

    for sym in SYMBOLS:
        name = sym["name"]
        try:
            raw = get_candles(sym["td_symbol"])
        except Exception as e:
            print(f"[{name}] fetch error: {e}")
            last_prices[name] = None
            continue

        last_prices[name] = raw[-1]["close"]   # live price, for heartbeat only
        candles = drop_unclosed(raw)           # signals use CLOSED candles only

        signal = check_signal(candles)
        if not signal:
            print(f"[{name}] no signal")
            continue

        delay_min = (now_utc() - signal["close_dt"]).total_seconds() / 60
        if delay_min > MAX_SIGNAL_DELAY_MIN:
            print(f"[{name}] signal skipped: stale ({delay_min:.0f} min after candle close)")
            continue

        if state.get(name) == signal["time"]:
            print(f"[{name}] signal already sent for {signal['time']}")
            continue

        o = signal["candle"]
        msg = (
            f"📢 <b>{signal['side']} SIGNAL — {name}</b>\n"
            f"Timeframe: 15M | Pinbar + EMA15 rejection (closed candle)\n"
            f"Entry: {signal['entry']:.3f}\n"
            f"SL: {signal['sl']:.3f}\n"
            f"TP: {signal['tp']:.3f}  (1:{RISK_REWARD:.0f} R:R)\n"
            f"Candle open time (UTC): {signal['time']}\n"
            f"\n"
            f"<i>Verify against your own chart:</i>\n"
            f"O: {o['open']:.3f}  H: {o['high']:.3f}\n"
            f"L: {o['low']:.3f}  C: {o['close']:.3f}\n"
            f"EMA9: {signal['ema9']:.3f}  EMA15: {signal['ema15']:.3f}\n"
            f"ATR(14): {signal['atr']:.3f}\n"
            f"<i>(data source: Twelve Data — may differ slightly from your broker feed)</i>"
        )
        send_telegram(msg)
        print(f"[{name}] SIGNAL SENT: {signal['side']} @ {signal['entry']}")
        state[name] = signal["time"]

    if should_send_heartbeat(state):
        send_heartbeat(last_prices)
        state["_last_heartbeat"] = now_utc().isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
