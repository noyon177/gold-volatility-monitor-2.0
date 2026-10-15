#!/usr/bin/env python3
"""
Volume Spike Alert Bot
----------------------
শুধু একটাই কাজ: ভলিউম বাড়লে Telegram-এ মেসেজ।

- মেইন ডাটা: Twelve Data   |  ব্যাকআপ: Alpha Vantage
- শুধু "বন্ধ হয়ে যাওয়া" (closed) ক্যান্ডেল বিশ্লেষণ করে (চলমান ক্যান্ডেলের ভুয়া ভলিউম ধরে না)
- বেসলাইন = গত LOOKBACK ক্যান্ডেলের গড় ভলিউম; স্পাইক = ratio + z-score দুটোই পার হলে
- একই ক্যান্ডেলের জন্য দ্বিতীয়বার অ্যালার্ট পাঠায় না (state.json)

ব্যবহার:
    python volume_spike_bot.py          # স্বাভাবিক রান
    python volume_spike_bot.py --test   # শুধু টেস্ট মেসেজ পাঠাবে
"""

import os
import sys
import json
import time
import statistics
from datetime import datetime, timedelta, timezone

import requests

# ───────────────────────── কনফিগ ─────────────────────────
TD_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
AV_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

INTERVAL = os.getenv("INTERVAL", "5min")          # 1min / 5min / 15min
INTERVAL_MIN = int(INTERVAL.replace("min", ""))
LOOKBACK = int(os.getenv("LOOKBACK", "20"))        # বেসলাইনের জন্য কতগুলো ক্যান্ডেল
OUTPUTSIZE = LOOKBACK + 20

SPIKE_RATIO = float(os.getenv("SPIKE_RATIO", "2.0"))   # গড়ের কত গুণ
SPIKE_Z = float(os.getenv("SPIKE_Z", "2.0"))           # z-score সীমা
BIG_RATIO = 3.0
HUGE_RATIO = 4.5
BUILDUP_RATIO = float(os.getenv("BUILDUP_RATIO", "1.6"))

FRESH_MIN = 12          # ক্যান্ডেল বন্ধ হওয়ার কত মিনিটের মধ্যে অ্যালার্ট দেবে
ERR_THROTTLE_H = 3      # ডাটা-এরর মেসেজ কত ঘণ্টায় একবার
LOCAL_OFFSET = float(os.getenv("LOCAL_TZ_OFFSET", "6"))   # আপনার টাইমজোন (BD = +6)
LOCAL_LABEL = os.getenv("LOCAL_TZ_LABEL", "BD")
STATE_FILE = "state.json"

SYMBOLS = {
    "BTC/USD": {"always_open": True, "av": ("crypto", "BTC", "USD")},
    "XAU/USD": {"always_open": False, "av": ("fx", "XAU", "USD")},
}


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ───────────────────────── স্টেট ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("sent", {})
    s.setdefault("err", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ───────────────────────── Telegram ─────────────────────────
def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for i in range(3):
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TG_CHAT,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.ok:
                return True
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            log(f"Telegram request failed: {e}")
        time.sleep(2 * (i + 1))
    return False


# ───────────────────────── মার্কেট টাইম ─────────────────────────
def gold_closed(now):
    """সোনা: শুক্র 22:00 UTC থেকে রবি 22:00 UTC পর্যন্ত বন্ধ (আনুমানিক)।"""
    wd, h = now.weekday(), now.hour
    return wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 22)


# ───────────────────────── ডাটা সোর্স ─────────────────────────
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_twelvedata(symbol):
    if not TD_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY নেই")
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": INTERVAL,
            "outputsize": OUTPUTSIZE,
            "timezone": "UTC",
            "apikey": TD_KEY,
        },
        timeout=20,
    )
    j = r.json()
    if j.get("status") == "error" or "values" not in j:
        raise RuntimeError(f"TwelveData: {j.get('message', 'bad response')}")
    out = []
    for v in j["values"]:
        t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({
            "t": t, "o": float(v["open"]), "h": float(v["high"]),
            "l": float(v["low"]), "c": float(v["close"]), "v": _num(v.get("volume")),
        })
    out.sort(key=lambda c: c["t"])
    return out


def fetch_alphavantage(cfg):
    if not AV_KEY:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY নেই")
    kind, a, b = cfg["av"]
    if kind == "crypto":
        params = {"function": "CRYPTO_INTRADAY", "symbol": a, "market": b,
                  "interval": INTERVAL, "outputsize": "compact", "apikey": AV_KEY}
        key = f"Time Series Crypto ({INTERVAL})"
    else:
        params = {"function": "FX_INTRADAY", "from_symbol": a, "to_symbol": b,
                  "interval": INTERVAL, "outputsize": "compact", "apikey": AV_KEY}
        key = f"Time Series FX ({INTERVAL})"
    j = requests.get("https://www.alphavantage.co/query", params=params, timeout=25).json()
    ts = j.get(key)
    if not ts:
        raise RuntimeError(f"AlphaVantage: {str(j)[:150]}")
    out = []
    for k, v in ts.items():
        t = datetime.strptime(k[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({
            "t": t, "o": float(v["1. open"]), "h": float(v["2. high"]),
            "l": float(v["3. low"]), "c": float(v["4. close"]), "v": _num(v.get("5. volume")),
        })
    out.sort(key=lambda c: c["t"])
    return out


def get_candles(symbol, cfg):
    """(candles, source) ফেরত দেয়। প্রথমে Twelve Data, ব্যর্থ হলে Alpha Vantage।"""
    last_err = None
    for attempt in range(2):
        try:
            return fetch_twelvedata(symbol), "Twelve Data"
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"{symbol} TD attempt {attempt + 1} failed: {e}")
            time.sleep(3)
    try:
        log(f"{symbol}: ব্যাকআপ সোর্সে (Alpha Vantage) যাচ্ছি")
        return fetch_alphavantage(cfg), "Alpha Vantage (ব্যাকআপ)"
    except Exception as e:  # noqa: BLE001
        log(f"{symbol} AV failed: {e}")
        raise RuntimeError(f"TD: {last_err} | AV: {e}")


# ───────────────────────── বিশ্লেষণ ─────────────────────────
def analyze(candles, now):
    closed = [c for c in candles if c["t"] + timedelta(minutes=INTERVAL_MIN) <= now]
    if len(closed) < LOOKBACK + 1:
        return None
    last = closed[-1]
    base = closed[-(LOOKBACK + 1):-1]

    # আসল ভলিউম আছে কি? (ফরেক্স/সোনায় অনেক সময় থাকে না)
    good = sum(1 for c in base if c["v"] and c["v"] > 0)
    has_volume = last["v"] is not None and good >= LOOKBACK * 0.7

    if has_volume:
        series = [(c["v"] or 0.0) for c in closed]
        mode = "volume"
    else:
        series = [c["h"] - c["l"] for c in closed]   # প্রক্সি: ক্যান্ডেল রেঞ্জ
        mode = "proxy"

    cur = series[-1]
    base_s = series[-(LOOKBACK + 1):-1]
    mean = statistics.fmean(base_s)
    sd = statistics.pstdev(base_s)
    if mean <= 0:
        return None
    ratio = cur / mean
    z = (cur - mean) / sd if sd > 0 else 0.0

    kind = None
    if ratio >= SPIKE_RATIO and z >= SPIKE_Z:
        kind = "spike"
    elif (len(series) >= 3 and series[-3] < series[-2] < series[-1]
          and ratio >= BUILDUP_RATIO):
        kind = "buildup"

    return {
        "kind": kind, "mode": mode, "candle": last, "cur": cur,
        "mean": mean, "ratio": ratio, "z": z,
    }


# ───────────────────────── মেসেজ ─────────────────────────
def fmt(x):
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 10:
        return f"{x:,.1f}"
    return f"{x:,.2f}"


def build_message(symbol, res, source):
    c = res["candle"]
    close_t = c["t"] + timedelta(minutes=INTERVAL_MIN)
    local_t = close_t + timedelta(hours=LOCAL_OFFSET)
    bull = c["c"] >= c["o"]
    chg = (c["c"] - c["o"]) / c["o"] * 100 if c["o"] else 0.0

    if res["kind"] == "spike":
        if res["ratio"] >= HUGE_RATIO:
            icon, title = "🚨", "অস্বাভাবিক ভলিউম"
        elif res["ratio"] >= BIG_RATIO:
            icon, title = "🔥", "বড় ভলিউম স্পাইক"
        else:
            icon, title = "⚡", "ভলিউম স্পাইক"
    else:
        icon, title = "📈", "ভলিউম ধাপে ধাপে বাড়ছে"

    label = "ভলিউম" if res["mode"] == "volume" else "অ্যাক্টিভিটি (রেঞ্জ)"
    lines = [
        f"{icon} <b>{title} — {symbol}</b>",
        f"⏱ {INTERVAL} | ক্যান্ডেল বন্ধ: {close_t:%H:%M} UTC ({local_t:%H:%M} {LOCAL_LABEL})",
        "",
        f"📊 {label}: <b>{fmt(res['cur'])}</b>  (গড়ের <b>{res['ratio']:.1f}x</b>)",
        f"▫️ গড় (গত {LOOKBACK} ক্যান্ডেল): {fmt(res['mean'])}",
        f"▫️ z-score: {res['z']:.1f}",
        f"{'🟢 বুলিশ' if bull else '🔴 বেয়ারিশ'} ক্যান্ডেল ({chg:+.2f}%)",
        f"💰 ক্লোজ: {c['c']:,.2f}",
    ]
    if res["mode"] == "proxy":
        lines += ["", "⚠️ এই ফিডে আসল ভলিউম নেই; ক্যান্ডেল রেঞ্জ দিয়ে অনুমান করা হয়েছে।"]
    if "ব্যাকআপ" in source:
        lines += ["", f"ℹ️ সোর্স: {source}"]
    return "\n".join(lines)


# ───────────────────────── মেইন ─────────────────────────
def main():
    if not TG_TOKEN or not TG_CHAT:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID সেট নেই")
        sys.exit(1)

    if "--test" in sys.argv:
        ok = tg_send(
            "✅ <b>Volume Spike Bot চালু আছে</b>\n"
            f"সিম্বল: {', '.join(SYMBOLS)} | টাইমফ্রেম: {INTERVAL}\n"
            f"স্পাইক শর্ত: গড়ের {SPIKE_RATIO}x এবং z ≥ {SPIKE_Z}"
        )
        log("টেস্ট মেসেজ পাঠানো হয়েছে" if ok else "টেস্ট মেসেজ ব্যর্থ")
        sys.exit(0 if ok else 1)

    state = load_state()
    now = datetime.now(timezone.utc)

    for symbol, cfg in SYMBOLS.items():
        try:
            if not cfg["always_open"] and gold_closed(now):
                log(f"{symbol}: মার্কেট বন্ধ, স্কিপ")
                continue

            candles, source = get_candles(symbol, cfg)
            res = analyze(candles, now)
            if not res:
                log(f"{symbol}: পর্যাপ্ত ডাটা নেই")
                continue

            c = res["candle"]
            log(f"{symbol} [{source}] mode={res['mode']} ratio={res['ratio']:.2f} "
                f"z={res['z']:.2f} kind={res['kind']}")
            state["err"].pop(symbol, None)

            if not res["kind"]:
                continue

            close_t = c["t"] + timedelta(minutes=INTERVAL_MIN)
            if (now - close_t) > timedelta(minutes=FRESH_MIN):
                log(f"{symbol}: ক্যান্ডেল পুরোনো, অ্যালার্ট স্কিপ")
                continue
            if state["sent"].get(symbol) == c["t"].isoformat():
                log(f"{symbol}: এই ক্যান্ডেলের অ্যালার্ট আগেই গেছে")
                continue

            if tg_send(build_message(symbol, res, source)):
                state["sent"][symbol] = c["t"].isoformat()
                log(f"{symbol}: অ্যালার্ট পাঠানো হয়েছে")

        except Exception as e:  # noqa: BLE001
            log(f"{symbol} ERROR: {e}")
            last = state["err"].get(symbol)
            due = True
            if last:
                due = (now - datetime.fromisoformat(last)) > timedelta(hours=ERR_THROTTLE_H)
            if due and tg_send(f"⚠️ <b>ডাটা সমস্যা — {symbol}</b>\nদুটো সোর্সই ব্যর্থ। বট চেক করুন।\n<code>{str(e)[:200]}</code>"):
                state["err"][symbol] = now.isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
