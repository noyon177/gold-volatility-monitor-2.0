"""গোল্ড (XAU) ও বিটকয়েন (BTC)-এর ভোলাটিলিটি অ্যালার্ট বট — উন্নত সংস্করণ।

শুধু ভোলাটিলিটি মাপে (কোনো ট্রেড সিগন্যাল বা সাপোর্ট/রেজিস্ট্যান্স নেই)।

তিনটা মেট্রিক:
- ATR   : সর্বশেষ ১-মিনিট ক্যান্ডেলের রেঞ্জ ÷ আগের LOOKBACK ক্যান্ডেলের "মিডিয়ান" রেঞ্জ।
- RANGE5: গত ৫ মিনিটের মোট রেঞ্জ ÷ আগের ৫-মিনিট রেঞ্জগুলোর মিডিয়ান
          (ধারাবাহিক টানা মুভ ধরে, যা এক ক্যান্ডেলে ধরা পড়ে না)।
- PVT   : ভলিউম × রিটার্ন (শুধু BTC, কারণ গোল্ড OTC বলে ভলিউম অনির্ভরযোগ্য)।

আগের সংস্করণ থেকে যা উন্নত হয়েছে:
- মিডিয়ান বেজলাইন: একটা বড় স্পাইক গড় নষ্ট করে না।
- স্টেট ফাইল (state.json): রানের মাঝেও cooldown/লেভেল মনে থাকে, তাই বারবার একই অ্যালার্ট আসে না।
- দুই লেভেল: 🟡 অস্থির, 🔴 খুবই অস্থির (লেভেল বাড়লে সাথে সাথে জানায়)।
- "শান্ত হয়েছে" মেসেজ: অস্থিরতা শেষ হলে জানায়।
- ডেটা-ডাউন ওয়ার্নিং: ডেটা না এলে চুপ না থেকে জানায়, ফিরলেও জানায়।
- গোল্ড রানে ২ বার, BTC প্রতি ২০ সেকেন্ডে (Kraken থেকে, ব্যাকআপ Coinbase)।
- API কী/টোকেন এরর মেসেজ থেকে মুছে ফেলা হয়।
"""
import datetime as dt
import json
import os
import statistics
import sys
import time

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

# ---------------- সেটিংস ----------------
LOOKBACK = 90            # বেজলাইনের জন্য আগের কতগুলো ১-মিনিট ক্যান্ডেল
RUN_SECONDS = 285        # এক রানে কতক্ষণ চলবে (cron ৫ মিনিট)
BTC_POLL = 20            # BTC কত সেকেন্ড পরপর
GOLD_POLL = 150          # গোল্ড কত সেকেন্ড পরপর (Twelve Data ফ্রি: দিনে ৮০০ ক্রেডিট)
COOLDOWN = 300           # নতুন অস্থিরতার অ্যালার্টের মাঝে ন্যূনতম বিরতি
REPEAT_EVERY = 900       # অস্থিরতা চলতে থাকলে কত সেকেন্ড পরপর আবার জানাবে
STRONG_MULT = 1.5        # থ্রেশহোল্ডের এতগুণ হলে 🔴 লেভেল
CALM_FRAC = 0.7          # থ্রেশহোল্ডের এই ভগ্নাংশের নিচে নামলে "শান্ত" ধরা হবে
CALM_HOLD = 300          # এতক্ষণ টানা শান্ত থাকলে "শান্ত হয়েছে" মেসেজ
MAX_STALE = 180          # ক্যান্ডেল এর চেয়ে পুরনো হলে (মার্কেট বন্ধ/ডেটা আটকে) অ্যালার্ট নয়
GAP_RESET = 300          # এর চেয়ে বড় ফাঁকের পর গ্যাপকে নড়াচড়া ধরা হবে না
HEARTBEAT_UTC_HOUR = 3   # ০৩:০০ UTC = সকাল ৯:০০ বাংলাদেশ
STATE_FILE = os.getenv("STATE_FILE", "state.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}
BD_TZ = dt.timezone(dt.timedelta(hours=6))
SECRETS = [TOKEN, TWELVE_DATA_KEY]

LABELS = {"atr": "১-মিনিট ATR", "r5": "৫-মিনিট রেঞ্জ", "pvt": "PVT"}


def safe(e):
    """এরর টেক্সট থেকে গোপন কী মুছে ফেলে (পাবলিক লগে যেন না যায়)।"""
    s = f"{type(e).__name__}: {e}"
    for x in SECRETS:
        if x:
            s = s.replace(x, "***")
    return s[:200]


def get_json(url, params=None, tries=3):
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(safe(err)) from None


# ---------------- ডেটা সোর্স ----------------
# ক্যান্ডেল ফরম্যাট: (time, open, high, low, close, volume), time = ক্যান্ডেল শুরুর UTC টাইমস্ট্যাম্প
def gold_candles():
    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": "XAU/USD",
            "interval": "1min",
            "outputsize": LOOKBACK + 15,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_KEY,
        },
        tries=2,
    )
    if data.get("status") == "error":
        raise RuntimeError("Twelve Data: " + str(data.get("message", "error"))[:150])
    rows = sorted(data["values"], key=lambda r: r["datetime"])
    out = []
    for r in rows:
        t = dt.datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        ).timestamp()
        out.append(
            (t, float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r.get("volume") or 0))
        )
    return out


def btc_kraken():
    # Kraken: প্রায় রিয়েল-টাইম, চলমান ক্যান্ডেলসহ। ফরম্যাট: [time, o, h, l, c, vwap, volume, count]
    data = get_json(
        "https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": 1}
    )
    if data.get("error"):
        raise RuntimeError("Kraken: " + str(data["error"])[:150])
    rows = next(v for k, v in data["result"].items() if k != "last")
    return [
        (float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6]))
        for r in rows
    ]


def btc_coinbase():
    # Coinbase ফরম্যাট: [time, low, high, open, close, volume]। এই ফিড কখনো ৩-৪ মিনিট পিছিয়ে থাকে।
    rows = get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        {"granularity": 60},
    )
    rows = sorted(rows, key=lambda x: x[0])
    return [(t, o, h, l, c, v) for t, l, h, o, c, v in rows]


def btc_candles():
    """আগে Kraken, না হলে Coinbase। যেটা তাজা (MAX_STALE-এর ভেতরে) সেটাই ব্যবহার হয়।"""
    best, last_err = None, None
    for src in (btc_kraken, btc_coinbase):
        try:
            c = src()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        if time.time() - c[-1][0] <= MAX_STALE:
            return c
        if best is None or c[-1][0] > best[-1][0]:
            best = c
    if best is None:
        raise RuntimeError(safe(last_err))
    return best


# th = থ্রেশহোল্ড (None মানে ওই মেট্রিক বন্ধ)। প্রথমে এই মান দিয়ে চালান, অ্যালার্ট বেশি/কম মনে হলে বদলান।
MARKETS = {
    "XAU": {
        "name": "গোল্ড (XAU)", "fetch": gold_candles, "poll": GOLD_POLL, "fail_limit": 4,
        "th": {"atr": 2.5, "r5": 2.0, "pvt": None},
    },
    "BTC": {
        "name": "বিটকয়েন (BTC)", "fetch": btc_candles, "poll": BTC_POLL, "fail_limit": 6,
        "th": {"atr": 2.5, "r5": 2.0, "pvt": 6.0},
    },
}


# ---------------- বিশ্লেষণ ----------------
def true_ranges(c):
    out = []
    for i, (t, _, h, l, _, _) in enumerate(c):
        if i == 0 or t - c[i - 1][0] > GAP_RESET:
            out.append(h - l)
        else:
            pc = c[i - 1][4]
            out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def pvt_series(c):
    out = [0.0]
    for i in range(1, len(c)):
        pc, cl, v = c[i - 1][4], c[i][4], c[i][5]
        out.append(abs(v * (cl - pc) / pc) if pc else 0.0)
    return out


def window_range(c, end, n=5):
    """end ইনডেক্সে শেষ হওয়া n ক্যান্ডেলের হাই-লো রেঞ্জ (ফাঁক থাকলে None)।"""
    w = c[end - n + 1:end + 1]
    if len(w) < n or w[-1][0] - w[0][0] > (n - 1) * 60 + 120:
        return None
    return max(x[2] for x in w) - min(x[3] for x in w)


def analyse(candles, use_pvt):
    n = len(candles)
    if n < LOOKBACK + 10:
        return None

    # শেষ দুই ক্যান্ডেল (শেষ সম্পূর্ণ + চলমান) দেখা হয়; চলমান ক্যান্ডেলের রেঞ্জ শুধু বাড়তে পারে,
    # তাই আগেভাগে ধরা যায়, ভুল অ্যালার্টের ঝুঁকি নেই।
    trs = true_ranges(candles)
    base_tr = statistics.median(trs[-(LOOKBACK + 2):-2])
    tr_now = max(trs[-2:])
    ratios = {"atr": tr_now / base_tr if base_tr > 0 else 0.0}

    r5_now = window_range(candles, n - 1)
    base_list = [window_range(candles, e) for e in range(n - 5 - LOOKBACK, n - 5)]
    base_list = [x for x in base_list if x is not None]
    base_r5 = statistics.median(base_list) if len(base_list) >= LOOKBACK // 2 else 0.0
    if r5_now is not None and base_r5 > 0:
        ratios["r5"] = r5_now / base_r5
    else:
        ratios["r5"] = 0.0

    if use_pvt:
        pv = pvt_series(candles)
        base_pv = statistics.median(pv[-(LOOKBACK + 2):-2])
        ratios["pvt"] = max(pv[-2:]) / base_pv if base_pv > 0 else None

    return {
        "ratios": ratios,
        "tr_now": tr_now, "base_tr": base_tr,
        "r5_now": r5_now or 0.0, "base_r5": base_r5,
        "price": candles[-1][4],
        "stale": time.time() - candles[-1][0],
    }


def evaluate(info, th):
    """(level, hits, calm) — level: 0 কিছু নয়, 1 = 🟡, 2 = 🔴"""
    hits, strong, calm = [], False, True
    for k, thr in th.items():
        r = info["ratios"].get(k)
        if thr is None or r is None:
            continue
        if r >= thr:
            hits.append(k)
        if r >= thr * STRONG_MULT:
            strong = True
        if r >= thr * CALM_FRAC:
            calm = False
    level = 0 if not hits else (2 if strong or len(hits) >= 2 else 1)
    return level, hits, calm


# ---------------- টেলিগ্রাম ও স্টেট ----------------
def send(text):
    err = None
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=15,
            )
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2)
    print(f"টেলিগ্রাম পাঠানো ব্যর্থ: {safe(err)}")
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    st.setdefault("heartbeat", "")
    st.setdefault("m", {})
    for k in MARKETS:
        st["m"].setdefault(
            k, {"level": 0, "last_alert": 0, "calm_since": 0, "fails": 0, "down": False, "count": 0}
        )
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def bd_now():
    return dt.datetime.now(BD_TZ).strftime("%H:%M")


def alert_text(cfg, info, level, hits):
    r = info["ratios"]
    icon, head = ("🔴", "খুবই অস্থির") if level == 2 else ("🟡", "অস্থির")
    lines = [f"{icon} {cfg['name']} এখন {head}!"]
    if "atr" in hits:
        lines.append(
            f"• ১-মিনিটের নড়াচড়া স্বাভাবিকের {r['atr']:.1f}x "
            f"(${info['tr_now']:,.2f}, স্বাভাবিক ${info['base_tr']:,.2f})"
        )
    if "r5" in hits:
        lines.append(
            f"• গত ৫ মিনিটের রেঞ্জ স্বাভাবিকের {r['r5']:.1f}x "
            f"(${info['r5_now']:,.2f}, স্বাভাবিক ${info['base_r5']:,.2f})"
        )
    if "pvt" in hits:
        lines.append(f"• ভলিউম-চালিত মুভ স্বাভাবিকের {r['pvt']:.1f}x (PVT)")
    lines.append(f"দাম: ${info['price']:,.2f}")
    lines.append(f"সময় (বাংলাদেশ): {bd_now()}")
    return "\n".join(lines)


def note_fetch(cfg, ms, ok, err=None):
    if ok:
        ms["fails"] = 0
        if ms["down"]:
            ms["down"] = False
            send(f"✅ {cfg['name']}: ডেটা আবার আসছে, নজরদারি চালু।")
        return
    ms["fails"] += 1
    if ms["fails"] >= cfg["fail_limit"] and not ms["down"]:
        ms["down"] = True
        send(
            f"⚠️ {cfg['name']}: ডেটা আনা যাচ্ছে না ({ms['fails']} বার টানা ব্যর্থ)।\n"
            f"কারণ: {err}\nএই সময়ে {cfg['name']}-এর অ্যালার্ট আসবে না।"
        )


def handle_data(cfg, ms, candles):
    name = cfg["name"]
    info = analyse(candles, cfg["th"].get("pvt") is not None)
    if not info:
        print(f"{name}: যথেষ্ট ডেটা নেই")
        return
    r = info["ratios"]
    print(
        f"{name}: ATR {r['atr']:.2f}x, R5 {r['r5']:.2f}x, PVT {r.get('pvt')}, "
        f"ডেটার বয়স {info['stale']:.0f}s"
    )
    if info["stale"] > MAX_STALE:
        return  # মার্কেট বন্ধ বা ডেটা পুরনো

    level, hits, calm = evaluate(info, cfg["th"])
    now = time.time()

    if level:
        ms["calm_since"] = 0
        gap = now - ms["last_alert"]
        escalated = ms["level"] > 0 and level > ms["level"]
        due = gap >= (REPEAT_EVERY if ms["level"] else COOLDOWN)
        if escalated or due:
            if send(alert_text(cfg, info, level, hits)):
                ms["level"] = level
                ms["last_alert"] = now
                ms["count"] += 1
    elif ms["level"] > 0:
        if not calm:
            ms["calm_since"] = 0
        elif not ms["calm_since"]:
            ms["calm_since"] = now
        elif now - ms["calm_since"] >= CALM_HOLD:
            send(f"✅ {name} শান্ত হয়েছে, নড়াচড়া স্বাভাবিকে ফিরেছে।\nদাম: ${info['price']:,.2f}")
            ms["level"] = 0
            ms["calm_since"] = 0


def age_text(sec):
    if sec > MAX_STALE:
        return f"⚠️ {sec / 60:.0f} মিনিট পুরনো (মার্কেট বন্ধ বা ডেটা দেরিতে)"
    return f"তাজা ({sec:.0f} সেকেন্ড আগের)"


def send_status(title, st, reset_counts=False):
    lines = [title]
    for key, cfg in MARKETS.items():
        ms = st["m"][key]
        try:
            info = analyse(cfg["fetch"](), cfg["th"].get("pvt") is not None)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{cfg['name']}: ডেটা আনতে ব্যর্থ ({safe(e)})")
            continue
        if not info:
            lines.append(f"{cfg['name']}: যথেষ্ট ডেটা নেই")
            continue
        parts = [f"{cfg['name']}: ${info['price']:,.2f}"]
        for k, thr in cfg["th"].items():
            r = info["ratios"].get(k)
            if thr is not None and r is not None:
                parts.append(f"{LABELS[k]} {r:.1f}x (থ্রে {thr:.1f}x)")
        parts.append(f"ডেটা {age_text(info['stale'])}")
        parts.append(f"গত রিপোর্টের পর অ্যালার্ট: {ms['count']}টি")
        lines.append(" | ".join(parts))
        if reset_counts:
            ms["count"] = 0
    send("\n".join(lines))


def main():
    st = load_state()
    now_utc = dt.datetime.now(dt.timezone.utc)
    today = now_utc.date().isoformat()

    if os.getenv("GITHUB_EVENT_NAME", "") == "workflow_dispatch":
        send_status("✅ টেস্ট: বট চালু আছে", st)
    elif now_utc.hour >= HEARTBEAT_UTC_HOUR and st["heartbeat"] != today:
        send_status("✅ বট চালু আছে (দৈনিক রিপোর্ট)", st, reset_counts=True)
        st["heartbeat"] = today
    save_state(st)

    next_due = {k: 0.0 for k in MARKETS}
    fetched_ok = {k: False for k in MARKETS}
    end = time.time() + RUN_SECONDS

    while True:
        now = time.time()
        for key, cfg in MARKETS.items():
            if now < next_due[key]:
                continue
            next_due[key] = now + cfg["poll"]
            ms = st["m"][key]
            try:
                candles = cfg["fetch"]()
            except Exception as e:  # noqa: BLE001
                print(f"{cfg['name']}: ত্রুটি - {safe(e)}")
                note_fetch(cfg, ms, False, safe(e))
                continue
            fetched_ok[key] = True
            note_fetch(cfg, ms, True)
            handle_data(cfg, ms, candles)
        save_state(st)

        nxt = min(next_due.values())
        if nxt >= end:
            break
        time.sleep(max(1.0, nxt - time.time()))

    if not all(fetched_ok.values()):
        # পুরো রানে কোনো মার্কেটের ডেটা একবারও না এলে Actions লাল দেখাবে
        sys.exit(1)


if __name__ == "__main__":
    main()
