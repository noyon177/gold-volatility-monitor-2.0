#!/usr/bin/env python3
"""
Momentum / Panic-Sell + High Volume ALERT bot  (শুধু অ্যালার্ট, কোনো ট্রেড সিগন্যাল নয়)

- M5 ক্যান্ডেল দেখে: বড় বডি + হাই ভলিউম + ক্যান্ডেল এক দিকে ক্লোজ
    সবুজ  -> 🚀 মোমেন্টাম শুরু
    লাল   -> 🔻 প্যানিক সেলিং শুরু
- প্রাইমারি ডেটা: Twelve Data, ব্যাকআপ: Yahoo Finance (ভলিউম না পেলে বা ফেইল করলে)
- GitHub Actions থেকে প্রতি ৫ মিনিটে চলে; state.json এ ডুপ্লিকেট আটকায়
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------- কনফিগ (ENV দিয়ে বদলানো যায়) ----------------
SYMBOLS = {
    "XAUUSD": {"td": "XAU/USD", "yf": "GC=F"},
    "BTCUSD": {"td": "BTC/USD", "yf": "BTC-USD"},
}
INTERVAL_MIN = 5
LOOKBACK = int(os.getenv("LOOKBACK", "20"))            # গড় বের করার ক্যান্ডেল সংখ্যা
VOL_MULT = float(os.getenv("VOL_MULT", "2.0"))         # ভলিউম গড়ের কত গুণ হলে "হাই"
BODY_MULT = float(os.getenv("BODY_MULT", "1.8"))       # বডি গড় বডির কত গুণ হলে "বড়"
CLOSE_POS = float(os.getenv("CLOSE_POS", "0.70"))      # ক্লোজ রেঞ্জের কত অংশে (শক্ত ক্লোজ)
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "20"))    # একই দিকে পরপর অ্যালার্ট আটকাতে
CHECK_LAST = int(os.getenv("CHECK_LAST", "3"))         # শেষ কয়টা ক্লোজড ক্যান্ডেল চেক (cron দেরি কভার)
MAX_AGE_MIN = int(os.getenv("MAX_AGE_MIN", "30"))      # এর চেয়ে পুরনো ডেটা = মার্কেট বন্ধ, স্কিপ
STATE_FILE = os.getenv("STATE_FILE", "momentum_state.json")

TD_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------- ডেটা ----------------
def fetch_twelvedata(symbol):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol, "interval": "5min", "outputsize": 60,
            "timezone": "UTC", "apikey": TD_KEY,
        },
        timeout=20,
    )
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "Twelve Data error"))
    out = []
    for v in data["values"]:
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({
            "t": t, "o": float(v["open"]), "h": float(v["high"]),
            "l": float(v["low"]), "c": float(v["close"]),
            "v": float(v.get("volume") or 0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def fetch_yahoo(symbol):
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "5m", "range": "1d"},
        headers=HEADERS, timeout=20,
    )
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        out.append({
            "t": datetime.fromtimestamp(t, tz=timezone.utc),
            "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
            "c": q["close"][i], "v": float(q["volume"][i] or 0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def has_volume(candles):
    recent = candles[-(LOOKBACK + 2):]
    return len(recent) >= LOOKBACK + 2 and sum(c["v"] for c in recent) > 0


def get_candles(name, cfg):
    try:
        c = fetch_twelvedata(cfg["td"])
        if has_volume(c):
            return c, "Twelve Data"
        print(f"[{name}] Twelve Data এ ভলিউম নেই, ব্যাকআপে যাচ্ছি")
    except Exception as e:
        print(f"[{name}] Twelve Data ফেইল: {e}")
    c = fetch_yahoo(cfg["yf"])
    if has_volume(c):
        return c, "Yahoo (ব্যাকআপ)"
    raise RuntimeError("কোনো সোর্সে ভলিউম পাওয়া যায়নি")


# ---------------- ডিটেকশন ----------------
def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def analyze(closed, idx):
    """closed[idx] ক্যান্ডেলটা চেক করে; ফেরত: dict বা None"""
    base = closed[max(0, idx - LOOKBACK):idx]
    if len(base) < LOOKBACK:
        return None
    cd = closed[idx]
    avg_vol = mean([b["v"] for b in base])
    avg_body = mean([abs(b["c"] - b["o"]) for b in base])
    rng = cd["h"] - cd["l"]
    if avg_vol <= 0 or avg_body <= 0 or rng <= 0:
        return None

    vol_ratio = cd["v"] / avg_vol
    body = abs(cd["c"] - cd["o"])
    body_ratio = body / avg_body
    pos = (cd["c"] - cd["l"]) / rng  # 1 = হাইয়ের কাছে ক্লোজ, 0 = লোয়ের কাছে

    if vol_ratio < VOL_MULT or body_ratio < BODY_MULT:
        return None
    if cd["c"] > cd["o"] and pos >= CLOSE_POS:
        kind = "MOMENTUM"
    elif cd["c"] < cd["o"] and pos <= 1 - CLOSE_POS:
        kind = "PANIC"
    else:
        return None
    return {"kind": kind, "vol_ratio": vol_ratio, "body_ratio": body_ratio, "candle": cd}


# ---------------- স্টেট / টেলিগ্রাম ----------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.text}")


def build_message(name, res, source):
    cd = res["candle"]
    bd = cd["t"] + timedelta(hours=6)  # বাংলাদেশ সময়
    move = cd["c"] - cd["o"]
    if res["kind"] == "MOMENTUM":
        head = f"🚀 {name} — মোমেন্টাম শুরু (হাই ভলিউম)"
    else:
        head = f"🔻 {name} — প্যানিক সেলিং শুরু (হাই ভলিউম)"
    return (
        f"{head}\n\n"
        f"ক্যান্ডেল (M5): {bd.strftime('%d %b, %I:%M %p')} (BD)\n"
        f"ওপেন: {cd['o']:.2f} → ক্লোজ: {cd['c']:.2f} ({move:+.2f})\n"
        f"ভলিউম: গড়ের {res['vol_ratio']:.1f} গুণ\n"
        f"বডি: গড়ের {res['body_ratio']:.1f} গুণ\n"
        f"সোর্স: {source}\n\n"
        f"⚠️ এটা শুধু অ্যালার্ট, এন্ট্রি সিগন্যাল নয়। চার্ট দেখে নিজে সিদ্ধান্ত নিন।"
    )


# ---------------- মেইন ----------------
def main():
    if not (TG_TOKEN and TG_CHAT):
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID সেট করা নেই")

    if os.getenv("TEST_MODE") == "true":
        send_telegram("✅ টেস্ট মেসেজ: মোমেন্টাম/প্যানিক অ্যালার্ট বট ঠিকমতো কানেক্টেড।")
        print("টেস্ট মেসেজ পাঠানো হয়েছে")
        return

    state = load_state()
    now = datetime.now(timezone.utc)
    changed = False

    for name, cfg in SYMBOLS.items():
        try:
            candles, source = get_candles(name, cfg)
        except Exception as e:
            print(f"[{name}] ডেটা আনা যায়নি: {e}")
            continue

        # ফর্ম হতে থাকা ক্যান্ডেল বাদ
        closed = [c for c in candles if c["t"] + timedelta(minutes=INTERVAL_MIN) <= now]
        if len(closed) < LOOKBACK + 1:
            continue
        if now - (closed[-1]["t"] + timedelta(minutes=INTERVAL_MIN)) > timedelta(minutes=MAX_AGE_MIN):
            print(f"[{name}] ডেটা পুরনো (মার্কেট বন্ধ?), স্কিপ")
            continue

        # লগে দেখার জন্য: শেষ ক্লোজড ক্যান্ডেলের ভলিউম/বডি গড়ের কত গুণ
        b = closed[-LOOKBACK - 1:-1]
        av, ab = mean([x["v"] for x in b]), mean([abs(x["c"] - x["o"]) for x in b])
        lc = closed[-1]
        if av > 0 and ab > 0:
            print(f"[{name}] {source} | শেষ ক্যান্ডেল {lc['t']:%H:%M} UTC | "
                  f"ভলিউম {lc['v'] / av:.1f}x (লাগবে {VOL_MULT}x) | "
                  f"বডি {abs(lc['c'] - lc['o']) / ab:.1f}x (লাগবে {BODY_MULT}x)")

        start = max(LOOKBACK, len(closed) - CHECK_LAST)
        for idx in range(start, len(closed)):
            res = analyze(closed, idx)
            if not res:
                continue
            key = f"{name}_{res['kind']}"
            ct = res["candle"]["t"]
            last = state.get(key)
            if last:
                last_t = datetime.fromisoformat(last)
                if ct <= last_t or (ct - last_t) < timedelta(minutes=COOLDOWN_MIN):
                    continue
            try:
                send_telegram(build_message(name, res, source))
                state[key] = ct.isoformat()
                changed = True
                print(f"[{name}] {res['kind']} অ্যালার্ট পাঠানো হয়েছে @ {ct}")
            except Exception as e:
                print(f"[{name}] টেলিগ্রাম ফেইল: {e}")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
