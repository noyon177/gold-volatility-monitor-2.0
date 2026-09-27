"""সুইং ট্রেন্ড অ্যালার্ট বট — বড় টাইমফ্রেমের ট্রেন্ড শুরু ধরার জন্য।

আগের ভোলাটিলিটি বট (volatility_bot.py) থেকে সম্পূর্ণ আলাদা ফাইল, তবে একই
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / TWELVE_DATA_API_KEY ব্যবহার করে।

মার্কেট: USD/JPY, GBP/USD, BTC/USD, XAU/USD
টাইমফ্রেম: 4H (প্রধান সিগন্যাল) + 1D (দিকনির্দেশনা কনফার্মেশন)

সিগন্যালের শর্ত (৪টাই মিলতে হবে — তাই সিগন্যাল কম আসবে কিন্তু হাই-কোয়ালিটি হবে):
  ১. 4H চার্টে EMA9 আর EMA15 ক্রসওভার হয়েছে (৯ উপরে গেলে বুলিশ, নিচে গেলে বেয়ারিশ)।
  ২. একই ক্যান্ডেলে লিকুইডিটি সুইপ হয়েছে — অর্থাৎ সাম্প্রতিক লো/হাই একটা wick দিয়ে
     ভেঙে আবার সেই লেভেলের ভেতরে ক্লোজ হয়েছে (স্টপ-হান্ট + রিভার্সাল প্যাটার্ন)।
  ৩. ADX(14) থ্রেশহোল্ডের উপরে এবং বাড়ছে, +DI/-DI দিকনির্দেশনার সাথে মিলছে
     (ট্রেন্ড সত্যিই শক্তিশালী হচ্ছে কিনা তার প্রমাণ)।
  ৪. 1D টাইমফ্রেমের ট্রেন্ড (EMA9 বনাম EMA15) একই দিকে থাকা আবশ্যক (higher-timeframe bias)।

একই ক্যান্ডেলের জন্য বারবার অ্যালার্ট যাতে না যায়, সেজন্য প্রতিটা মার্কেটের শেষ
অ্যালার্টেড ক্যান্ডেলের টাইমস্ট্যাম্প state ফাইলে রাখা হয়।
"""
import datetime as dt
import json
import os
import time

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

# ---------------- সেটিংস (দরকার হলে টিউন করো) ----------------
EMA_FAST = 9
EMA_SLOW = 15
ADX_PERIOD = 14
ADX_THRESHOLD = 20          # এর নিচে ট্রেন্ড দুর্বল ধরা হবে, অ্যালার্ট যাবে না
SWEEP_LOOKBACK = 10         # লিকুইডিটি সুইপ খোঁজার জন্য আগের কতগুলো ক্যান্ডেল দেখা হবে
OUTPUT_SIZE = 200           # প্রতি ফেচে কতগুলো ক্যান্ডেল আনা হবে (EMA/ADX ওয়ার্ম-আপের জন্য)
# আলাদা state ফাইল — ভোলাটিলিটি বটের state.json-এর সাথে যেন conflict না হয়
STATE_FILE = os.getenv("SWING_STATE_FILE", "swing_state.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}
BD_TZ = dt.timezone(dt.timedelta(hours=6))
SECRETS = [TOKEN, TWELVE_DATA_KEY]

MARKETS = {
    "XAUUSD": {"name": "গোল্ড (XAU/USD)", "symbol": "XAU/USD"},
    "BTCUSD": {"name": "বিটকয়েন (BTC/USD)", "symbol": "BTC/USD"},
    "USDJPY": {"name": "USD/JPY", "symbol": "USD/JPY"},
    "GBPUSD": {"name": "GBP/USD", "symbol": "GBP/USD"},
}


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
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(safe(err)) from None


# ---------------- ডেটা ----------------
def fetch_candles(symbol, interval, outputsize=OUTPUT_SIZE):
    """ক্যান্ডেল ফরম্যাট: (time, open, high, low, close, volume)। সময় অনুসারে সাজানো, সবচেয়ে নতুনটা শেষে।"""
    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
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
        fmt = "%Y-%m-%d %H:%M:%S" if len(r["datetime"]) > 10 else "%Y-%m-%d"
        t = dt.datetime.strptime(r["datetime"], fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        out.append(
            (t, float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r.get("volume") or 0))
        )
    return out


# ---------------- ইন্ডিকেটর ----------------
def ema_series(closes, period):
    n = len(closes)
    if n < period:
        return [None] * n
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    prev = sum(closes[:period]) / period
    out.append(prev)
    for price in closes[period:]:
        prev = price * k + prev * (1 - k)
        out.append(prev)
    return out


def wilder_smooth(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    out[period - 1] = sum(values[:period])
    for i in range(period, n):
        out[i] = out[i - 1] - out[i - 1] / period + values[i]
    return out


def calc_adx(candles, period=ADX_PERIOD):
    """রিটার্ন করে (plus_di, minus_di, adx) — তিনটা লিস্ট, candles-এর সাথে ইনডেক্স মিলিয়ে।"""
    n = len(candles)
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    s_tr = wilder_smooth(tr, period)
    s_plus = wilder_smooth(plus_dm, period)
    s_minus = wilder_smooth(minus_dm, period)

    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n
    for i in range(n):
        if s_tr[i]:
            plus_di[i] = 100 * s_plus[i] / s_tr[i]
            minus_di[i] = 100 * s_minus[i] / s_tr[i]
            denom = plus_di[i] + minus_di[i]
            if denom > 0:
                dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom

    adx = [None] * n
    valid = [i for i in range(n) if dx[i] is not None]
    if len(valid) >= period:
        start = valid[period - 1]
        adx[start] = sum(dx[i] for i in valid[:period]) / period
        for i in range(start + 1, n):
            if dx[i] is None:
                continue
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return plus_di, minus_di, adx


def crossover_direction(ema_fast, ema_slow, idx):
    if idx < 1:
        return None
    f0, f1 = ema_fast[idx - 1], ema_fast[idx]
    s0, s1 = ema_slow[idx - 1], ema_slow[idx]
    if None in (f0, f1, s0, s1):
        return None
    if f0 <= s0 and f1 > s1:
        return "bull"
    if f0 >= s0 and f1 < s1:
        return "bear"
    return None


def liquidity_sweep(candles, idx, direction, lookback=SWEEP_LOOKBACK):
    """idx-এর ক্যান্ডেলে লিকুইডিটি সুইপ হয়েছে কিনা (স্টপ-হান্ট + রিভার্সাল)।

    bull: আগের `lookback` ক্যান্ডেলের সর্বনিম্ন লো, wick দিয়ে ভেঙে আবার তার উপরে ক্লোজ।
    bear: সর্বোচ্চ হাই ভেঙে আবার তার নিচে ক্লোজ।
    """
    start = max(0, idx - lookback)
    window = candles[start:idx]
    if len(window) < max(3, lookback // 2):
        return False
    cur_low, cur_high, cur_close = candles[idx][3], candles[idx][2], candles[idx][4]
    if direction == "bull":
        recent_low = min(c[3] for c in window)
        return cur_low < recent_low and cur_close > recent_low
    recent_high = max(c[2] for c in window)
    return cur_high > recent_high and cur_close < recent_high


# ---------------- সিগন্যাল যাচাই ----------------
def evaluate_signal(cfg):
    c4 = fetch_candles(cfg["symbol"], "4h")
    c1 = fetch_candles(cfg["symbol"], "1day")
    if len(c4) < EMA_SLOW + ADX_PERIOD + 5 or len(c1) < EMA_SLOW + 5:
        return None

    closes4 = [c[4] for c in c4]
    ema9_4 = ema_series(closes4, EMA_FAST)
    ema15_4 = ema_series(closes4, EMA_SLOW)
    plus_di4, minus_di4, adx4 = calc_adx(c4, ADX_PERIOD)

    idx = len(c4) - 2  # শেষ সম্পূর্ণ (ক্লোজড) 4H ক্যান্ডেল; শেষটা এখনো চলমান হতে পারে
    direction = crossover_direction(ema9_4, ema15_4, idx)
    if not direction:
        return None

    if adx4[idx] is None or adx4[idx] < ADX_THRESHOLD:
        return None
    if adx4[idx - 1] is not None and adx4[idx] <= adx4[idx - 1]:
        return None  # ADX বাড়ছে না, ট্রেন্ড কনফার্মেশন দুর্বল

    if plus_di4[idx] is None or minus_di4[idx] is None:
        return None
    if direction == "bull" and not (plus_di4[idx] > minus_di4[idx]):
        return None
    if direction == "bear" and not (minus_di4[idx] > plus_di4[idx]):
        return None

    if not liquidity_sweep(c4, idx, direction):
        return None

    # 1D higher-timeframe bias কনফার্মেশন
    closes1 = [c[4] for c in c1]
    ema9_1 = ema_series(closes1, EMA_FAST)
    ema15_1 = ema_series(closes1, EMA_SLOW)
    idx1 = len(c1) - 2
    if ema9_1[idx1] is None or ema15_1[idx1] is None:
        return None
    daily_bull = ema9_1[idx1] > ema15_1[idx1]
    if direction == "bull" and not daily_bull:
        return None
    if direction == "bear" and daily_bull:
        return None

    return {
        "direction": direction,
        "bar_time": c4[idx][0],
        "price": c4[idx][4],
        "adx": adx4[idx],
        "plus_di": plus_di4[idx],
        "minus_di": minus_di4[idx],
    }


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
    st.setdefault("m", {})
    for k in MARKETS:
        st["m"].setdefault(k, {"last_bar": 0})
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def bd_now():
    return dt.datetime.now(BD_TZ).strftime("%Y-%m-%d %H:%M")


def price_fmt(p):
    return f"${p:,.4f}" if p < 10 else f"${p:,.2f}"


def alert_text(cfg, s):
    if s["direction"] == "bull":
        icon, word, sweep_line = "🟢", "বাই (Bullish)", "নিচের লিকুইডিটি সুইপ হয়েছে (স্টপ-হান্ট + রিভার্সাল)"
    else:
        icon, word, sweep_line = "🔴", "সেল (Bearish)", "উপরের লিকুইডিটি সুইপ হয়েছে (স্টপ-হান্ট + রিভার্সাল)"

    lines = [
        f"{icon} {cfg['name']} — সুইং ট্রেন্ড সিগন্যাল: {word}",
        "৯ ও ১৫ EMA ক্রসওভার হয়েছে",
        sweep_line,
        f"ADX(14): {s['adx']:.1f} (থ্রে {ADX_THRESHOLD}) | +DI {s['plus_di']:.1f} / -DI {s['minus_di']:.1f}",
        "1D ট্রেন্ড একই দিকে কনফার্ম করছে",
        f"দাম: {price_fmt(s['price'])}",
        "টাইমফ্রেম: 4H",
        f"সময় (বাংলাদেশ): {bd_now()}",
    ]
    return "\n".join(lines)


def handle_market(key, cfg, st):
    name = cfg["name"]
    try:
        signal = evaluate_signal(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"{name}: ত্রুটি - {safe(e)}")
        return
    if not signal:
        print(f"{name}: কোনো সিগন্যাল নেই")
        return

    ms = st["m"][key]
    if signal["bar_time"] <= ms["last_bar"]:
        print(f"{name}: সিগন্যাল আছে কিন্তু এই ক্যান্ডেলের অ্যালার্ট আগেই পাঠানো হয়েছে")
        return

    print(f"{name}: নতুন সিগন্যাল ({signal['direction']}) — অ্যালার্ট পাঠানো হচ্ছে")
    if send(alert_text(cfg, signal)):
        ms["last_bar"] = signal["bar_time"]


def main():
    st = load_state()
    for key, cfg in MARKETS.items():
        handle_market(key, cfg, st)
    save_state(st)


if __name__ == "__main__":
    main()
