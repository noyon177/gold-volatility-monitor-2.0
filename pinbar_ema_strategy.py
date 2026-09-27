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
  - Volatility filter : SL distance must be >= 0.5x ATR(14), otherwise the
    signal is skipped (protects against unrealistically tight stops)
  - Heartbeat    : sends a "bot is alive" message with current prices every
    HEARTBEAT_INTERVAL_MINUTES, even when there is no trade signal

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
import datetime
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
ATR_PERIOD = 14
CANDLES_NEEDED = 60          # enough history for stable EMA15 / ATR14
PINBAR_WICK_RATIO = 2.0      # dominant wick must be >= 2x the body
PINBAR_NOSE_MAX_RATIO = 0.4  # opposite wick must be small vs the body
EMA_TOUCH_TOLERANCE_PCT = 0.15  # how close (%) price must get to EMA15 to count as a "touch"
RISK_REWARD = 2.0
MIN_SL_ATR_MULTIPLIER = 0.5  # SL distance must be >= 0.5x ATR14, else signal is skipped (too tight/noisy)

HEARTBEAT_INTERVAL_MINUTES = 60  # send an "I'm alive" message this often

STATE_FILE = "last_signal_state.json"  # remembers last alerted candle per symbol, and last heartbeat time

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


def atr_series(candles, period):
    """Average True Range, aligned with `candles` (None where not enough data)."""
    trs = [None] * len(candles)
    for i in range(len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        if i == 0:
            trs[i] = h - l
        else:
            prev_close = candles[i - 1]["close"]
            trs[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))

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
    atr = atr_series(candles, ATR_PERIOD)

    i = len(candles) - 1  # last CLOSED candle
    if ema9[i] is None or ema15[i] is None or atr[i] is None:
        return None

    candle = candles[i]
    trend_up = ema9[i] > ema15[i]
    trend_down = ema9[i] < ema15[i]

    pinbar_type = classify_pinbar(candle)
    if pinbar_type is None:
        return None
    if not touches_ema(candle, ema15[i]):
        return None

    min_sl_distance = MIN_SL_ATR_MULTIPLIER * atr[i]

    if pinbar_type == "bullish" and trend_up:
        entry = candle["close"]
        sl = candle["low"]
        risk = entry - sl
        if risk <= 0 or risk < min_sl_distance:
            return None  # SL too tight relative to current volatility -> skip
        tp = entry + RISK_REWARD * risk
        return {"side": "BUY", "entry": entry, "sl": sl, "tp": tp,
                "time": candle["time"], "candle": candle,
                "ema9": ema9[i], "ema15": ema15[i], "atr": atr[i]}

    if pinbar_type == "bearish" and trend_down:
        entry = candle["close"]
        sl = candle["high"]
        risk = sl - entry
        if risk <= 0 or risk < min_sl_distance:
            return None  # SL too tight relative to current volatility -> skip
        tp = entry - RISK_REWARD * risk
        return {"side": "SELL", "entry": entry, "sl": sl, "tp": tp,
                "time": candle["time"], "candle": candle,
                "ema9": ema9[i], "ema15": ema15[i], "atr": atr[i]}

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
# Heartbeat ("bot is alive") message
# ---------------------------------------------------------------------------

def should_send_heartbeat(state):
    last = state.get("_last_heartbeat")
    if not last:
        return True
    last_time = datetime.datetime.fromisoformat(last)
    elapsed_minutes = (datetime.datetime.utcnow() - last_time).total_seconds() / 60
    return elapsed_minutes >= HEARTBEAT_INTERVAL_MINUTES


def send_heartbeat(prices):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"✅ <b>বট চালু আছে</b> ({now})"]
    for name, price in prices.items():
        if price is not None:
            lines.append(f"{name}: {price:.3f}")
        else:
            lines.append(f"{name}: ডেটা আনতে ব্যর্থ")
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
            candles = get_candles(sym["td_symbol"])
        except Exception as e:
            print(f"[{name}] fetch error: {e}")
            last_prices[name] = None
            continue

        last_prices[name] = candles[-1]["close"]

        signal = check_signal(candles)
        if not signal:
            print(f"[{name}] no signal")
            continue

        # avoid re-alerting the same candle
        if state.get(name) == signal["time"]:
            print(f"[{name}] signal already sent for {signal['time']}")
            continue

        ohlc = signal["candle"]
        msg = (
            f"📢 <b>{signal['side']} SIGNAL — {name}</b>\n"
            f"Timeframe: 15M | Pinbar + EMA15 rejection\n"
            f"Entry: {signal['entry']:.3f}\n"
            f"SL: {signal['sl']:.3f}\n"
            f"TP: {signal['tp']:.3f}  (1:{RISK_REWARD:.0f} R:R)\n"
            f"Candle time: {signal['time']}\n"
            f"\n"
            f"<i>Verify against your own chart:</i>\n"
            f"O: {ohlc['open']:.3f}  H: {ohlc['high']:.3f}\n"
            f"L: {ohlc['low']:.3f}  C: {ohlc['close']:.3f}\n"
            f"EMA9: {signal['ema9']:.3f}  EMA15: {signal['ema15']:.3f}\n"
            f"ATR(14): {signal['atr']:.3f}\n"
            f"<i>(data source: Twelve Data — may differ slightly from your broker feed)</i>"
        )
        send_telegram(msg)
        print(f"[{name}] SIGNAL SENT: {signal}")

        state[name] = signal["time"]

    if should_send_heartbeat(state):
        send_heartbeat(last_prices)
        state["_last_heartbeat"] = datetime.datetime.utcnow().isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
      
