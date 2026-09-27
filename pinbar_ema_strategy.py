"""
Pinbar + EMA(9/15) Trend Rejection Strategy — Signal Scanner
==============================================================

Strategy rules (15-minute candles):
  - Trend filter : EMA9 vs EMA15
        Uptrend   -> EMA9 > EMA15
        Downtrend -> EMA9 < EMA15
  - Setup        : price touches/reacts off EMA15 and forms a pinbar
        Bullish pinbar in an UPTREND   -> BUY signal
        Bearish pinbar in a DOWNTREND  -> SELL signal
  - Stop Loss    : just beyond the pinbar's wick
        BUY  -> SL below the pinbar low
        SELL -> SL above the pinbar high
  - Take Profit  : Risk:Reward = 1:2 (TP distance = 2x SL distance)

Data source : Twelve Data (unified endpoint for both XAU/USD and BTC/USD)
Alert       : Telegram Bot

Required environment variables / GitHub Actions secrets:
  TWELVE_DATA_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Run this on a schedule (e.g. every 15 minutes) via GitHub Actions cron.
"""

import os
import json
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOLS = [
    {"name": "XAUUSD", "td_symbol": "XAU/USD"},
    {"name": "BTCUSD", "td_symbol": "BTC/USD"},
]

INTERVAL = "15min"
EMA_FAST = 9
EMA_SLOW = 15
CANDLES_NEEDED = 60          # enough history for stable EMA15
PINBAR_WICK_RATIO = 2.0      # dominant wick must be >= 2x the body
PINBAR_NOSE_MAX_RATIO = 0.4  # opposite wick must be small vs the body
EMA_TOUCH_TOLERANCE_PCT = 0.15  # how close (%) price must get to EMA15 to count as a "touch"
RISK_REWARD = 2.0

STATE_FILE = "last_signal_state.json"  # remembers last alerted candle per symbol

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_candles(td_symbol: str):
    """Fetch OHLC candles from Twelve Data, oldest -> newest."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol,
        "interval": INTERVAL,
        "outputsize": CANDLES_NEEDED,
        "apikey": TWELVE_DATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {td_symbol}: {data}")

    candles = []
    for row in reversed(data["values"]):  # API returns newest first
        candles.append({
            "time": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })
    return candles


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema_series(closes, period):
    """Return an EMA list aligned with `closes` (None where not enough data)."""
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    sma = sum(closes[:period]) / period
    ema[period - 1] = sma
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


# ---------------------------------------------------------------------------
# Pinbar detection
# ---------------------------------------------------------------------------

def classify_pinbar(candle):
    """Return 'bullish', 'bearish' or None."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    if body == 0:
        body = 1e-9  # avoid div by zero on doji

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Bullish pinbar: long lower wick, small body, small upper wick ("nose")
    if (lower_wick >= PINBAR_WICK_RATIO * body and
            upper_wick <= PINBAR_NOSE_MAX_RATIO * lower_wick):
        return "bullish"

    # Bearish pinbar: long upper wick, small body, small lower wick ("nose")
    if (upper_wick >= PINBAR_WICK_RATIO * body and
            lower_wick <= PINBAR_NOSE_MAX_RATIO * upper_wick):
        return "bearish"

    return None


def touches_ema(candle, ema_value):
    """True if the candle's range came within tolerance of the EMA15 line."""
    tolerance = ema_value * (EMA_TOUCH_TOLERANCE_PCT / 100)
    return candle["low"] - tolerance <= ema_value <= candle["high"] + tolerance


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------

def check_signal(candles):
    closes = [c["close"] for c in candles]
    ema9 = ema_series(closes, EMA_FAST)
    ema15 = ema_series(closes, EMA_SLOW)

    i = len(candles) - 1  # last CLOSED candle
    if ema9[i] is None or ema15[i] is None:
        return None

    candle = candles[i]
    trend_up = ema9[i] > ema15[i]
    trend_down = ema9[i] < ema15[i]

    pinbar_type = classify_pinbar(candle)
    if pinbar_type is None:
        return None
    if not touches_ema(candle, ema15[i]):
        return None

    if pinbar_type == "bullish" and trend_up:
        entry = candle["close"]
        sl = candle["low"]
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + RISK_REWARD * risk
        return {"side": "BUY", "entry": entry, "sl": sl, "tp": tp,
                "time": candle["time"]}

    if pinbar_type == "bearish" and trend_down:
        entry = candle["close"]
        sl = candle["high"]
        risk = sl - entry
        if risk <= 0:
            return None
        tp = entry - RISK_REWARD * risk
        return {"side": "SELL", "entry": entry, "sl": sl, "tp": tp,
                "time": candle["time"]}

    return None


# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    resp = requests.post(url, data=payload, timeout=20)
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.text}")


# ---------------------------------------------------------------------------
# Simple duplicate-alert guard (per symbol, per candle time)
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
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()

    for sym in SYMBOLS:
        name = sym["name"]
        try:
            candles = get_candles(sym["td_symbol"])
        except Exception as e:
            print(f"[{name}] fetch error: {e}")
            continue

        signal = check_signal(candles)
        if not signal:
            print(f"[{name}] no signal")
            continue

        # avoid re-alerting the same candle
        if state.get(name) == signal["time"]:
            print(f"[{name}] signal already sent for {signal['time']}")
            continue

        msg = (
            f"📢 <b>{signal['side']} SIGNAL — {name}</b>\n"
            f"Timeframe: 15M | Pinbar + EMA15 rejection\n"
            f"Entry: {signal['entry']:.3f}\n"
            f"SL: {signal['sl']:.3f}\n"
            f"TP: {signal['tp']:.3f}  (1:{RISK_REWARD:.0f} R:R)\n"
            f"Candle time: {signal['time']}"
        )
        send_telegram(msg)
        print(f"[{name}] SIGNAL SENT: {signal}")

        state[name] = signal["time"]

    save_state(state)


if __name__ == "__main__":
    main()
