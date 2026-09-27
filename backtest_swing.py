"""EMA ট্রেন্ড রেজিম সিগন্যাল + ATR-ভিত্তিক ব্রেক-ইভেন/ট্রেইলিং-স্টপের বাস্তবসম্মত ব্যাকটেস্ট।

আগের ভার্সন থেকে বদল:
  - ADX থ্রেশহোল্ড 22→26 + ADX rising কনফার্মেশন, যাতে চপি মার্কেটকে ভুলভাবে
    "ট্রেন্ডিং" ধরে false crossover কম নেওয়া হয়
  - RSI-ভিত্তিক রেঞ্জিং রেজিম ডিফল্টে বন্ধ (ENABLE_RANGE_REGIME=False) — sample
    সাইজ ছোট আর ফলাফল অসামঞ্জস্যপূর্ণ ছিল
  - ফিক্সড TP-এর বদলে ব্রেক-ইভেন + ট্রেইলিং স্টপ — দাম ১×ATR অনুকূলে গেলে SL
    ব্রেক-ইভেনে, তারপর extreme থেকে ATR দূরত্বে ট্রেইল করে, বড় মুভ cap হয় না
  - একই মার্কেটে একবারে একটার বেশি ট্রেড ওপেন থাকতে পারে না (overlap প্রতিরোধ)

প্রতিটা সিগন্যালের পরে ক্যান্ডেল বাই ক্যান্ডেল এগিয়ে দেখা হয় SL/ব্রেক-ইভেন/ট্রেইল
কীভাবে রেজল্ভ হয় — তার ভিত্তিতে আসল লাভ/লস হিসাব করা হয় (ফিক্সড % এর বদলে)।

চালানোর নিয়ম: TWELVE_DATA_API_KEY=xxxx python backtest_swing.py
"""
import datetime as dt
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

EMA_FAST = 9
EMA_SLOW = 15
ADX_PERIOD = 14
ADX_THRESHOLD = 26          # আগে ছিল 22 — চপি মার্কেটকে ভুলভাবে "ট্রেন্ডিং" ধরে ফেলার সম্ভাবনা কমাতে বাড়ানো হলো
ADX_RISING_LOOKBACK = 3     # ADX গত N ক্যান্ডেলের চেয়ে বেশি কিনা — ট্রেন্ড সত্যিই শক্তিশালী হচ্ছে তার কনফার্মেশন
ENABLE_RANGE_REGIME = False  # RSI-ভিত্তিক রেঞ্জিং সিগন্যাল বন্ধ — আগের রানে সব মার্কেটে ৩-১০টা ট্রেড, ফলাফল অসামঞ্জস্যপূর্ণ ছিল
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
ATR_PERIOD = 14
SL_ATR_MULT = 1.5           # ইনিশিয়াল স্টপ লস (এন্ট্রি থেকে দূরত্ব)
BE_TRIGGER_ATR_MULT = 1.0   # দাম এই পরিমাণ ATR অনুকূলে গেলে SL ব্রেক-ইভেনে টানা হয়
TRAIL_ATR_MULT = 1.5        # ব্রেক-ইভেনের পর, সর্বোচ্চ/সর্বনিম্ন প্রাইস থেকে এই দূরত্বে ট্রেইলিং স্টপ (আর ফিক্সড TP নেই)
MAX_HOLD_CANDLES = 60       # সর্বোচ্চ কতগুলো 4H ক্যান্ডেল (~10 দিন) ধরে ট্রেড খোলা রাখা হবে
OUTPUT_4H = 2200            # ~১ বছর

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


def wilder_smooth_sum(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    out[period - 1] = sum(values[:period])
    for i in range(period, n):
        out[i] = out[i - 1] - out[i - 1] / period + values[i]
    return out


def wilder_avg(values, period):
    n = len(values)
    out = [None] * n
    if n <= period:
        return out
    out[period] = sum(values[1:period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def true_range_series(candles):
    n = len(candles)
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def calc_atr(candles, period=ATR_PERIOD):
    return wilder_avg(true_range_series(candles), period)


def calc_adx(candles, period=ADX_PERIOD):
    n = len(candles)
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_range_series(candles)
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    s_tr = wilder_smooth_sum(tr, period)
    s_plus = wilder_smooth_sum(plus_dm, period)
    s_minus = wilder_smooth_sum(minus_dm, period)

    dx = [None] * n
    for i in range(n):
        if s_tr[i]:
            pdi = 100 * s_plus[i] / s_tr[i]
            mdi = 100 * s_minus[i] / s_tr[i]
            denom = pdi + mdi
            if denom > 0:
                dx[i] = 100 * abs(pdi - mdi) / denom

    adx = [None] * n
    valid = [i for i in range(n) if dx[i] is not None]
    if len(valid) >= period:
        start = valid[period - 1]
        adx[start] = sum(dx[i] for i in valid[:period]) / period
        for i in range(start + 1, n):
            if dx[i] is None:
                continue
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def calc_rsi(closes, period=RSI_PERIOD):
    n = len(closes)
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0
    avg_gain = wilder_avg(gains, period)
    avg_loss = wilder_avg(losses, period)
    rsi = [None] * n
    for i in range(n):
        ag, al = avg_gain[i], avg_loss[i]
        if ag is None or al is None:
            continue
        rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return rsi


def ema_crossover(ema_fast, ema_slow, idx):
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


def rsi_reversal(rsi, idx):
    if idx < 1:
        return None
    r0, r1 = rsi[idx - 1], rsi[idx]
    if r0 is None or r1 is None:
        return None
    if r0 < RSI_OVERSOLD <= r1:
        return "bull"
    if r0 > RSI_OVERBOUGHT >= r1:
        return "bear"
    return None


def adx_rising(adx, idx, lookback=ADX_RISING_LOOKBACK):
    """ADX বর্তমানে lookback ক্যান্ডেল আগের চেয়ে বেশি কিনা — ট্রেন্ড দুর্বল হয়ে আসার
    সময় false crossover এড়াতে সাহায্য করে।"""
    if idx < lookback:
        return False
    a0, a1 = adx[idx - lookback], adx[idx]
    if a0 is None or a1 is None:
        return False
    return a1 > a0


def simulate_exit(c4, idx, direction, entry, initial_sl, atr_val, max_hold=MAX_HOLD_CANDLES):
    """entry-এর পরের ক্যান্ডেলগুলো ধরে এগিয়ে ব্রেক-ইভেন + ট্রেইলিং স্টপ সিমুলেট করে।
    দাম BE_TRIGGER_ATR_MULT×ATR অনুকূলে গেলে SL ব্রেক-ইভেনে টানা হয়, তারপর সর্বোচ্চ/
    সর্বনিম্ন প্রাইস থেকে TRAIL_ATR_MULT×ATR দূরত্বে ট্রেইল করে — কোনো ফিক্সড TP নেই,
    তাই বড় মুভগুলো cap না হয়ে যতদূর যায় ততদূর ধরা যায়। একই ক্যান্ডেলে SL হিট ও নতুন
    হাই/লো দুটোই সম্ভব হলে রক্ষণাত্মকভাবে SL আগে চেক করা হয়।"""
    end = min(idx + max_hold, len(c4) - 1)
    sl = initial_sl
    moved_be = False
    extreme = entry

    for j in range(idx + 1, end + 1):
        hi, lo = c4[j][2], c4[j][3]

        hit_sl = lo <= sl if direction == "bull" else hi >= sl
        if hit_sl:
            if not moved_be:
                outcome = "SL"
            elif abs(sl - entry) < 1e-9:
                outcome = "BE"
            else:
                outcome = "TRAIL"
            return outcome, sl, j - idx

        if direction == "bull":
            if hi > extreme:
                extreme = hi
            if not moved_be and extreme - entry >= BE_TRIGGER_ATR_MULT * atr_val:
                sl = max(sl, entry)
                moved_be = True
            if moved_be:
                sl = max(sl, extreme - TRAIL_ATR_MULT * atr_val)
        else:
            if lo < extreme:
                extreme = lo
            if not moved_be and entry - extreme >= BE_TRIGGER_ATR_MULT * atr_val:
                sl = min(sl, entry)
                moved_be = True
            if moved_be:
                sl = min(sl, extreme + TRAIL_ATR_MULT * atr_val)

    exit_price = c4[end][4]
    return "TIMEOUT", exit_price, end - idx


def trade_return_pct(direction, entry, exit_price):
    raw = (exit_price - entry) / entry * 100
    return raw if direction == "bull" else -raw


def backtest_market(key, cfg):
    name = cfg["name"]
    print(f"\n{'=' * 60}\n{name} — ডেটা আনা হচ্ছে...")
    c4 = fetch_candles(cfg["symbol"], "4h", OUTPUT_4H)
    print(f"{name}: {len(c4)}টা 4H ক্যান্ডেল পাওয়া গেছে")

    closes = [c[4] for c in c4]
    ema9 = ema_series(closes, EMA_FAST)
    ema15 = ema_series(closes, EMA_SLOW)
    adx = calc_adx(c4, ADX_PERIOD)
    rsi = calc_rsi(closes, RSI_PERIOD)
    atr = calc_atr(c4, ATR_PERIOD)

    warmup = max(EMA_SLOW, ADX_PERIOD, RSI_PERIOD, ATR_PERIOD) + 10
    trades = []
    next_free_idx = 0  # আগের ট্রেড খোলা থাকা অবস্থায় নতুন ট্রেড না নেওয়ার জন্য (overlap প্রতিরোধ)

    for idx in range(warmup, len(c4) - MAX_HOLD_CANDLES):
        if idx < next_free_idx:
            continue
        if adx[idx] is None or atr[idx] is None:
            continue

        direction = None
        regime = None
        if adx[idx] >= ADX_THRESHOLD and adx_rising(adx, idx):
            regime = "ট্রেন্ডিং (EMA)"
            direction = ema_crossover(ema9, ema15, idx)
        elif ENABLE_RANGE_REGIME and adx[idx] < ADX_THRESHOLD:
            regime = "রেঞ্জিং (RSI)"
            direction = rsi_reversal(rsi, idx)
        if not direction:
            continue

        entry = c4[idx][4]
        atr_val = atr[idx]
        initial_sl = entry - SL_ATR_MULT * atr_val if direction == "bull" else entry + SL_ATR_MULT * atr_val

        outcome, exit_price, bars = simulate_exit(c4, idx, direction, entry, initial_sl, atr_val)
        ret = trade_return_pct(direction, entry, exit_price)
        entry_time = dt.datetime.fromtimestamp(c4[idx][0], tz=dt.timezone.utc)

        trades.append(
            {"time": entry_time, "direction": direction, "regime": regime,
             "entry": entry, "outcome": outcome, "ret": ret, "bars": bars}
        )
        next_free_idx = idx + bars + 1

    days = OUTPUT_4H * 4 // 24
    print(f"{name}: মোট {len(trades)}টা ট্রেড (গত ~{days} দিনে)\n")
    for t in trades:
        arrow = "🟢 বাই" if t["direction"] == "bull" else "🔴 সেল"
        hold_hours = t["bars"] * 4
        print(
            f"  {t['time'].strftime('%Y-%m-%d %H:%M')} UTC — {arrow} [{t['regime']}] "
            f"@ {t['entry']:,.4f} — {t['outcome']} — রিটার্ন: {t['ret']:+.2f}% "
            f"({hold_hours}h পরে)"
        )

    if trades:
        wins = [t for t in trades if t["ret"] > 0]
        losses = [t for t in trades if t["ret"] <= 0]
        total_ret = sum(t["ret"] for t in trades)
        avg_ret = total_ret / len(trades)
        win_rate = len(wins) / len(trades) * 100
        sl_count = sum(1 for t in trades if t["outcome"] == "SL")
        be_count = sum(1 for t in trades if t["outcome"] == "BE")
        trail_count = sum(1 for t in trades if t["outcome"] == "TRAIL")
        timeout_count = sum(1 for t in trades if t["outcome"] == "TIMEOUT")

        print(f"\n  মোট ট্রেড: {len(trades)} | Win rate: {len(wins)}/{len(trades)} ({win_rate:.0f}%)")
        print(f"  ট্রেইলে উইন: {trail_count} | ব্রেক-ইভেন: {be_count} | SL হিট: {sl_count} | Timeout: {timeout_count}")
        print(f"  গড় রিটার্ন/ট্রেড: {avg_ret:+.2f}% | সব ট্রেড যোগ করলে মোট: {total_ret:+.2f}%")

        for label, subset in [("ট্রেন্ডিং (EMA)", [t for t in trades if "ট্রেন্ডিং" in t["regime"]]),
                               ("রেঞ্জিং (RSI)", [t for t in trades if "রেঞ্জিং" in t["regime"]])]:
            if subset:
                w = sum(1 for t in subset if t["ret"] > 0)
                avg = sum(t["ret"] for t in subset) / len(subset)
                print(f"  শুধু {label}: {w}/{len(subset)} win ({w / len(subset) * 100:.0f}%), গড় রিটার্ন {avg:+.2f}%")
    return trades


def main():
    all_trades = {}
    for key, cfg in MARKETS.items():
        try:
            all_trades[key] = backtest_market(key, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"{cfg['name']}: ত্রুটি - {e}")

    print(f"\n{'=' * 60}\nসারসংক্ষেপ")
    for key, cfg in MARKETS.items():
        trades = all_trades.get(key, [])
        if not trades:
            print(f"  {cfg['name']}: 0টা ট্রেড")
            continue
        wins = sum(1 for t in trades if t["ret"] > 0)
        total = sum(t["ret"] for t in trades)
        print(f"  {cfg['name']}: {len(trades)}টা ট্রেড, win rate {wins}/{len(trades)}, মোট রিটার্ন {total:+.2f}%")


if __name__ == "__main__":
    main()
    
