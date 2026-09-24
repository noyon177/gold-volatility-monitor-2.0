"""গোল্ড আর বিটকয়েনের কারেন্ট ভোলাটিলিটি ও ভলিউম-স্পাইক মাপার বট।

দুইটা মেট্রিক ব্যবহার হয়:
- ATR (Average True Range): দাম কতটা নড়ছে, আগের LOOKBACK ক্যান্ডেলের গড় রেঞ্জের
  তুলনায় বর্তমান ক্যান্ডেলের রেঞ্জ কত গুণ।
- PVT (Price Volume Trend): ভলিউম-সহ দামের পরিবর্তন, হঠাৎ ভলিউম-চালিত মুভ ধরার জন্য।
  (নোট: গোল্ড/XAU একটা OTC মার্কেট, তাই এর ভলিউম ডেটা সবসময় নির্ভরযোগ্য না।
  BTC-তে (Coinbase) রিয়েল ভলিউম থাকায় PVT ওখানে বেশি কার্যকর।)

দুটো মেট্রিকের যেকোনো একটা নিজের থ্রেশহোল্ড ছাড়ালে অ্যালার্ট যাবে।

- গোল্ডের ডেটা: Twelve Data (রেট-লিমিট সহনীয়), প্রতি রানে একবারই আনা হয়।
- বিটকয়েনের ডেটা: Coinbase (রেট-লিমিট নেই), প্রতি ৩০ সেকেন্ডে রিফ্রেশ হয়।
- ম্যানুয়ালি Run workflow চাপলে সাথে সাথে স্ট্যাটাস মেসেজ আসে।
- প্রতিদিন সকাল ৯টায় (বাংলাদেশ) একটা "বট চালু আছে" রিপোর্ট আসে।
"""
import datetime as dt
import os
import sys
import time

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

LOOKBACK = 60            # তুলনার জন্য আগের কতগুলো ১-মিনিট ক্যান্ডেল
RUN_SECONDS = 270        # এক রানে কতক্ষণ BTC নজর রাখবে
POLL_SECONDS = 30        # BTC কত সেকেন্ড পরপর দেখবে (গোল্ড শুধু একবার আনা হয়)
COOLDOWN = 300           # একই মার্কেটে দুই অ্যালার্টের মাঝে ন্যূনতম বিরতি (সেকেন্ড)
MAX_STALE = 180          # ক্যান্ডেল এর চেয়ে পুরনো হলে (মার্কেট বন্ধ/ডেটা আটকে) অ্যালার্ট নয়
GAP_RESET = 300          # ক্যান্ডেলের মাঝে এর চেয়ে বড় ফাঁক থাকলে গ্যাপকে নড়াচড়া ধরা হবে না
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
    # Twelve Data: XAU/USD, ১-মিনিট ক্যান্ডেল। ভলিউম OTC মার্কেট হওয়ায় অনির্ভরযোগ্য হতে পারে।
    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": "XAU/USD",
            "interval": "1min",
            "outputsize": LOOKBACK + 5,
            "apikey": TWELVE_DATA_KEY,
        },
    )
    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data error"))
    rows = data["values"]
    rows = sorted(rows, key=lambda r: r["datetime"])
    out = []
    for r in rows:
        t = dt.datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        ).timestamp()
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        v = float(r.get("volume") or 0)
        out.append((t, o, h, l, c, v))
    return out


def btc_candles():
    # Coinbase: নিয়ন্ত্রিত এক্সচেঞ্জ, সরাসরি ডেটা সহ real volume।
    # ফরম্যাট: [time, low, high, open, close, volume]
    rows = get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        {"granularity": 60},
    )
    rows = sorted(rows, key=lambda x: x[0])
    return [(t, o, h, l, c, v) for t, l, h, o, c, v in rows]


# প্রতিটা মার্কেটের নিজস্ব থ্রেশহোল্ড। ATR = দামের নড়াচড়া, PVT = ভলিউম-চালিত নড়াচড়া।
MARKETS = {
    "গোল্ড (XAU)": {"fetch": gold_candles, "atr_threshold": 1.8, "pvt_threshold": 2.5},
    "বিটকয়েন (BTC)": {"fetch": btc_candles, "atr_threshold": 2.0, "pvt_threshold": 2.0},
}


def analyse(candles):
    if len(candles) < LOOKBACK + 2:
        return None

    trs = []       # true range প্রতি ক্যান্ডেলে
    pvts = []      # volume * রিটার্ন প্রতি ক্যান্ডেলে
    for i in range(1, len(candles)):
        t, _, h, l, c, v = candles[i]
        pt, pc = candles[i - 1][0], candles[i - 1][4]
        if t - pt > GAP_RESET:  # বিরতির পর প্রথম ক্যান্ডেলে গ্যাপ গোনা হবে না
            tr = h - l
        else:
            tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if pc:
            pvts.append(abs(v * (c - pc) / pc))
        else:
            pvts.append(0.0)

    atr_avg = sum(trs[-1 - LOOKBACK:-1]) / LOOKBACK
    pvt_avg = sum(pvts[-1 - LOOKBACK:-1]) / LOOKBACK

    atr_ratio = trs[-1] / atr_avg if atr_avg > 0 else 0.0
    pvt_ratio = pvts[-1] / pvt_avg if pvt_avg > 0 else 0.0

    t, _, _, _, c, _ = candles[-1]
    return {
        "atr_ratio": atr_ratio,
        "pvt_ratio": pvt_ratio,
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
                    f"{name}: ${info['price']:,.2f} | ATR {info['atr_ratio']:.1f}x "
                    f"(থ্রে {cfg['atr_threshold']:.1f}x) | PVT {info['pvt_ratio']:.1f}x "
                    f"(থ্রে {cfg['pvt_threshold']:.1f}x) | ডেটা {age_text(info['stale'])}"
                )
        except Exception as e:  # noqa: BLE001
            lines.append(f"{name}: ডেটা আনতে ব্যর্থ ({type(e).__name__})")
    send("\n".join(lines))


def check_market(name, cfg, candles, last_alert):
    info = analyse(candles)
    if not info:
        print(f"{name}: যথেষ্ট ডেটা নেই")
        return
    print(
        f"{name}: ATR {info['atr_ratio']:.2f}x, PVT {info['pvt_ratio']:.2f}x, "
        f"ডেটার বয়স {info['stale']:.0f}s"
    )
    if info["stale"] > MAX_STALE:
        return
    if time.time() - last_alert.get(name, 0) < COOLDOWN:
        return

    hit_atr = info["atr_ratio"] >= cfg["atr_threshold"]
    hit_pvt = info["pvt_ratio"] >= cfg["pvt_threshold"]
    if not (hit_atr or hit_pvt):
        return

    reasons = []
    if hit_atr:
        reasons.append(f"দাম {info['atr_ratio']:.1f}x স্বাভাবিকের চেয়ে বেশি নড়ছে (ATR)")
    if hit_pvt:
        reasons.append(f"ভলিউম-চালিত মুভ {info['pvt_ratio']:.1f}x স্বাভাবিকের বেশি (PVT)")
    send(
        f"⚡ {name} এখন অস্থির!\n"
        + "\n".join(reasons)
        + f"\nদাম: ${info['price']:,.2f}"
    )
    last_alert[name] = time.time()


def main():
    now = dt.datetime.now(dt.timezone.utc)
    event = os.getenv("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        send_status("✅ টেস্ট: বট চালু আছে")
    elif now.hour == HEARTBEAT_UTC_HOUR and now.minute < 5:
        send_status("✅ বট চালু আছে (দৈনিক রিপোর্ট)")

    last_alert = {}
    fetched_ok = {name: False for name in MARKETS}

    # গোল্ড রেট-লিমিটেড, তাই এই রানে একবারই আনা হচ্ছে।
    gold_cfg = MARKETS["গোল্ড (XAU)"]
    try:
        gold_candle_data = gold_cfg["fetch"]()
        fetched_ok["গোল্ড (XAU)"] = True
    except Exception as e:  # noqa: BLE001
        gold_candle_data = None
        print(f"গোল্ড (XAU): ত্রুটি - {e}")
    if gold_candle_data:
        check_market("গোল্ড (XAU)", gold_cfg, gold_candle_data, last_alert)

    # BTC-র রেট লিমিট নেই, তাই পুরো রান জুড়ে বারবার চেক করা হয়।
    btc_cfg = MARKETS["বিটকয়েন (BTC)"]
    end = time.time() + RUN_SECONDS
    while True:
        try:
            candles = btc_cfg["fetch"]()
            fetched_ok["বিটকয়েন (BTC)"] = True
            check_market("বিটকয়েন (BTC)", btc_cfg, candles, last_alert)
        except Exception as e:  # noqa: BLE001
            print(f"বিটকয়েন (BTC): ত্রুটি - {e}")
        if time.time() + POLL_SECONDS >= end:
            break
        time.sleep(POLL_SECONDS)

    if not all(fetched_ok.values()):
        # পুরো রানে কোনো মার্কেটের ডেটা একবারও না এলে Actions লাল দেখাবে
        sys.exit(1)


if __name__ == "__main__":
    main()
