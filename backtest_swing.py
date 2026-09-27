"""সুইং সিগন্যাল ব্যাকটেস্ট — swing_bot.py-এর ঠিক একই লজিক অতীতের ডেটার উপর চালিয়ে দেখায়:
- অতীতে কবে কবে (EMA9/15 ক্রসওভার + লিকুইডিটি সুইপ + ADX কনফার্মেশন + 1D bias) মিলেছিল
- সেই সিগন্যালের পরে ১ দিন, ৩ দিন, ৫ দিন পর দাম কোন দিকে কতটা মুভ করেছে
- মোট কতগুলো সিগন্যাল, তার মধ্যে কতগুলো সঠিক দিকে গিয়েছে (win rate)

এটা GitHub Actions-এ চালানোর দরকার নেই — নিজের কম্পিউটারে বা যেকোনো জায়গায় একবার রান করলেই হবে:
    TWELVE_DATA_API_KEY=xxxx python backtest_swing.py

শুধু TWELVE_DATA_API_KEY লাগবে (টেলিগ্রাম টোকেন লাগবে না, কারণ এটা কোনো মেসেজ পাঠায় না,
শুধু ফলাফল টার্মিনালে প্রিন্ট করে)।
"""
import datetime as dt
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

# ---------------- সেটিংস (swing_bot.py-এর সাথে হুবহু মিলিয়ে রাখা হয়েছে) ----------------
EMA_FAST = 9
EMA_SLOW = 15
ADX_PERIOD = 14
ADX_THRESHOLD = 20
SWEEP_LOOKBACK = 10

# ব্যাকটেস্টের জন্য কত ইতিহাস আনা হবে
OUTPUT_4H = 1000   # ~1000 x 4h ≈ ১৬৬ দিন (৫.৫ মাস)
OUTPUT_1D = 300    # ~৩০০ দিন

# সিগন্যালের পরে কত ক্যান্ডেল/দিন পর ফলাফল চেক করা হবে
FORWARD_4H_CHECKS = {"১ দিন": 6, "৩ দিন": 18, "৫ দিন": 30}

MARKETS = {
    "XAUUSD": {"name": "গোল্ড (XAU/USD)", "symbol": "XAU/USD"},
    "BTCUSD": {"name": "বিটকয়েন (BTC/USD)", "symbol": "BTC/USD"},
    "USDJPY": {"name": "USD/JPY", "symbol": "USD/JPY"},
    "GBPUSD": {"name": "GBP/USD", "symbol": "GBP/USD"},
}

HEADERS = {"User-Agent": "Mozilla/5.0"}


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
    raise RuntimeError(str(err)) from None


def fetch_candles(symbol, interval, outputsize):
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


# ---------------- ইন্ডিকেটর (swing_bot.py থেকে হুবহু) ----------------
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


def find_daily_idx(c1, t):
    """t সময়ের আগে সম্পূর্ণ ক্লোজড শেষ ডেইলি ক্যান্ডেলের ইনডেক্স (ফরওয়ার্ড-লিকিং এড়াতে)।"""
    idx = None
    for j, c in enumerate(c1):
        if c[0] + 86400 <= t:
            idx = j
        else:
            break
    return idx


# ---------------- ব্যাকটেস্ট ----------------
def backtest_market(key, cfg):
    name = cfg["name"]
    print(f"\n{'=' * 60}\n{name} — ডেটা আনা হচ্ছে...")
    c4 = fetch_candles(cfg["symbol"], "4h", OUTPUT_4H)
    c1 = fetch_candles(cfg["symbol"], "1day", OUTPUT_1D)
    print(f"{name}: {len(c4)}টা 4H ক্যান্ডেল, {len(c1)}টা 1D ক্যান্ডেল পাওয়া গেছে")

    closes4 = [c[4] for c in c4]
    ema9_4 = ema_series(closes4, EMA_FAST)
    ema15_4 = ema_series(closes4, EMA_SLOW)
    plus_di4, minus_di4, adx4 = calc_adx(c4, ADX_PERIOD)

    closes1 = [c[4] for c in c1]
    ema9_1 = ema_series(closes1, EMA_FAST)
    ema15_1 = ema_series(closes1, EMA_SLOW)

    max_forward = max(FORWARD_4H_CHECKS.values())
    signals = []

    for idx in range(EMA_SLOW + ADX_PERIOD, len(c4) - max_forward):
        direction = crossover_direction(ema9_4, ema15_4, idx)
        if not direction:
            continue
        if adx4[idx] is None or adx4[idx] < ADX_THRESHOLD:
            continue
        if adx4[idx - 1] is not None and adx4[idx] <= adx4[idx - 1]:
            continue
        if plus_di4[idx] is None or minus_di4[idx] is None:
            continue
        if direction == "bull" and not (plus_di4[idx] > minus_di4[idx]):
            continue
        if direction == "bear" and not (minus_di4[idx] > plus_di4[idx]):
            continue
        if not liquidity_sweep(c4, idx, direction):
            continue

        idx1 = find_daily_idx(c1, c4[idx][0])
        if idx1 is None or ema9_1[idx1] is None or ema15_1[idx1] is None:
            continue
        daily_bull = ema9_1[idx1] > ema15_1[idx1]
        if direction == "bull" and not daily_bull:
            continue
        if direction == "bear" and daily_bull:
            continue

        entry_price = c4[idx][4]
        entry_time = dt.datetime.fromtimestamp(c4[idx][0], tz=dt.timezone.utc)
        forward = {}
        for label, n_candles in FORWARD_4H_CHECKS.items():
            future_price = c4[idx + n_candles][4]
            pct = (future_price - entry_price) / entry_price * 100
            forward[label] = pct

        signals.append(
            {"time": entry_time, "direction": direction, "price": entry_price, "forward": forward}
        )

    print(f"{name}: মোট {len(signals)}টা সিগন্যাল পাওয়া গেছে (গত ~{OUTPUT_4H * 4 // 24} দিনে)\n")
    for s in signals:
        arrow = "🟢 বাই" if s["direction"] == "bull" else "🔴 সেল"
        fwd_str = " | ".join(f"{label}: {pct:+.2f}%" for label, pct in s["forward"].items())
        print(f"  {s['time'].strftime('%Y-%m-%d %H:%M')} UTC — {arrow} @ {s['price']:,.4f} — {fwd_str}")

    # win rate: bull হলে + হওয়া উচিত, bear হলে - হওয়া উচিত (৩ দিনের চেক দিয়ে ধরা হলো)
    if signals:
        wins = 0
        for s in signals:
            pct = s["forward"]["৩ দিন"]
            if (s["direction"] == "bull" and pct > 0) or (s["direction"] == "bear" and pct < 0):
                wins += 1
        print(f"\n  ৩-দিনের হিসেবে win rate: {wins}/{len(signals)} ({wins / len(signals) * 100:.0f}%)")
    return signals


def main():
    all_signals = {}
    for key, cfg in MARKETS.items():
        try:
            all_signals[key] = backtest_market(key, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"{cfg['name']}: ত্রুটি - {e}")

    print(f"\n{'=' * 60}\nসারসংক্ষেপ")
    for key, cfg in MARKETS.items():
        n = len(all_signals.get(key, []))
        print(f"  {cfg['name']}: {n}টা সিগন্যাল")


if __name__ == "__main__":
    main()
