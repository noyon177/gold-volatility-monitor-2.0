
"""
গোল্ড (XAU) ও বিটকয়েন (BTC)-এর ভোলাটিলিটি অ্যালার্ট বট।

সময় প্রদর্শন:
বাংলাদেশ সময় (UTC+6) এবং UTC 0 / GMT

শুধু ভোলাটিলিটি মাপে, কোনো ট্রেড সিগন্যাল বা
সাপোর্ট/রেজিস্ট্যান্স দেয় না।
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
LOOKBACK = 90
RUN_SECONDS = 285
BTC_POLL = 20
GOLD_POLL = 150
COOLDOWN = 300
REPEAT_EVERY = 900
STRONG_MULT = 1.5
CALM_FRAC = 0.7
CALM_HOLD = 300
MAX_STALE = 180
GAP_RESET = 300
HEARTBEAT_UTC_HOUR = 3

STATE_FILE = os.getenv("STATE_FILE", "state.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}

BD_TZ = dt.timezone(dt.timedelta(hours=6))
UTC = dt.timezone.utc

SECRETS = [TOKEN, TWELVE_DATA_KEY]

LABELS = {
    "atr": "১-মিনিট ATR",
    "r5": "৫-মিনিট রেঞ্জ",
    "pvt": "PVT"
}


# ---------------- সময় ----------------

def time_text():
    """
    বাংলাদেশ সময় ও UTC 0 সময় একসাথে দেখায়।
    """

    now_utc = dt.datetime.now(UTC)
    now_bd = now_utc.astimezone(BD_TZ)

    return (
        f"🇧🇩 বাংলাদেশ সময় (UTC+6): "
        f"{now_bd.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🌐 UTC 0 / GMT: "
        f"{now_utc.strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ---------------- নিরাপদ এরর ----------------

def safe(e):
    s = f"{type(e).__name__}: {e}"

    for x in SECRETS:
        if x:
            s = s.replace(x, "***")

    return s[:200]


def get_json(url, params=None, tries=3):
    err = None

    for i in range(tries):
        try:
            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            r.raise_for_status()
            return r.json()

        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))

    raise RuntimeError(safe(err)) from None


# ---------------- ডেটা সোর্স ----------------

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
        raise RuntimeError(
            "Twelve Data: " +
            str(data.get("message", "error"))[:150]
        )

    rows = sorted(
        data["values"],
        key=lambda r: r["datetime"]
    )

    out = []

    for r in rows:
        t = dt.datetime.strptime(
            r["datetime"],
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC).timestamp()

        out.append((
            t,
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r.get("volume") or 0)
        ))

    return out


def btc_kraken():
    data = get_json(
        "https://api.kraken.com/0/public/OHLC",
        {
            "pair": "XBTUSD",
            "interval": 1
        }
    )

    if data.get("error"):
        raise RuntimeError(
            "Kraken: " + str(data["error"])[:150]
        )

    rows = next(
        v for k, v in data["result"].items()
        if k != "last"
    )

    return [
        (
            float(r[0]),
            float(r[1]),
            float(r[2]),
            float(r[3]),
            float(r[4]),
            float(r[6])
        )
        for r in rows
    ]


def btc_coinbase():
    rows = get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        {"granularity": 60}
    )

    rows = sorted(rows, key=lambda x: x[0])

    return [
        (t, o, h, l, c, v)
        for t, l, h, o, c, v in rows
    ]


def btc_candles():
    best, last_err = None, None

    for src in (btc_kraken, btc_coinbase):
        try:
            c = src()

        except Exception as e:
            last_err = e
            continue

        if time.time() - c[-1][0] <= MAX_STALE:
            return c

        if best is None or c[-1][0] > best[-1][0]:
            best = c

    if best is None:
        raise RuntimeError(safe(last_err))

    return best


# ---------------- মার্কেট সেটিংস ----------------

MARKETS = {
    "XAU": {
        "name": "গোল্ড (XAU)",
        "fetch": gold_candles,
        "poll": GOLD_POLL,
        "fail_limit": 4,
        "th": {
            "atr": 2.5,
            "r5": 2.0,
            "pvt": None
        },
        "vol_confirm": None,
    },

    "BTC": {
        "name": "বিটকয়েন (BTC)",
        "fetch": btc_candles,
        "poll": BTC_POLL,
        "fail_limit": 6,
        "th": {
            "atr": 2.5,
            "r5": 2.0,
            "pvt": 6.0
        },
        "vol_confirm": 1.4,
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

            out.append(
                max(
                    h - l,
                    abs(h - pc),
                    abs(l - pc)
                )
            )

    return out


def pvt_series(c):
    out = [0.0]

    for i in range(1, len(c)):
        pc = c[i - 1][4]
        cl = c[i][4]
        v = c[i][5]

        out.append(
            abs(v * (cl - pc) / pc) if pc else 0.0
        )

    return out


def window_range(c, end, n=5):
    w = c[end - n + 1:end + 1]

    if len(w) < n:
        return None

    if w[-1][0] - w[0][0] > (n - 1) * 60 + 120:
        return None

    return max(x[2] for x in w) - min(x[3] for x in w)


def analyse(candles, use_pvt):
    n = len(candles)

    if n < LOOKBACK + 10:
        return None

    trs = true_ranges(candles)

    base_tr = statistics.median(
        trs[-(LOOKBACK + 2):-2]
    )

    tr_now = max(trs[-2:])

    ratios = {
        "atr": tr_now / base_tr if base_tr > 0 else 0.0
    }

    r5_now = window_range(candles, n - 1)

    base_list = [
        window_range(candles, e)
        for e in range(n - 5 - LOOKBACK, n - 5)
    ]

    base_list = [x for x in base_list if x is not None]

    base_r5 = (
        statistics.median(base_list)
        if len(base_list) >= LOOKBACK // 2
        else 0.0
    )

    if r5_now is not None and base_r5 > 0:
        ratios["r5"] = r5_now / base_r5
    else:
        ratios["r5"] = 0.0

    if use_pvt:
        pv = pvt_series(candles)

        base_pv = statistics.median(
            pv[-(LOOKBACK + 2):-2]
        )

        ratios["pvt"] = (
            max(pv[-2:]) / base_pv
            if base_pv > 0 else None
        )

        vols = [x[5] for x in candles]

        base_vol = statistics.median(
            vols[-(LOOKBACK + 2):-2]
        )

        vol_now = max(vols[-2:])

        ratios["vol"] = (
            vol_now / base_vol
            if base_vol > 0 else None
        )

    return {
        "ratios": ratios,
        "tr_now": tr_now,
        "base_tr": base_tr,
        "r5_now": r5_now or 0.0,
        "base_r5": base_r5,
        "price": candles[-1][4],
        "stale": time.time() - candles[-1][0],
    }


def evaluate(info, th, vol_confirm=None):
    hits = []
    strong = False
    calm = True

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

    vol_ok = None

    if vol_confirm is not None:
        vr = info["ratios"].get("vol")
        vol_ok = vr is not None and vr >= vol_confirm

    if not hits:
        level = 0

    elif vol_confirm is not None:
        level = (
            2 if strong or len(hits) >= 2 or vol_ok
            else 1
        )

    else:
        level = 2 if strong or len(hits) >= 2 else 1

    return level, hits, calm, vol_ok


# ---------------- টেলিগ্রাম ----------------

def send(text):
    err = None

    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={
                    "chat_id": CHAT_ID,
                    "text": text
                },
                timeout=15
            )

            r.raise_for_status()
            return True

        except Exception as e:
            err = e
            time.sleep(2)

    print(f"টেলিগ্রাম পাঠানো ব্যর্থ: {safe(err)}")
    return False


# ---------------- স্টেট ----------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)

    except Exception:
        st = {}

    st.setdefault("heartbeat", "")
    st.setdefault("m", {})

    for k in MARKETS:
        st["m"].setdefault(
            k,
            {
                "level": 0,
                "last_alert": 0,
                "calm_since": 0,
                "fails": 0,
                "down": False,
                "count": 0
            }
        )

    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)

    os.replace(tmp, STATE_FILE)


# ---------------- অ্যালার্ট মেসেজ ----------------

def alert_text(cfg, info, level, hits, vol_ok=None):
    r = info["ratios"]

    if vol_ok is not None:
        if level == 2:
            icon = "🔴"
            head = "খুবই অস্থির, ভলিউমও নিশ্চিত করছে"
        else:
            icon = "🟡"
            head = "অস্থির (ভলিউম এখনও স্বাভাবিক, নিশ্চিত হয়নি)"

    else:
        if level == 2:
            icon = "🔴"
            head = "খুবই অস্থির"
        else:
            icon = "🟡"
            head = "অস্থির"

    lines = [
        f"{icon} {cfg['name']} এখন {head}!"
    ]

    if "atr" in hits:
        lines.append(
            f"• ১-মিনিটের নড়াচড়া স্বাভাবিকের "
            f"{r['atr']:.1f}x "
            f"(${info['tr_now']:,.2f}, "
            f"স্বাভাবিক ${info['base_tr']:,.2f})"
        )

    if "r5" in hits:
        lines.append(
            f"• গত ৫ মিনিটের রেঞ্জ স্বাভাবিকের "
            f"{r['r5']:.1f}x "
            f"(${info['r5_now']:,.2f}, "
            f"স্বাভাবিক ${info['base_r5']:,.2f})"
        )

    if "pvt" in hits:
        lines.append(
            f"• ভলিউম-চালিত মুভ স্বাভাবিকের "
            f"{r['pvt']:.1f}x (PVT)"
        )

    if vol_ok is not None and r.get("vol") is not None:
        lines.append(
            f"• ভলিউম স্বাভাবিকের {r['vol']:.1f}x"
        )

    lines.append(f"দাম: ${info['price']:,.2f}")

    # নতুন: বাংলাদেশ সময় + UTC 0
    lines.append("")
    lines.append(time_text())

    return "\n".join(lines)


# ---------------- ডেটা স্ট্যাটাস ----------------

def note_fetch(cfg, ms, ok, err=None):
    if ok:
        ms["fails"] = 0

        if ms["down"]:
            ms["down"] = False

            send(
                f"✅ {cfg['name']}: ডেটা আবার আসছে, "
                f"নজরদারি চালু।\n\n{time_text()}"
            )

        return

    ms["fails"] += 1

    if ms["fails"] >= cfg["fail_limit"] and not ms["down"]:
        ms["down"] = True

        send(
            f"⚠️ {cfg['name']}: ডেটা আনা যাচ্ছে না "
            f"({ms['fails']} বার টানা ব্যর্থ)।\n"
            f"কারণ: {err}\n"
            f"এই সময়ে {cfg['name']}-এর অ্যালার্ট আসবে না।\n\n"
            f"{time_text()}"
        )


# ---------------- ডেটা প্রসেসিং ----------------

def handle_data(cfg, ms, candles):
    name = cfg["name"]

    info = analyse(
        candles,
        cfg["th"].get("pvt") is not None
    )

    if not info:
        print(f"{name}: যথেষ্ট ডেটা নেই")
        return

    r = info["ratios"]

    print(
        f"{name}: ATR {r['atr']:.2f}x, "
        f"R5 {r['r5']:.2f}x, "
        f"PVT {r.get('pvt')}, "
        f"VOL {r.get('vol')}, "
        f"ডেটার বয়স {info['stale']:.0f}s"
    )

    if info["stale"] > MAX_STALE:
        return

    level, hits, calm, vol_ok = evaluate(
        info,
        cfg["th"],
        cfg.get("vol_confirm")
    )

    now = time.time()

    if level:
        ms["calm_since"] = 0

        gap = now - ms["last_alert"]

        escalated = (
            ms["level"] > 0 and level > ms["level"]
        )

        due = gap >= (
            REPEAT_EVERY if ms["level"] else COOLDOWN
        )

        if escalated or due:
            if send(
                alert_text(cfg, info, level, hits, vol_ok)
            ):
                ms["level"] = level
                ms["last_alert"] = now
                ms["count"] += 1

    elif ms["level"] > 0:
        if not calm:
            ms["calm_since"] = 0

        elif not ms["calm_since"]:
            ms["calm_since"] = now

        elif now - ms["calm_since"] >= CALM_HOLD:
            send(
                f"✅ {name} শান্ত হয়েছে, "
                f"নড়াচড়া স্বাভাবিকে ফিরেছে।\n"
                f"দাম: ${info['price']:,.2f}\n\n"
                f"{time_text()}"
            )

            ms["level"] = 0
            ms["calm_since"] = 0


# ---------------- স্ট্যাটাস রিপোর্ট ----------------

def age_text(sec):
    if sec > MAX_STALE:
        return (
            f"⚠️ {sec / 60:.0f} মিনিট পুরনো "
            f"(মার্কেট বন্ধ বা ডেটা দেরিতে)"
        )

    return f"তাজা ({sec:.0f} সেকেন্ড আগের)"


def send_status(title, st, reset_counts=False):
    lines = [title]

    for key, cfg in MARKETS.items():
        ms = st["m"][key]

        try:
            info = analyse(
                cfg["fetch"](),
                cfg["th"].get("pvt") is not None
            )

        except Exception as e:
            lines.append(
                f"{cfg['name']}: ডেটা আনতে ব্যর্থ ({safe(e)})"
            )
            continue

        if not info:
            lines.append(f"{cfg['name']}: যথেষ্ট ডেটা নেই")
            continue

        parts = [
            f"{cfg['name']}: ${info['price']:,.2f}"
        ]

        for k, thr in cfg["th"].items():
            r = info["ratios"].get(k)

            if thr is not None and r is not None:
                parts.append(
                    f"{LABELS[k]} {r:.1f}x "
                    f"(থ্রে {thr:.1f}x)"
                )

        if (
            cfg.get("vol_confirm")
            and info["ratios"].get("vol") is not None
        ):
            parts.append(
                f"ভলিউম {info['ratios']['vol']:.1f}x "
                f"(থ্রে {cfg['vol_confirm']:.1f}x)"
            )

        parts.append(f"ডেটা {age_text(info['stale'])}")
        parts.append(
            f"গত রিপোর্টের পর অ্যালার্ট: {ms['count']}টি"
        )

        lines.append(" | ".join(parts))

        if reset_counts:
            ms["count"] = 0

    lines.append("")
    lines.append(time_text())

    send("\n".join(lines))


# ---------------- মূল বট ----------------

def main():
    st = load_state()

    now_utc = dt.datetime.now(UTC)
    today = now_utc.date().isoformat()

    if os.getenv("GITHUB_EVENT_NAME", "") == "workflow_dispatch":
        send_status("✅ টেস্ট: বট চালু আছে", st)

    elif (
        now_utc.hour >= HEARTBEAT_UTC_HOUR
        and st["heartbeat"] != today
    ):
        send_status(
            "✅ বট চালু আছে (দৈনিক রিপোর্ট)",
            st,
            reset_counts=True
        )

        st["heartbeat"] = today

    save_state(st)

    next_due = {
        k: 0.0 for k in MARKETS
    }

    fetched_ok = {
        k: False for k in MARKETS
    }

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

            except Exception as e:
                print(
                    f"{cfg['name']}: ত্রুটি - {safe(e)}"
                )

                note_fetch(
                    cfg,
                    ms,
                    False,
                    safe(e)
                )

                continue

            fetched_ok[key] = True

            note_fetch(cfg, ms, True)

            handle_data(cfg, ms, candles)

        save_state(st)

        nxt = min(next_due.values())

        if nxt >= end:
            break

        time.sleep(
            max(1.0, nxt - time.time())
        )

    if not all(fetched_ok.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
          
