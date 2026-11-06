#!/usr/bin/env python3
"""
টেলিগ্রাম মেনু বাটন সহ মোমেন্টাম/প্যানিক অ্যালার্ট বট (সবসময় চালু থাকার জন্য)
momentum_panic_alert.py এর পাশে রাখতে হবে (ওটার লজিক ব্যবহার করে)।
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask

import momentum_panic_alert as mpa

TOKEN = mpa.TG_TOKEN
CHAT = str(mpa.TG_CHAT)
API = f"https://api.telegram.org/bot{TOKEN}"

PRESETS = {
    "low": (3.0, 2.2, "কম অ্যালার্ট (কড়া)"),
    "mid": (2.0, 1.8, "মাঝারি"),
    "high": (1.5, 1.4, "বেশি অ্যালার্ট"),
}
S = {
    "enabled": True,
    "mute_until": None,
    "sym": {"XAUUSD": True, "BTCUSD": True},
    "level": "mid",
    "last_alert": {},
}
lock = threading.Lock()


# ---------------- টেলিগ্রাম ----------------
def tg(method, **payload):
    try:
        return requests.post(f"{API}/{method}", json=payload, timeout=60).json()
    except Exception as e:
        print("tg error:", e)
        return {}


MAIN_KB = {
    "keyboard": [
        ["📊 অবস্থা", "▶️ চালু / ⏸ বন্ধ"],
        ["⚙️ সেনসিটিভিটি", "🔕 মিউট"],
        ["🥇 সোনা অন/অফ", "₿ BTC অন/অফ"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def send(text, markup=None):
    p = {"chat_id": CHAT, "text": text}
    if markup:
        p["reply_markup"] = markup
    tg("sendMessage", **p)


def apply_level(level):
    vol, body, _ = PRESETS[level]
    mpa.VOL_MULT, mpa.BODY_MULT = vol, body
    S["level"] = level


def muted():
    mu = S["mute_until"]
    return bool(mu and datetime.now(timezone.utc) < mu)


# ---------------- স্ট্যাটাস ----------------
def latest_stats(name):
    cfg = mpa.SYMBOLS[name]
    candles, source = mpa.get_candles(name, cfg)
    now = datetime.now(timezone.utc)
    closed = [c for c in candles if c["t"] + timedelta(minutes=mpa.INTERVAL_MIN) <= now]
    b = closed[-mpa.LOOKBACK - 1:-1]
    av = mpa.mean([x["v"] for x in b])
    ab = mpa.mean([abs(x["c"] - x["o"]) for x in b])
    lc = closed[-1]
    return (
        f"দাম {lc['c']:.2f} | ভলিউম {lc['v'] / av:.1f}x | "
        f"বডি {abs(lc['c'] - lc['o']) / ab:.1f}x ({source})"
    )


def status_text():
    state = "▶️ চালু" if S["enabled"] else "⏸ বন্ধ"
    if muted():
        left = int((S["mute_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1
        state += f" (🔕 মিউট, আরও ~{left} মিনিট)"
    lines = [
        f"বট: {state}",
        f"সেনসিটিভিটি: {PRESETS[S['level']][2]} "
        f"(ভলিউম {mpa.VOL_MULT}x, বডি {mpa.BODY_MULT}x লাগবে)",
        "",
    ]
    for name in mpa.SYMBOLS:
        on = "✅" if S["sym"][name] else "❌"
        try:
            lines.append(f"{on} {name}: {latest_stats(name)}")
        except Exception as e:
            lines.append(f"{on} {name}: ডেটা আনা যায়নি ({e})")
    return "\n".join(lines)


# ---------------- বাটন হ্যান্ডলার ----------------
def handle_text(text):
    t = text.strip()
    with lock:
        if t.startswith("/start") or t.startswith("/menu"):
            send("মেনু চালু হয়েছে 👇", MAIN_KB)
        elif "অবস্থা" in t or t.startswith("/status"):
            send(status_text())
        elif "চালু /" in t:
            S["enabled"] = not S["enabled"]
            send("▶️ অ্যালার্ট চালু করা হলো" if S["enabled"] else "⏸ অ্যালার্ট বন্ধ করা হলো")
        elif "সেনসিটিভিটি" in t:
            send("কোন লেভেল চান?", {"inline_keyboard": [
                [{"text": "🔇 কম অ্যালার্ট (কড়া)", "callback_data": "lvl:low"}],
                [{"text": "⚖️ মাঝারি", "callback_data": "lvl:mid"}],
                [{"text": "🔔 বেশি অ্যালার্ট", "callback_data": "lvl:high"}],
            ]})
        elif "মিউট" in t:
            send("কতক্ষণ মিউট করবেন?", {"inline_keyboard": [
                [{"text": "৩০ মিনিট", "callback_data": "mute:30"},
                 {"text": "১ ঘণ্টা", "callback_data": "mute:60"}],
                [{"text": "৪ ঘণ্টা", "callback_data": "mute:240"},
                 {"text": "🔔 মিউট বন্ধ", "callback_data": "mute:0"}],
            ]})
        elif "সোনা" in t:
            toggle_symbol("XAUUSD")
        elif "BTC" in t:
            toggle_symbol("BTCUSD")


def toggle_symbol(name):
    S["sym"][name] = not S["sym"][name]
    send(f"{name} অ্যালার্ট: {'✅ অন' if S['sym'][name] else '❌ অফ'}")


def handle_callback(cb):
    data = cb.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    with lock:
        if data.startswith("lvl:"):
            level = data.split(":")[1]
            apply_level(level)
            send(f"⚙️ সেনসিটিভিটি: {PRESETS[level][2]} "
                 f"(ভলিউম {mpa.VOL_MULT}x, বডি {mpa.BODY_MULT}x)")
        elif data.startswith("mute:"):
            mins = int(data.split(":")[1])
            if mins == 0:
                S["mute_until"] = None
                send("🔔 মিউট বন্ধ, অ্যালার্ট আবার আসবে")
            else:
                S["mute_until"] = datetime.now(timezone.utc) + timedelta(minutes=mins)
                send(f"🔕 {mins} মিনিটের জন্য মিউট")


def poll_loop():
    offset = None
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=50,
                   allowed_updates=["message", "callback_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message")
                cb = u.get("callback_query")
                if msg and str(msg["chat"]["id"]) == CHAT and "text" in msg:
                    handle_text(msg["text"])
                elif cb and str(cb["message"]["chat"]["id"]) == CHAT:
                    handle_callback(cb)
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)


# ---------------- স্ক্যান ----------------
def scan_once():
    if not S["enabled"] or muted():
        return
    now = datetime.now(timezone.utc)
    for name, cfg in mpa.SYMBOLS.items():
        if not S["sym"][name]:
            continue
        try:
            candles, source = mpa.get_candles(name, cfg)
        except Exception as e:
            print(f"[{name}] ডেটা ফেইল: {e}")
            continue
        closed = [c for c in candles if c["t"] + timedelta(minutes=mpa.INTERVAL_MIN) <= now]
        if len(closed) < mpa.LOOKBACK + 1:
            continue
        age = now - (closed[-1]["t"] + timedelta(minutes=mpa.INTERVAL_MIN))
        if age > timedelta(minutes=mpa.MAX_AGE_MIN):
            continue
        start = max(mpa.LOOKBACK, len(closed) - mpa.CHECK_LAST)
        for idx in range(start, len(closed)):
            res = mpa.analyze(closed, idx)
            if not res:
                continue
            key = f"{name}_{res['kind']}"
            ct = res["candle"]["t"]
            last = S["last_alert"].get(key)
            if last and (ct <= last or (ct - last) < timedelta(minutes=mpa.COOLDOWN_MIN)):
                continue
            send(mpa.build_message(name, res, source))
            S["last_alert"][key] = ct
            print(f"[{name}] {res['kind']} অ্যালার্ট @ {ct}")


def scan_loop():
    while True:
        # পরের ৫ মিনিটের সীমা + ১৫ সেকেন্ড পর চেক
        now = time.time()
        wait = 300 - (now % 300) + 15
        time.sleep(wait)
        try:
            with lock:
                scan_once()
        except Exception as e:
            print("scan error:", e)


# ---------------- ওয়েব (জাগিয়ে রাখার জন্য) ----------------
app = Flask(__name__)


@app.route("/")
def home():
    return "ok"


if __name__ == "__main__":
    if not (TOKEN and CHAT):
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID সেট করা নেই")
    tg("deleteWebhook")
    apply_level("mid")
    send("🤖 বট চালু হয়েছে। মেনু দেখতে /menu লিখুন।", MAIN_KB)
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
