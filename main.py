"""
Gold (XAU/USD) Volatility Monitor
-----------------------------------
এই স্ক্রিপ্টটি xaus.com এর ফ্রি, key-বিহীন API থেকে গোল্ডের ২-মিনিটের
ইন্ট্রাডে প্রাইস সিরিজ নেয়, একটা রোলিং উইন্ডোতে দামের রেঞ্জ (%) দিয়ে
মার্কেট ভোলাটিলিটি মাপে, এবং থ্রেশহোল্ড ক্রস করলে Telegram-এ নোটিফিকেশন
পাঠায়।

চালানোর আগে এই environment variable গুলো সেট করতে হবে:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  VOLATILITY_THRESHOLD_PERCENT (ঐচ্ছিক, ডিফল্ট 0.5)
  WINDOW_HOURS                 (ঐচ্ছিক, ডিফল্ট 2 — কত ঘন্টার ডেটা নিয়ে ভোলাটিলিটি মাপা হবে)
"""

import os
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "2"))
VOLATILITY_THRESHOLD_PERCENT = float(
    os.environ.get("VOLATILITY_THRESHOLD_PERCENT", "0.5")
)


def fetch_gold_intraday():
    """xaus.com থেকে XAU/USD এর ইন্ট্রাডে (প্রতি ২ মিনিটের) প্রাইস পয়েন্ট আনে।"""
    url = "https://xaus.com/api/v1/intraday"
    params = {"symbol": "xau", "hours": WINDOW_HOURS}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    points = data.get("points")
    if not points:
        raise RuntimeError(f"API থেকে প্রত্যাশিত ডেটা আসেনি: {data}")

    # প্রতিটা পয়েন্ট {"t": timestamp, "p": price}
    prices = [float(pt["p"]) for pt in points]
    return prices


def compute_volatility_percent(prices):
    """উইন্ডোর মধ্যে দামের (high-low) রেঞ্জকে গড় দামের % হিসেবে বের করে।"""
    if len(prices) < 2:
        raise RuntimeError("ভোলাটিলিটি হিসাব করার জন্য যথেষ্ট ডেটা নেই।")

    high = max(prices)
    low = min(prices)
    avg = sum(prices) / len(prices)
    range_percent = ((high - low) / avg) * 100
    return range_percent, high, low


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    prices = fetch_gold_intraday()
    if len(prices) < 2:
        print("যথেষ্ট ডেটা নেই, এই রাউন্ড স্কিপ করা হলো।")
        return

    range_percent, high, low = compute_volatility_percent(prices)
    last_price = prices[-1]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"[{now}] দাম: ${last_price:.2f} | গত {WINDOW_HOURS}ঘ. রেঞ্জ: "
        f"${low:.2f}-${high:.2f} | ভোলাটিলিটি%: {range_percent:.3f}%"
    )

    if range_percent >= VOLATILITY_THRESHOLD_PERCENT:
        message = (
            "🔔 <b>গোল্ড মার্কেট ভোলাটিলিটি অ্যালার্ট</b>\n\n"
            f"বর্তমান দাম: <b>${last_price:.2f}</b>\n"
            f"গত {WINDOW_HOURS} ঘন্টার রেঞ্জ: ${low:.2f} - ${high:.2f}\n"
            f"ভোলাটিলিটি: {range_percent:.3f}% (থ্রেশহোল্ড: {VOLATILITY_THRESHOLD_PERCENT}%)\n\n"
            "⚠️ মার্কেটে স্বাভাবিকের চেয়ে বেশি মুভমেন্ট হচ্ছে।"
        )
        send_telegram_message(message)
        print("✅ অ্যালার্ট পাঠানো হয়েছে।")
    else:
        print("শান্ত মার্কেট — অ্যালার্ট পাঠানো হয়নি।")


if __name__ == "__main__":
    main()
