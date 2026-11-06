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
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "Twelve Data error"))
    if not isinstance(data["values"], list):
        raise RuntimeError("Twelve Data returned an invalid candle list")
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
    r.raise_for_status()
    payload = r.json()
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance returned no chart data")
    res = results[0]
    ts = res.get("timestamp") or []
    quotes = (res.get("indicators") or {}).get("quote") or []
    if not ts or not quotes:
        raise RuntimeError("Yahoo Finance returned incomplete candle data")
    q = quotes[0]
    out = []
    for i, t in enumerate(ts):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        volume_values = q.get("volume") or []
        volume = volume_values[i] if i < len(volume_values) else 0
        out.append({
            "t": datetime.fromtimestamp(t, tz=timezone.utc),
            "o": float(q["open"][i]), "h": float(q["high"][i]),
            "l": float(q["low"][i]), "c": float(q["close"][i]),
            "v": float(volume or 0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def has_volume(candles):
    """ভলিউম ফিল্ড আছে এবং সাম্প্রতিক পর্যবেক্ষণে অন্তত কিছু non-zero volume আছে কি না।"""
    recent = candles[-(LOOKBACK + 2):]
    return (
        len(recent) >= LOOKBACK + 2
        and any(c.get("v", 0) > 0 for c in recent)
    )


def clean_candles(candles):
    """ভাঙা/ডুপ্লিকেট ক্যান্ডেল বাদ দিয়ে সময় অনুযায়ী সাজায়।"""
    unique = {}
    for candle in candles:
        try:
            t = candle["t"]
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            o, h, low, close = (float(candle[k]) for k in ("o", "h", "l", "c"))
            volume = max(0.0, float(candle.get("v") or 0))
            if not all(map(lambda x: x == x and abs(x) != float("inf"), (o, h, low, close, volume))):
                continue
            if min(o, h, low, close) <= 0:
                continue
            if h < max(o, close, low) or low > min(o, close, h):
                continue
            unique[t] = {"t": t, "o": o, "h": h, "l": low, "c": close, "v": volume}
        except (KeyError, TypeError, ValueError):
            continue
    return [unique[t] for t in sorted(unique)]


def get_candles(name, cfg):
    try:
        if not TD_KEY:
            raise RuntimeError("TWELVE_DATA_API_KEY সেট করা নেই")
        c = clean_candles(fetch_twelvedata(cfg["td"]))
        if has_volume(c):
            return c, "Twelve Data"
        print(f"[{name}] Twelve Data এ ভলিউম নেই, ব্যাকআপে যাচ্ছি")
    except Exception as e:
        print(f"[{name}] Twelve Data ফেইল: {e}")
    c = clean_candles(fetch_yahoo(cfg["yf"]))
    if has_volume(c):
        source = "Yahoo (ব্যাকআপ)"
        if name == "XAUUSD":
            source = "Yahoo GC=F Futures proxy (Spot volume নয়)"
        return c, source
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
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    """আংশিক লেখা এড়াতে atomic replace ব্যবহার করে।"""
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, STATE_FILE)


PRESETS = {
    "low": (3.0, 2.2, "কম অ্যালার্ট (কড়া)"),
    "mid": (2.0, 1.8, "মাঝারি"),
    "high": (1.5, 1.4, "বেশি অ্যালার্ট"),
}
MAIN_KB = {
    "keyboard": [
        ["📊 অবস্থা", "▶️ চালু / ⏸ বন্ধ"],
        ["⚙️ সেনসিটিভিটি", "🔕 মিউট"],
        ["🥇 সোনা অন/অফ", "₿ BTC অন/অফ"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def default_settings():
    return {"enabled": True, "mute_until": None, "level": "mid",
            "sym": {"XAUUSD": True, "BTCUSD": True}}


def apply_settings(cfg):
    global VOL_MULT, BODY_MULT
    VOL_MULT, BODY_MULT = PRESETS[cfg["level"]][0], PRESETS[cfg["level"]][1]


def tg(method, **payload):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
            json=payload, timeout=30
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok", False):
            raise RuntimeError(result.get("description", f"Telegram {method} failed"))
        return result
    except Exception as e:
        print(f"Telegram {method} error:", e)
        return {}


def reply(text, markup=None):
    p = {"chat_id": TG_CHAT, "text": text}
    if markup:
        p["reply_markup"] = markup
    tg("sendMessage", **p)


def latest_stats(name):
    candles, source = get_candles(name, SYMBOLS[name])
    now = datetime.now(timezone.utc)
    closed = [c for c in candles if c["t"] + timedelta(minutes=INTERVAL_MIN) <= now]
    if len(closed) < LOOKBACK + 1:
        return f"পর্যাপ্ত ক্লোজড ক্যান্ডেল নেই ({source})"
    b = closed[-LOOKBACK - 1:-1]
    av = mean([x["v"] for x in b])
    ab = mean([abs(x["c"] - x["o"]) for x in b])
    lc = closed[-1]
    vol_text = f"{lc['v'] / av:.1f}x" if av > 0 else "N/A"
    body_text = f"{abs(lc['c'] - lc['o']) / ab:.1f}x" if ab > 0 else "N/A"
    return f"দাম {lc['c']:.2f} | ভলিউম {vol_text} | বডি {body_text} ({source})"


def status_text(cfg):
    now = datetime.now(timezone.utc)
    state = "▶️ চালু" if cfg["enabled"] else "⏸ বন্ধ"
    mu = cfg.get("mute_until")
    if mu and now < datetime.fromisoformat(mu):
        left = int((datetime.fromisoformat(mu) - now).total_seconds() // 60) + 1
        state += f" (🔕 মিউট, আরও ~{left} মিনিট)"
    lines = [f"বট: {state}",
             f"সেনসিটিভিটি: {PRESETS[cfg['level']][2]} "
             f"(ভলিউম {VOL_MULT}x, বডি {BODY_MULT}x লাগবে)", ""]
    for name in SYMBOLS:
        on = "✅" if cfg["sym"].get(name, True) else "❌"
        try:
            lines.append(f"{on} {name}: {latest_stats(name)}")
        except Exception as e:
            lines.append(f"{on} {name}: ডেটা আনা যায়নি ({e})")
    return "\n".join(lines)


def handle_text(t, cfg):
    t = t.strip()
    if t.startswith("/start") or t.startswith("/menu"):
        reply("মেনু চালু হয়েছে 👇", MAIN_KB)
    elif "অবস্থা" in t or t.startswith("/status"):
        reply(status_text(cfg))
    elif "চালু /" in t:
        cfg["enabled"] = not cfg["enabled"]
        reply("▶️ অ্যালার্ট চালু করা হলো" if cfg["enabled"] else "⏸ অ্যালার্ট বন্ধ করা হলো")
    elif "সেনসিটিভিটি" in t:
        reply("কোন লেভেল চান?", {"inline_keyboard": [
            [{"text": "🔇 কম অ্যালার্ট (কড়া)", "callback_data": "lvl:low"}],
            [{"text": "⚖️ মাঝারি", "callback_data": "lvl:mid"}],
            [{"text": "🔔 বেশি অ্যালার্ট", "callback_data": "lvl:high"}]]})
    elif "মিউট" in t:
        reply("কতক্ষণ মিউট করবেন?", {"inline_keyboard": [
            [{"text": "৩০ মিনিট", "callback_data": "mute:30"},
             {"text": "১ ঘণ্টা", "callback_data": "mute:60"}],
            [{"text": "৪ ঘণ্টা", "callback_data": "mute:240"},
             {"text": "🔔 মিউট বন্ধ", "callback_data": "mute:0"}]]})
    elif "সোনা" in t or "BTC" in t:
        name = "XAUUSD" if "সোনা" in t else "BTCUSD"
        cfg["sym"][name] = not cfg["sym"].get(name, True)
        reply(f"{name} অ্যালার্ট: {'✅ অন' if cfg['sym'][name] else '❌ অফ'}")


def handle_callback(cb, cfg):
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    if data.startswith("lvl:"):
        cfg["level"] = data.split(":")[1]
        apply_settings(cfg)
        reply(f"⚙️ সেনসিটিভিটি: {PRESETS[cfg['level']][2]} "
              f"(ভলিউম {VOL_MULT}x, বডি {BODY_MULT}x)")
    elif data.startswith("mute:"):
        mins = int(data.split(":")[1])
        if mins == 0:
            cfg["mute_until"] = None
            reply("🔔 মিউট বন্ধ, অ্যালার্ট আবার আসবে")
        else:
            cfg["mute_until"] = (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()
            reply(f"🔕 {mins} মিনিটের জন্য মিউট")


def process_updates(state):
    """মেনুর চাপ/মেসেজ পড়ে সেটিংস বদলায়। কিছু বদলালে True"""
    cfg = state.setdefault("settings", default_settings())
    apply_settings(cfg)
    r = tg("getUpdates", offset=state.get("tg_offset"), timeout=0,
           allowed_updates=["message", "callback_query"])
    changed = False
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        changed = True
        msg, cb = u.get("message"), u.get("callback_query")
        if msg and str(msg["chat"]["id"]) == str(TG_CHAT) and "text" in msg:
            handle_text(msg["text"], cfg)
        elif cb and str(cb["message"]["chat"]["id"]) == str(TG_CHAT):
            handle_callback(cb, cfg)
    return changed


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
    tg("deleteWebhook")
    changed = process_updates(state)
    settings = state.setdefault("settings", default_settings())
    settings.setdefault("enabled", True)
    settings.setdefault("mute_until", None)
    settings.setdefault("level", "mid")
    if settings["level"] not in PRESETS:
        settings["level"] = "mid"
    settings.setdefault("sym", {})
    for symbol_name in SYMBOLS:
        settings["sym"].setdefault(symbol_name, True)
    apply_settings(settings)
    mu = settings.get("mute_until")
    paused = (not settings["enabled"]) or bool(mu and now < datetime.fromisoformat(mu))

    for name, cfg in SYMBOLS.items():
        if paused or not settings["sym"].get(name, True):
            continue
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

    # প্রতিটি রানেই settings/Telegram offset লিখে রাখি; শুধু alert হলে নয়।
    save_state(state)


if __name__ == "__main__":
    main()
