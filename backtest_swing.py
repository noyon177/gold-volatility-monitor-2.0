"""EMA ট্রেন্ড রেজিম সিগন্যাল + ATR-ভিত্তিক ব্রেক-ইভেন/ট্রেইলিং-স্টপের সুইং ব্যাকটেস্ট — v2

আগের ভার্সনের রেজাল্ট বিশ্লেষণ করে এই ৩টা উন্নতি যোগ করা হয়েছে:

  1. প্রতি মার্কেটে আলাদা ADX থ্রেশহোল্ড — GBP/USD-এর মতো তুলনামূলক কম-ট্রেন্ডিং পেয়ারে
     ফ্ল্যাট আগের থ্রেশহোল্ড (২৫) দিয়ে অনেক দুর্বল/false ট্রেন্ড সিগন্যাল ধরা পড়ছিল, যেটা
     আগের রানে GBP/USD-এ -১৪.২৪% মোট রিটার্ন আর -১৫% ড্রডাউনের একটা কারণ হতে পারে।
     ADX_THRESHOLD_OVERRIDE দিয়ে প্রতি মার্কেটে আলাদা মান সেট করার সুযোগ রাখা হলো, যাতে
     GBP/USD-এর মতো পেয়ারে কড়া ফিল্টার আর XAU/BTC-এর মতো বেশি ট্রেন্ডি মার্কেটে আগের মতোই
     রাখা যায়।
  2. Walk-forward split — পুরো ডেটাকে প্রথম ৭০% (train/in-sample) আর শেষ ৩০% (test/
     out-of-sample) এ ভাগ করে আলাদা করে পারফরম্যান্স রিপোর্ট করা হয়। রেজাল্ট যদি শুধু
     train অংশে ভালো হয় আর test অংশে খারাপ/উল্টো হয়, সেটা overfitting-এর সিগন্যাল —
     আগের সিঙ্গেল-রান রেজাল্ট দিয়ে এটা বোঝা যাচ্ছিল না।
  3. Combined portfolio drawdown — একই সময়ে ৪টা মার্কেটেই পজিশন খোলা থাকতে পারে (কোড
     শুধু একই মার্কেটের মধ্যে overlap আটকায়, মার্কেট-জুড়ে না)। তাই সব মার্কেটের ট্রেড
     তারিখ অনুযায়ী একসাথে সাজিয়ে একটা কম্বাইন্ড ইকুইটি কার্ভ ও ড্রডাউন হিসাব করা হয়েছে —
     এটাই আসল পোর্টফোলিও-লেভেল রিস্ক, এক-এক মার্কেটের আলাদা ড্রডাউনের চেয়ে বেশি বাস্তবসম্মত।

চালানোর নিয়ম: TWELVE_DATA_API_KEY=xxxx python backtest_swing_v2.py
"""
import datetime as dt
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

CANDLE_INTERVAL = "1day"
OUTPUT_SIZE = 2500

EMA_FAST = 20
EMA_SLOW = 50
ADX_PERIOD = 14
ADX_THRESHOLD = 25          # ডিফল্ট — যে মার্কেটের জন্য override নেই তার জন্য প্রযোজ্য
# (নতুন) প্রতি মার্কেটে আলাদা ADX থ্রেশহোল্ড — GBP/USD-এ কড়া ফিল্টার
ADX_THRESHOLD_OVERRIDE = {
    "GBPUSD": 30,   # আগের রানে GBP/USD-এ সবচেয়ে খারাপ ফল — উচ্চতর থ্রেশহোল্ড দিয়ে
                     # দুর্বল ট্রেন্ডে এন্ট্রি কমানো হলো
    "USDJPY": 27,
}
ADX_RISING_LOOKBACK = 5
ENABLE_RANGE_REGIME = False
TREND_ENTRY_MODE = "pullback"
PULLBACK_BODY_CONFIRM = True
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
ATR_PERIOD = 14
SL_ATR_MULT = 2.0
BE_TRIGGER_ATR_MULT = 1.5
TRAIL_ATR_MULT = 2.5
MAX_HOLD_CANDLES = 40

TRAIN_SPLIT_RATIO = 0.7     # (নতুন) walk-forward split: প্রথম ৭০% train, শেষ ৩০% test

ROUND_TRIP_COST_PCT = {
    "XAUUSD": 0.10,
    "BTCUSD": 0.05,
    "USDJPY": 0.02,
    "GBPUSD": 0.02,
}

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
    if idx < lookback:
        return False
    a0, a1 = adx[idx - lookback], adx[idx]
    if a0 is None or a1 is None:
        return False
    return a1 > a0


def pullback_signal(c4, ema_fast_vals, ema_slow_vals, adx, idx, adx_threshold):
    ef, es, a = ema_fast_vals[idx], ema_slow_vals[idx], adx[idx]
    if ef is None or es is None or a is None or a < adx_threshold:
        return None
    o, hi, lo, cl = c4[idx][1], c4[idx][2], c4[idx][3], c4[idx][4]
    if ef > es:
        if lo <= ef and cl > ef and (not PULLBACK_BODY_CONFIRM or cl > o):
            return "bull"
    else:
        if hi >= ef and cl < ef and (not PULLBACK_BODY_CONFIRM or cl < o):
            return "bear"
    return None


def simulate_exit(c4, idx, direction, entry, initial_sl, atr_val, max_hold=MAX_HOLD_CANDLES):
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


def summarize(trades, label):
    """(নতুন) একগুচ্ছ ট্রেডের জন্য win-rate/গড় রিটার্ন/ড্রডাউন প্রিন্ট করার হেল্পার —
    আগে শুধু পুরো ডেটাসেটের জন্য ছিল, এখন train/test দুটোর জন্যই ব্যবহার করা হয়।"""
    if not trades:
        print(f"    {label}: 0টা ট্রেড")
        return
    wins = [t for t in trades if t["ret"] > 0]
    total_ret = sum(t["ret"] for t in trades)
    avg_ret = total_ret / len(trades)
    win_rate = len(wins) / len(trades) * 100
    print(f"    {label}: {len(trades)}টা ট্রেড | win rate {len(wins)}/{len(trades)} ({win_rate:.0f}%) "
          f"| গড় রিটার্ন {avg_ret:+.2f}% | মোট রিটার্ন {total_ret:+.2f}%")


def backtest_market(key, cfg):
    name = cfg["name"]
    adx_threshold = ADX_THRESHOLD_OVERRIDE.get(key, ADX_THRESHOLD)
    print(f"\n{'=' * 60}\n{name} — ডেটা আনা হচ্ছে... (ADX থ্রেশহোল্ড: {adx_threshold})")
    c4 = fetch_candles(cfg["symbol"], CANDLE_INTERVAL, OUTPUT_SIZE)
    print(f"{name}: {len(c4)}টা দৈনিক ক্যান্ডেল পাওয়া গেছে")

    closes = [c[4] for c in c4]
    ema9 = ema_series(closes, EMA_FAST)
    ema15 = ema_series(closes, EMA_SLOW)
    adx = calc_adx(c4, ADX_PERIOD)
    rsi = calc_rsi(closes, RSI_PERIOD)
    atr = calc_atr(c4, ATR_PERIOD)

    warmup = max(EMA_SLOW, ADX_PERIOD, RSI_PERIOD, ATR_PERIOD) + 10
    scan_range = range(warmup, len(c4) - MAX_HOLD_CANDLES)

    trades = []
    next_free_idx = 0

    for idx in scan_range:
        if idx < next_free_idx:
            continue
        if adx[idx] is None or atr[idx] is None:
            continue

        direction = None
        regime = None
        if adx[idx] is not None and adx[idx] >= adx_threshold:
            if TREND_ENTRY_MODE == "pullback":
                regime = "ট্রেন্ডিং (পুলব্যাক)"
                direction = pullback_signal(c4, ema9, ema15, adx, idx, adx_threshold)
            else:
                regime = "ট্রেন্ডিং (EMA)"
                direction = ema_crossover(ema9, ema15, idx) if adx_rising(adx, idx) else None
        elif ENABLE_RANGE_REGIME and adx[idx] is not None and adx[idx] < adx_threshold:
            regime = "রেঞ্জিং (RSI)"
            direction = rsi_reversal(rsi, idx)
        if not direction:
            continue

        entry = c4[idx][4]
        atr_val = atr[idx]
        initial_sl = entry - SL_ATR_MULT * atr_val if direction == "bull" else entry + SL_ATR_MULT * atr_val

        outcome, exit_price, bars = simulate_exit(c4, idx, direction, entry, initial_sl, atr_val)
        ret = trade_return_pct(direction, entry, exit_price) - ROUND_TRIP_COST_PCT.get(key, 0.0)
        entry_time = dt.datetime.fromtimestamp(c4[idx][0], tz=dt.timezone.utc)

        trades.append(
            {"time": entry_time, "market": key, "direction": direction, "regime": regime,
             "entry": entry, "outcome": outcome, "ret": ret, "bars": bars}
        )
        next_free_idx = idx + bars + 1

    days = int((c4[-1][0] - c4[0][0]) / 86400) if len(c4) > 1 else 0
    print(f"{name}: মোট {len(trades)}টা ট্রেড (গত ~{days} দিনে)")
    for t in trades:
        arrow = "🟢 বাই" if t["direction"] == "bull" else "🔴 সেল"
        print(
            f"  {t['time'].strftime('%Y-%m-%d')} — {arrow} [{t['regime']}] "
            f"@ {t['entry']:,.4f} — {t['outcome']} — রিটার্ন: {t['ret']:+.2f}% "
            f"({t['bars']} দিন পরে)"
        )

    if trades:
        summarize(trades, "সম্পূর্ণ ডেটাসেট")

        # (নতুন) walk-forward split: সময় অনুযায়ী প্রথম TRAIN_SPLIT_RATIO অংশ train, বাকিটা test
        split_i = int(len(trades) * TRAIN_SPLIT_RATIO)
        train_trades, test_trades = trades[:split_i], trades[split_i:]
        print("  Walk-forward split:")
        summarize(train_trades, f"  Train (প্রথম {int(TRAIN_SPLIT_RATIO*100)}%)")
        summarize(test_trades, f"  Test  (শেষ {int((1-TRAIN_SPLIT_RATIO)*100)}%, out-of-sample)")

        sl_count = sum(1 for t in trades if t["outcome"] == "SL")
        be_count = sum(1 for t in trades if t["outcome"] == "BE")
        trail_count = sum(1 for t in trades if t["outcome"] == "TRAIL")
        timeout_count = sum(1 for t in trades if t["outcome"] == "TIMEOUT")
        print(f"  ট্রেইলে উইন: {trail_count} | ব্রেক-ইভেন: {be_count} | SL হিট: {sl_count} | Timeout: {timeout_count}")

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        cur_loss_streak = 0
        max_loss_streak = 0
        for t in trades:
            equity += t["ret"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            if t["ret"] <= 0:
                cur_loss_streak += 1
                max_loss_streak = max(max_loss_streak, cur_loss_streak)
            else:
                cur_loss_streak = 0
        print(f"  একক-মার্কেট সর্বোচ্চ ড্রডাউন: -{max_dd:.2f}% | সবচেয়ে বেশি টানা লস: {max_loss_streak}টা ট্রেড")
    return trades


def combined_portfolio_report(all_trades):
    """(নতুন) সব মার্কেটের ট্রেড একসাথে তারিখ অনুযায়ী সাজিয়ে কম্বাইন্ড ইকুইটি কার্ভ ও
    ড্রডাউন হিসাব করে — এটাই আসল পোর্টফোলিও রিস্ক, কারণ আলাদা মার্কেটে একই সময়ে পজিশন
    খোলা থাকতে পারে এবং তাদের লস একসাথে যোগ হতে পারে।"""
    flat = [t for trades in all_trades.values() for t in trades]
    if not flat:
        return
    flat.sort(key=lambda t: t["time"])

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_time = flat[0]["time"]
    dd_start, dd_end = None, None
    for t in flat:
        equity += t["ret"]
        if equity > peak:
            peak = equity
            peak_time = t["time"]
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            dd_start, dd_end = peak_time, t["time"]

    print(f"\n{'=' * 60}\nপোর্টফোলিও-লেভেল রিপোর্ট (৪টা মার্কেট একসাথে, তারিখ অনুযায়ী)")
    print(f"  মোট ট্রেড (সব মার্কেট): {len(flat)}")
    print(f"  কম্বাইন্ড মোট রিটার্ন: {equity:+.2f}%")
    print(f"  কম্বাইন্ড সর্বোচ্চ ড্রডাউন: -{max_dd:.2f}%"
          + (f" ({dd_start.strftime('%Y-%m-%d')} থেকে {dd_end.strftime('%Y-%m-%d')})" if dd_start else ""))
    print("  (এই ড্রডাউন একক-মার্কেট রিপোর্টের চেয়ে বেশি গুরুত্বপূর্ণ যদি একসাথে সব মার্কেটে ট্রেড করার প্ল্যান থাকে)")


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

    combined_portfolio_report(all_trades)


if __name__ == "__main__":
    main()
  
