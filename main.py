"""গোল্ড আর বিটকয়েনের কারেন্ট ভোলাটিলিটি মাপার বট (স্ক্যাল্পিংয়ের জন্য ১ মিনিটের ক্যান্ডেল)।
সাপোর্ট/রেজিস্টেন্স বা ট্রেড সিগন্যাল নেই, শুধু ভোলাটিলিটি অ্যালার্ট।

- প্রতি রান ~৪.৫ মিনিট চলে, ৩০ সেকেন্ড পরপর দাম দেখে (GitHub Actions ৫ মিনিটে একবার চালায়)।
- চলমান ১ মিনিটের ক্যান্ডেলের রেঞ্জ আগের ৬০ ক্যান্ডেলের গড় রেঞ্জের THRESHOLD গুণ ছাড়ালে মেসেজ।
- গোল্ড আর বিটকয়েনের জন্য আলাদা আলাদা THRESHOLD (স্ক্যাল্পিংয়ের জন্য বেশি সংবেদনশীল করা হয়েছে)।
- ম্যানুয়ালি Run workflow চাপলে সাথে সাথে স্ট্যাটাস মেসেজ আসে (বট ঠিক আছে কিনা যাচাই)।
- প্রতিদিন সকাল ৯টায় (বাংলাদেশ) একটা "বট চালু আছে" রিপোর্ট আসে।
"""
import datetime as dt
import os
import sys
import time

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOOKBACK = 60            # তুলনার জন্য আগের কতগুলো ১-মিনিট ক্যান্ডেল
RUN_SECONDS = 270        # এক রানে কতক্ষণ নজর রাখবে
POLL_SECONDS = 30        # কত সেকেন্ড পরপর দেখবে
COOLDOWN = 300           # একই মার্কেটে দুই অ্যালার্টের মাঝে ন্যূনতম বিরতি (সেকেন্ড)
MAX_STALE = 180          # ক্যান্ডেল এর চেয়ে পুরনো হলে (মার্কেট বন্ধ/ডেটা আটকে) অ্যালার্ট নয়
GAP_RESET = 300          # ক্যান্ডেলের মাঝে এর চেয়ে বড় ফাঁক থাকলে (বিরতি/উইকএন্ড) গ্যাপকে নড়াচড়া ধরা হবে না
HEARTBEAT_UTC_HOUR = 3   # ০৩:০০ UTC = সকাল ৯:০০ বাংলাদেশ

HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_json(url, params=None, tries=3):
    err = None
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2)
    raise err


def gold_candles():
    # COMEX গোল্ড ফিউচার্স (Yahoo): গোল্ডের প্রধান মূল্য-নির্ধারণী বাজার
    last_err = None
    for host in ("query1", "query2"):
        try:
            data = get_json(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F",
                {"interval": "1m", "range": "2d"},
                tries=2,
            )
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(res["timestamp"]):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                out.append((t, o, h, l, c))
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err


def btc_candles():
    # Coinbase: নিয়ন্ত্রিত এক্সচেঞ্জ, সরাসরি ডেটা। ফরম্যাট: [time, low, high, open, close, volume]
    rows = get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        {"granularity": 60},
    )
    rows = sorted(rows, key=lambda x: x[0])
    return [(t, o, h, l, c) for t, l, h, o, c, _v in rows]


# প্রতিটা মার্কেটের নিজস্ব থ্রেশহোল্ড — BTC এমনিতেই বেশি ভোলাটাইল, তাই একটু বেশি রাখা হয়েছে।
# স্ক্যাল্পিং সিগন্যাল কেমন আসে দেখার জন্য শুরুতে সংবেদনশীল (কম) মান দেওয়া হলো;
# false alert বেশি মনে হলে ধীরে ধীরে বাড়িয়ে নিও।
MARKETS = {
    "গোল্ড (XAU)": {"fetch": gold_candles, "threshold": 1.8},
    "বিটকয়েন (BTC)": {"fetch": btc_candles, "threshold": 2.0},
}


def analyse(candles):
    if len(candles) < LOOKBACK + 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        t, _, h, l, _ = candles[i]
        pt, pc = candles[i - 1][0], candles[i - 1][4]
        if t - pt > GAP_RESET:      # বিরতির পর প্রথম ক্যান্ডেলে গ্যাপ গোনা হবে না
            tr = h - l
        else:
            tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    avg = sum(trs[-1 - LOOKBACK:-1]) / LOOKBACK
    if avg <= 0:
        return None
    t, _, _, _, c = candles[-1]
    return {
        "ratio": trs[-1] / avg,
        "move": trs[-1],
        "price": c,
        "stale": time.time() - t,
    }


def send(text):
    last_err = None
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=15,
            )
            r.raise_for_status()
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    print(f"টেলিগ্রাম পাঠানো ব্যর্থ: {last_err}")


def age_text(sec):
    if sec > MAX_STALE:
        return f"⚠️ {sec / 60:.0f} মিনিট পুরনো (মার্কেট বন্ধ বা ডেটা দেরিতে)"
    return f"তাজা ({sec:.0f} সেকেন্ড আগের)"


def send_status(title):
    lines = [title]
    for name, cfg in MARKETS.items():
        try:
            info = analyse(cfg["fetch"]())
            if not info:
                lines.append(f"{name}: যথেষ্ট ডেটা নেই")
            else:
                lines.append(
                    f"{name}: ${info['price']:,.2f} | ভোলাটিলিটি {info['ratio']:.1f}x "
                    f"(থ্রেশহোল্ড {cfg['threshold']:.1f}x) | ডেটা {age_text(info['stale'])}"
                )
        except Exception as e:  # noqa: BLE001
            lines.append(f"{name}: ডেটা আনতে ব্যর্থ ({type(e).__name__})")
    send("\n".join(lines))


def main():
    now = dt.datetime.now(dt.timezone.utc)
    event = os.getenv("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        send_status("✅ টেস্ট: বট চালু আছে")
    elif now.hour == HEARTBEAT_UTC_HOUR and now.minute < 5:
        send_status("✅ বট চালু আছে (দৈনিক রিপোর্ট)")

    end = time.time() + RUN_SECONDS
    last_alert = {}
    fetched_ok = {name: False for name in MARKETS}

    while True:
        for name, cfg in MARKETS.items():
            try:
                info = analyse(cfg["fetch"]())
                fetched_ok[name] = True
                if not info:
                    print(f"{name}: যথেষ্ট ডেটা নেই")
                    continue
                print(f"{name}: অনুপাত {info['ratio']:.2f}, ডেটার বয়স {info['stale']:.0f}s")
                if info["stale"] > MAX_STALE:
                    continue
                threshold = cfg["threshold"]
                if info["ratio"] >= threshold and time.time() - last_alert.get(name, 0) >= COOLDOWN:
                    send(
                        f"⚡ {name} এখন ভোলাটাইল!\n"
                        f"এই ১ মিনিটে নড়াচড়া স্বাভাবিকের {info['ratio']:.1f} গুণ (${info['move']:,.2f})\n"
                        f"দাম: ${info['price']:,.2f}"
                    )
                    last_alert[name] = time.time()
            except Exception as e:  # noqa: BLE001
                print(f"{name}: ত্রুটি - {e}")
        if time.time() + POLL_SECONDS >= end:
            break
        time.sleep(POLL_SECONDS)

    if not all(fetched_ok.values()):
        # পুরো রানে কোনো মার্কেটের ডেটা একবারও না এলে Actions লাল দেখাবে
        sys.exit(1)


if __name__ == "__main__":
    main()
