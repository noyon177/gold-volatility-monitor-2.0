"""
Gold (XAU/USD) Volatility Monitor
-----------------------------------
এই স্ক্রিপ্টটি Alpha Vantage API থেকে গোল্ডের ৫ মিনিটের ইন্ট্রাডে ডেটা নেয়,
ATR (Average True Range) ক্যালকুলেট করে মার্কেট ভোলাটিলিটি মাপে, এবং
থ্রেশহোল্ড ক্রস করলে Telegram-এ নোটিফিকেশন পাঠায়।

চালানোর আগে এই environment variable গুলো সেট করতে হবে:
  ALPHA_VANTAGE_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  ATR_THRESHOLD_PERCENT (ঐচ্ছিক, ডিফল্ট 0.5)
  ATR_PERIOD            (ঐচ্ছিক, ডিফল্ট 14)
"""

import os
from datetime import datetime, timezone

import requests

ALPHA_VANTAGE_KEY = os.environ["ALPHA_VANTAGE_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_THRESHOLD_PERCENT = float(os.environ.get("ATR_THRESHOLD_PERCENT", "0.5"))


def fetch_gold_intraday():
    """Alpha Vantage থেকে XAU/USD এর ৫ মিনিটের ক্যান্ডেল ডেটা আনে।"""
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY",
        "from_symbol": "XAU",
        "to_symbol": "USD",
        "interval": "5min",
        "outputsize": "compact",
        "apikey": ALPHA_VANTAGE_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    ts_key = "Time Series FX (5min)"
    if ts_key not in data:
        raise RuntimeError(f"API থেকে প্রত্যাশিত ডেটা আসেনি: {data}")

    rows = sorted(data[ts_key].items())  # সময় অনুযায়ী পুরনো থেকে নতুন সাজানো
    candles = [
        {
            "time": t,
            "open": float(v["1. open"]),
            "high": float(v["2. high"]),
            "low": float(v["3. low"]),
            "close": float(v["4. close"]),
        }
        for t, v in rows
    ]
    return candles


def compute_atr(candles, period):
    """Average True Range হিসাব করে — একটি স্ট্যান্ডার্ড ভোলাটিলিটি মেট্রিক।"""
    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if not true_ranges:
        raise RuntimeError("ATR হিসাব করার জন্য যথেষ্ট ডেটা নেই।")

    window = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    return sum(window) / len(window)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    candles = fetch_gold_intraday()
    if len(candles) < 2:
        print("যথেষ্ট ডেটা নেই, এই রাউন্ড স্কিপ করা হলো।")
        return

    atr = compute_atr(candles, ATR_PERIOD)
    last_price = candles[-1]["close"]
    atr_percent = (atr / last_price) * 100

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] দাম: ${last_price:.2f} | ATR: {atr:.2f} | ATR%: {atr_percent:.3f}%")

    if atr_percent >= ATR_THRESHOLD_PERCENT:
        message = (
            "🔔 <b>গোল্ড মার্কেট ভোলাটিলিটি অ্যালার্ট</b>\n\n"
            f"বর্তমান দাম: <b>${last_price:.2f}</b>\n"
            f"ATR ({ATR_PERIOD}-period): {atr:.2f}\n"
            f"ATR%: {atr_percent:.3f}% (থ্রেশহোল্ড: {ATR_THRESHOLD_PERCENT}%)\n\n"
            "⚠️ মার্কেটে স্বাভাবিকের চেয়ে বেশি মুভমেন্ট হচ্ছে।"
        )
        send_telegram_message(message)
        print("✅ অ্যালার্ট পাঠানো হয়েছে।")
    else:
        print("শান্ত মার্কেট — অ্যালার্ট পাঠানো হয়নি।")


if __name__ == "__main__":
    main()
