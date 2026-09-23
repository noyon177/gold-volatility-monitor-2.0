import os
import requests

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# NOTE: "DJI" is used as the US30 (Dow Jones) symbol on Twelve Data.
# Verify via https://api.twelvedata.com/symbol_search?symbol=US30
# and change it below if needed.
SYMBOLS = ["XAU/USD", "BTC/USD", "GBP/USD", "USD/JPY", "DJI"]

INTERVAL = "15min"
OUTPUT_SIZE = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
SR_LOOKBACK = 20        # candles to look back for support/resistance
SR_TOUCH_PCT = 0.15     # % distance from a level that counts as "near"


def fetch_candles(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "outputsize": OUTPUT_SIZE,
        "apikey": TWELVE_DATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "values" not in data:
        print(f"[{symbol}] API error: {data}")
        return None
    candles = list(reversed(data["values"]))  # oldest -> newest
    for c in candles:
        for k in ("open", "high", "low", "close"):
            c[k] = float(c[k])
    return candles


def compute_rsi(closes, period=RSI_PERIOD):
    """Pure-Python RSI calculation — no external libraries needed."""
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)

    if len(gains) < period:
        return 50.0  # not enough data yet, neutral RSI

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def find_support_resistance(candles, lookback=SR_LOOKBACK):
    recent = candles[-lookback:]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    return max(highs), min(lows)


def is_near(price, level, pct=SR_TOUCH_PCT):
    return abs(price - level) / level * 100 <= pct


def is_reversal_candle(candles):
    """Simple bullish/bearish engulfing check on the last 2 candles."""
    if len(candles) < 2:
        return None
    prev, last = candles[-2], candles[-1]
    bullish_engulf = (
        prev["close"] < prev["open"]
        and last["close"] > last["open"]
        and last["close"] >= prev["open"]
        and last["open"] <= prev["close"]
    )
    bearish_engulf = (
        prev["close"] > prev["open"]
        and last["close"] < last["open"]
        and last["open"] >= prev["close"]
        and last["close"] <= prev["open"]
    )
    if bullish_engulf:
        return "bullish engulfing"
    if bearish_engulf:
        return "bearish engulfing"
    return None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=15,
    )


def scan_symbol(symbol):
    candles = fetch_candles(symbol)
    if not candles or len(candles) < RSI_PERIOD + 2:
        return

    closes = [c["close"] for c in candles]
    price = closes[-1]
    rsi = compute_rsi(closes)
    resistance, support = find_support_resistance(candles)
    reversal = is_reversal_candle(candles)

    alerts = []
    if rsi >= RSI_OVERBOUGHT:
        alerts.append(f"RSI ওভারবট ({rsi:.1f})")
    elif rsi <= RSI_OVERSOLD:
        alerts.append(f"RSI ওভারসোল্ড ({rsi:.1f})")
    if is_near(price, resistance):
        alerts.append(f"রেজিস্ট্যান্সের কাছে ({resistance:.4f})")
    if is_near(price, support):
        alerts.append(f"সাপোর্টের কাছে ({support:.4f})")
    if reversal:
        alerts.append(f"{reversal} ক্যান্ডেল")

    if alerts:
        msg = (
            f"⚡ <b>{symbol}</b> ({INTERVAL})\n"
            f"প্রাইস: {price:.4f}\n" + "\n".join(f"• {a}" for a in alerts)
        )
        send_telegram(msg)
        print(f"[{symbol}] alert sent: {alerts}")
    else:
        print(f"[{symbol}] no signal. price={price:.4f} rsi={rsi:.1f}")


def main():
    for symbol in SYMBOLS:
        try:
            scan_symbol(symbol)
        except Exception as e:
            print(f"[{symbol}] error: {e}")


if __name__ == "__main__":
    main()
