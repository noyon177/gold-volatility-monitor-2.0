#!/usr/bin/env python3
"""
Volume Spike Bot  (v1)
----------------------
বাজারে ভলিউম হঠাৎ বাড়লে (volume spike) নিজে থেকে ধরে Telegram-এ সিগন্যাল পাঠায়।

ডেটা সোর্স:
  - BTCUSD  -> Kraken (প্রাইমারি), Coinbase (ফলব্যাক), Binance (শেষ ভরসা)
               [আসল ভলিউম, API key লাগে না; Binance US সার্ভারে ব্লকড]
  - XAUUSD  -> Twelve Data (XAU/USD)           [TWELVEDATA_API_KEY লাগবে]
      * স্পট গোল্ডে অনেক সময় ভলিউম থাকে না। সেক্ষেত্রে বট স্বয়ংক্রিয়ভাবে
        "রেঞ্জ-অ্যাক্টিভিটি" (ক্যান্ডেলের সাইজ বনাম গড়) দিয়ে কাজ চালায়
        এবং মেসেজে স্পষ্ট লিখে দেয়।

ডিটেকশন লজিক (শুধু CLOSED ক্যান্ডেলে):
  1. Relative Volume (RVOL) = বর্তমান ভলিউম / গত N ক্যান্ডেলের গড়
  2. Z-score = (ভলিউম - গড়) / স্ট্যান্ডার্ড ডেভিয়েশন
  3. RVOL >= RVOL_MIN এবং Z >= Z_MIN হলে স্পাইক
  4. ক্যান্ডেলের দিক (bullish/bearish), বডি রেশিও ও ক্লোজ পজিশন দেখে
     ধারণা (bias) দেয়: ব্রেকআউট-পুশ / অ্যাবজর্পশন / ক্লাইম্যাক্স
  5. ATR দিয়ে একটি রেফারেন্স SL/TP লেভেল দেয় (সিদ্ধান্ত তোমার)

এটা ফিনান্সিয়াল অ্যাডভাইস নয়, অ্যালার্ট টুল।
"""

import os
import sys
import json
import time
import math
import argparse
from datetime import datetime, timezone

import requests

# ----------------------------- কনফিগ ---------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_API_KEY") or os.getenv("TWELVE_DATA_API_KEY", "")

TIMEFRAME = os.getenv("TIMEFRAME", "15min")          # 5min / 15min / 1h
LOOKBACK = int(os.getenv("VOL_LOOKBACK", "20"))      # গড়ের জন্য ক্যান্ডেল সংখ্যা
RVOL_MIN = float(os.getenv("RVOL_MIN", "2.0"))       # গড়ের কত গুণ হলে স্পাইক
Z_MIN = float(os.getenv("Z_MIN", "2.0"))             # z-score সর্বনিম্ন
ATR_PERIOD = 14
SL_ATR = float(os.getenv("SL_ATR", "1.0"))
TP_RR = float(os.getenv("TP_RR", "2.0"))
STATE_FILE = os.getenv("STATE_FILE", "volume_state.json")

# প্রতিটি সিম্বলের জন্য সোর্সের তালিকা: প্রথমটা ফেইল করলে পরেরটা চেষ্টা করবে।
# Binance GitHub-এর US সার্ভার থেকে ব্লকড (HTTP 451), তাই শেষে রাখা।
SYMBOLS = [
    {
        "name": "BTCUSD",
        "sources": [
            ("kraken", "XBTUSD"),
            ("coinbase", "BTC-USD"),
            ("binance", "BTCUSDT"),
        ],
    },
    {"name": "XAUUSD", "sources": [("twelvedata", "XAU/USD")]},
]

TF_MAP_BINANCE = {"1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "1h": "1h"}
TF_MAP_KRAKEN = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1h": 60}
TF_MAP_COINBASE = {"1min": 60, "5min": 300, "15min": 900, "1h": 3600}
TF_SECONDS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800, "1h": 3600}


# ----------------------------- ডেটা ফেচ -------------------------------
def fetch_binance(code, tf, limit=120):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": code, "interval": TF_MAP_BINANCE[tf], "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for k in r.json():
        out.append(
            {
                "t": int(k[0]) // 1000,          # open time (sec)
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            }
        )
    return out


def fetch_twelvedata(code, tf, limit=120):
    if not TWELVEDATA_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY সেট করা নেই")
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": code,
            "interval": tf,
            "outputsize": limit,
            "apikey": TWELVEDATA_KEY,
            "timezone": "UTC",
            "order": "ASC",
        },
        timeout=20,
    )
    r.raise_for_status()
    js = r.json()
    if js.get("status") == "error" or "values" not in js:
        raise RuntimeError(f"TwelveData error: {js.get('message', js)}")
    out = []
    for row in js["values"]:
        dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        out.append(
            {
                "t": int(dt.timestamp()),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row.get("volume") or 0.0),
            }
        )
    return out


def parse_kraken(js):
    if js.get("error"):
        raise RuntimeError(f"Kraken error: {js['error']}")
    result = js["result"]
    key = next(k for k in result if k != "last")
    # row: [time, open, high, low, close, vwap, volume, count]
    return [
        {
            "t": int(r[0]),
            "o": float(r[1]),
            "h": float(r[2]),
            "l": float(r[3]),
            "c": float(r[4]),
            "v": float(r[6]),
        }
        for r in result[key]
    ]


def fetch_kraken(code, tf):
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": code, "interval": TF_MAP_KRAKEN[tf]},
        timeout=20,
    )
    r.raise_for_status()
    return parse_kraken(r.json())


def parse_coinbase(rows):
    # row: [time, low, high, open, close, volume], নতুন থেকে পুরোনো ক্রমে আসে
    out = [
        {
            "t": int(r[0]),
            "o": float(r[3]),
            "h": float(r[2]),
            "l": float(r[1]),
            "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in rows
    ]
    return sorted(out, key=lambda c: c["t"])


def fetch_coinbase(code, tf):
    if tf not in TF_MAP_COINBASE:
        raise RuntimeError(f"Coinbase {tf} সাপোর্ট করে না")
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{code}/candles",
        params={"granularity": TF_MAP_COINBASE[tf]},
        headers={"User-Agent": "volume-spike-bot"},
        timeout=20,
    )
    r.raise_for_status()
    return parse_coinbase(r.json())


FETCHERS = {
    "kraken": fetch_kraken,
    "coinbase": fetch_coinbase,
    "binance": fetch_binance,
    "twelvedata": fetch_twelvedata,
}


def fetch(sym, tf):
    """সোর্সগুলো ক্রমানুসারে চেষ্টা করে; প্রথম যেটা কাজ করে সেটার ডেটা ফেরত দেয়।"""
    errors = []
    for source, code in sym["sources"]:
        try:
            candles = FETCHERS[source](code, tf)
            if candles:
                return candles
            errors.append(f"{source}: খালি ডেটা")
        except Exception as e:
            errors.append(f"{source}: {e}")
            print(f"[{sym['name']}] {source} ব্যর্থ -> {e}")
    raise RuntimeError("সব সোর্স ব্যর্থ: " + " | ".join(errors))


# ----------------------------- ইন্ডিকেটর ------------------------------
def only_closed(candles, tf, now=None):
    """এখনো চলমান (forming) ক্যান্ডেল বাদ দেয়।"""
    now = now or time.time()
    step = TF_SECONDS[tf]
    return [c for c in candles if c["t"] + step <= now]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def atr(candles, period=ATR_PERIOD):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs[-period:])


def analyze(candles, lookback=LOOKBACK, rvol_min=RVOL_MIN, z_min=Z_MIN):
    """সর্বশেষ closed ক্যান্ডেল বিশ্লেষণ। স্পাইক হলে dict, না হলে None।"""
    if len(candles) < lookback + 2:
        return None

    last = candles[-1]
    prev = candles[-1 - lookback : -1]

    has_volume = sum(c["v"] for c in prev) > 0 and last["v"] > 0
    if has_volume:
        series_last = last["v"]
        series_prev = [c["v"] for c in prev]
        metric = "volume"
    else:
        # ভলিউম নেই -> রেঞ্জ-অ্যাক্টিভিটি প্রক্সি
        series_last = last["h"] - last["l"]
        series_prev = [c["h"] - c["l"] for c in prev]
        metric = "range"

    avg = mean(series_prev)
    sd = stdev(series_prev)
    if avg <= 0:
        return None
    rvol = series_last / avg
    z = (series_last - avg) / sd if sd > 0 else 0.0

    if not (rvol >= rvol_min and z >= z_min):
        return None

    rng = last["h"] - last["l"]
    body = abs(last["c"] - last["o"])
    body_ratio = body / rng if rng > 0 else 0.0
    close_pos = (last["c"] - last["l"]) / rng if rng > 0 else 0.5
    bullish = last["c"] > last["o"]

    # ক্যান্ডেল টাইপ ও bias
    if body_ratio >= 0.6 and bullish and close_pos >= 0.7:
        kind, bias = "Strong Bullish Push", "BUY-bias"
    elif body_ratio >= 0.6 and (not bullish) and close_pos <= 0.3:
        kind, bias = "Strong Bearish Push", "SELL-bias"
    elif body_ratio < 0.35 and close_pos >= 0.6:
        kind, bias = "Buying Absorption (নিচে কেনা)", "BUY-bias (নিশ্চিতকরণ দরকার)"
    elif body_ratio < 0.35 and close_pos <= 0.4:
        kind, bias = "Selling Absorption (উপরে বিক্রি)", "SELL-bias (নিশ্চিতকরণ দরকার)"
    else:
        kind, bias = "Mixed / Wide-range spike", "NEUTRAL (অপেক্ষা করো)"

    a = atr(candles)
    sl = tp = None
    if "BUY" in bias:
        sl = last["c"] - SL_ATR * a
        tp = last["c"] + SL_ATR * a * TP_RR
    elif "SELL" in bias:
        sl = last["c"] + SL_ATR * a
        tp = last["c"] - SL_ATR * a * TP_RR

    return {
        "t": last["t"],
        "close": last["c"],
        "high": last["h"],
        "low": last["l"],
        "metric": metric,
        "value": series_last,
        "avg": avg,
        "rvol": rvol,
        "z": z,
        "kind": kind,
        "bias": bias,
        "atr": a,
        "sl": sl,
        "tp": tp,
    }


# ----------------------------- স্টেট ---------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[warn] state save failed: {e}")


# ----------------------------- Telegram -------------------------------
def fmt_price(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:.4f}"


def build_message(sym, tf, res):
    ts = datetime.fromtimestamp(res["t"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note = ""
    if res["metric"] == "range":
        note = "\n⚠️ এই সোর্সে ভলিউম নেই, তাই ক্যান্ডেল-রেঞ্জ অ্যাক্টিভিটি দিয়ে স্পাইক ধরা হয়েছে।"
    lines = [
        f"🔊 VOLUME SPIKE — {sym} ({tf})",
        f"ক্যান্ডেল: {ts}",
        f"টাইপ: {res['kind']}",
        f"Bias: {res['bias']}",
        f"RVOL: {res['rvol']:.2f}x   Z-score: {res['z']:.2f}",
        f"Close: {fmt_price(res['close'])}   ATR: {fmt_price(res['atr'])}",
    ]
    if res["sl"] is not None:
        lines.append(
            f"রেফারেন্স SL: {fmt_price(res['sl'])}   TP ({TP_RR:.0f}R): {fmt_price(res['tp'])}"
        )
    lines.append(note.strip()) if note else None
    lines.append("📌 এটা অ্যালার্ট, এন্ট্রি নয়। চার্টে স্ট্রাকচার/লেভেল দেখে সিদ্ধান্ত নাও।")
    return "\n".join(l for l in lines if l)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram skipped: token/chat id নেই]\n" + text)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    ok = r.status_code == 200
    if not ok:
        print(f"[telegram error] {r.status_code} {r.text}")
    return ok


# ----------------------------- মেইন ----------------------------------
def run_once(dry=False):
    state = load_state()
    sent = 0
    failed = 0
    for sym in SYMBOLS:
        name = sym["name"]
        try:
            candles = only_closed(fetch(sym, TIMEFRAME), TIMEFRAME)
            res = analyze(candles)
        except Exception as e:
            print(f"[{name}] error: {e}")
            failed += 1
            continue

        if not res:
            print(f"[{name}] স্পাইক নেই")
            continue

        key = f"{name}_{TIMEFRAME}"
        if state.get(key) == res["t"]:
            print(f"[{name}] এই ক্যান্ডেলের অ্যালার্ট আগেই গেছে, স্কিপ")
            continue

        msg = build_message(name, TIMEFRAME, res)
        print(msg)
        if dry or send_telegram(msg):
            state[key] = res["t"]
            sent += 1
    save_state(state)
    print(f"done. alerts sent: {sent}, failed symbols: {failed}")
    if failed == len(SYMBOLS):
        # সব সিম্বল ফেইল করলে job লাল হবে, যাতে চুপচাপ ব্যর্থ না হয়
        sys.exit(1)


# ----------------------------- সেলফটেস্ট -------------------------------
def selftest():
    """সিনথেটিক ডেটায় লজিক যাচাই (নেটওয়ার্ক লাগে না)।"""
    import random

    random.seed(7)
    step = TF_SECONDS[TIMEFRAME]
    base_t = int(time.time()) - 200 * step
    price = 100.0
    candles = []
    for i in range(60):
        o = price
        c = o + random.uniform(-0.3, 0.3)
        h = max(o, c) + random.uniform(0, 0.15)
        l = min(o, c) - random.uniform(0, 0.15)
        v = random.uniform(900, 1100)
        candles.append({"t": base_t + i * step, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c

    # 1) স্বাভাবিক ডেটা -> স্পাইক থাকা উচিত না
    assert analyze(candles) is None, "স্বাভাবিক ডেটায় ভুল অ্যালার্ট"
    print("✔ স্বাভাবিক ডেটায় অ্যালার্ট নেই")

    # 2) বুলিশ স্পাইক ক্যান্ডেল
    o = price
    c = o + 1.2
    candles.append({"t": base_t + 60 * step, "o": o, "h": c + 0.05, "l": o - 0.05, "c": c, "v": 5200})
    r = analyze(candles)
    assert r and "BUY" in r["bias"] and r["rvol"] > 3, f"বুলিশ স্পাইক ধরা পড়েনি: {r}"
    print(f"✔ বুলিশ স্পাইক ধরা পড়েছে: RVOL {r['rvol']:.1f}x, {r['kind']}")
    print(build_message("TEST", TIMEFRAME, r))

    # 3) বেয়ারিশ স্পাইক
    o = candles[-1]["c"]
    c = o - 1.3
    candles.append({"t": base_t + 61 * step, "o": o, "h": o + 0.05, "l": c - 0.05, "c": c, "v": 6100})
    r = analyze(candles)
    assert r and "SELL" in r["bias"], f"বেয়ারিশ স্পাইক ধরা পড়েনি: {r}"
    print(f"✔ বেয়ারিশ স্পাইক ধরা পড়েছে: RVOL {r['rvol']:.1f}x, {r['kind']}")

    # 4) ভলিউম নেই (গোল্ড-স্টাইল) -> রেঞ্জ প্রক্সি
    nov = [dict(c, v=0.0) for c in candles]
    r = analyze(nov)
    assert r and r["metric"] == "range", "রেঞ্জ ফলব্যাক কাজ করেনি"
    print("✔ ভলিউম-হীন ডেটায় রেঞ্জ ফলব্যাক কাজ করছে")

    # 5) ফর্মিং ক্যান্ডেল বাদ
    forming = candles + [{"t": int(time.time()) - 10, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9e9}]
    assert len(only_closed(forming, TIMEFRAME)) == len(candles), "ফর্মিং ক্যান্ডেল বাদ যায়নি"
    print("✔ চলমান ক্যান্ডেল বাদ যাচ্ছে")

    # 6) ডুপ্লিকেট প্রতিরোধ
    global STATE_FILE
    STATE_FILE = "/tmp/_vol_state_test.json"
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    st = {}
    key = "TEST_" + TIMEFRAME
    st[key] = r["t"]
    save_state(st)
    assert load_state().get(key) == r["t"], "স্টেট সেভ/লোড ব্যর্থ"
    print("✔ স্টেট (ডুপ্লিকেট রোধ) কাজ করছে")
    # 7) Kraken / Coinbase রেসপন্স পার্সিং (মক ডেটা)
    k = parse_kraken(
        {"error": [], "result": {"XXBTZUSD": [[1700000000, "100", "110", "95", "105", "102", "12.5", 40]], "last": 1}}
    )
    assert k[0] == {"t": 1700000000, "o": 100.0, "h": 110.0, "l": 95.0, "c": 105.0, "v": 12.5}, k
    cb = parse_coinbase([[1700000900, 96, 112, 105, 108, 7.0], [1700000000, 95, 110, 100, 105, 12.5]])
    assert cb[0]["t"] == 1700000000 and cb[0] == {"t": 1700000000, "o": 100.0, "h": 110.0, "l": 95.0, "c": 105.0, "v": 12.5}, cb
    assert cb[1]["t"] == 1700000900, "Coinbase সর্টিং ভুল"
    print("✔ Kraken/Coinbase পার্সিং ঠিক আছে")

    # 8) ফলব্যাক চেইন: প্রথম সোর্স ফেইল করলে পরেরটা নেয়
    FETCHERS["_bad"] = lambda code, tf: (_ for _ in ()).throw(RuntimeError("451"))
    FETCHERS["_good"] = lambda code, tf: [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]
    got = fetch({"name": "T", "sources": [("_bad", "x"), ("_good", "y")]}, TIMEFRAME)
    assert got and got[0]["t"] == 1, "ফলব্যাক কাজ করেনি"
    try:
        fetch({"name": "T", "sources": [("_bad", "x")]}, TIMEFRAME)
        raise AssertionError("সব সোর্স ফেইল করলে error আসা উচিত ছিল")
    except RuntimeError:
        pass
    print("✔ সোর্স ফলব্যাক চেইন কাজ করছে")
    print("\nসব টেস্ট পাস ✅")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="অফলাইন লজিক টেস্ট")
    ap.add_argument("--dry", action="store_true", help="Telegram ছাড়া শুধু প্রিন্ট")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run_once(dry=args.dry)
