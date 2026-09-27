"""EMA পুলব্যাক ট্রেন্ড-ফলোয়িং (সুইং SL/TP) + RSI রেঞ্জ রিভার্সাল — ব্যাকটেস্ট (v4)

আগের ভার্সনগুলো (v1-v3) ADX-ভিত্তিক ট্রেন্ড ফিল্টার আর ATR-ভিত্তিক স্টপ/ট্রেইলিং দিয়ে
তৈরি ছিল, যেটা বারবার প্যারামিটার/রিস্ক-লেয়ার পরিবর্তনেও স্থিতিশীল এজ দেখাতে পারেনি।

v4-তে সম্পূর্ণ নতুন এন্ট্রি-এক্সিট লজিক:

  রেজিম নির্ধারণ (ADX বাদ):
    - EMA_FAST > EMA_SLOW এবং EMA_FAST উপরের দিকে উঠছে (TREND_SLOPE_LOOKBACK ক্যান্ডেল
      আগের চেয়ে বেশি) → আপট্রেন্ড
    - বিপরীত হলে ডাউনট্রেন্ড
    - কোনোটাই না হলে (EMA দুটো কাছাকাছি/ফ্ল্যাট) → রেঞ্জ

  ট্রেন্ড মার্কেটে এন্ট্রি (পুলব্যাক):
    - আপট্রেন্ডে দাম fast EMA-তে পুলব্যাক করে (low <= EMA) আবার বন্ধ হয় EMA-এর ওপরে,
      আর ক্যান্ডেলের বডি দিয়ে সেটা কনফার্ম হয় (close > open) — তাহলে বাই সিগন্যাল
    - ডাউনট্রেন্ডে মিরর — সেল সিগন্যাল
    - SL = সর্বশেষ কনফার্মড সুইং লো (আপট্রেন্ড) / সুইং হাই (ডাউনট্রেন্ড)
    - TP = তার আগের সুইং হাই (আপট্রেন্ড) / সুইং লো (ডাউনট্রেন্ড) — অর্থাৎ পরবর্তী
      স্ট্রাকচারাল লেভেল পর্যন্ত টার্গেট, ফিক্সড R-মাল্টিপল না

  রেঞ্জ মার্কেটে এন্ট্রি (RSI + প্রাইস অ্যাকশন):
    - RSI ওভারসোল্ড থেকে উপরে ক্রস করলে (৩০-এর নিচ থেকে ওপরে) আর ক্যান্ডেল বুলিশ হলে
      (close > open) → বাই, টার্গেট রেঞ্জের ওপরের সুইং হাই, SL রেঞ্জের সুইং লো-এর
      একটু নিচে (বাফার সহ)
    - RSI ওভারবট থেকে নিচে ক্রস করলে মিরর → সেল

  সুইং হাই/লো একটা ফ্র্যাক্টাল উইন্ডো (SWING_LOOKBACK ক্যান্ডেল ডানে-বামে) দিয়ে বের করা
  হয়, কিন্তু ট্রেডিং সিদ্ধান্তে ব্যবহার করা হয় শুধু "কনফার্মড" সুইং পয়েন্ট — অর্থাৎ সেই
  পয়েন্টের পরের SWING_LOOKBACK ক্যান্ডেল পার হওয়ার পরেই সেটা রেফারেন্স হিসেবে ব্যবহার করা
  হয়, যাতে ভবিষ্যতের ডেটা দেখে সিদ্ধান্ত নেওয়ার (lookahead bias) ভুল না হয়।

  MIN_REWARD_RISK_RATIO দিয়ে খুব খারাপ risk:reward-এর ট্রেড (যেমন SL/TP প্রায় কাছাকাছি)
  বাদ দেওয়া হয়।

চালানোর নিয়ম: TWELVE_DATA_API_KEY=xxxx python backtest_swing_v4.py
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
TREND_SLOPE_LOOKBACK = 10     # এই কয়টা ক্যান্ডেল আগের তুলনায় EMA_FAST-এর ঢাল দেখে ট্রেন্ড ঠিক হয়

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

SWING_LOOKBACK = 5            # ফ্র্যাক্টাল সুইং হাই/লো — প্রতিদিকে এতগুলো ক্যান্ডেল
PULLBACK_BODY_CONFIRM = True

MIN_REWARD_RISK_RATIO = 1.0   # এর কম R:R হলে ট্রেড বাদ
RANGE_SL_BUFFER_PCT = 0.3     # রেঞ্জ ট্রেডে সুইং লেভেলের বাইরে এই % বাফার
MAX_HOLD_CANDLES = 40

ROUND_TRIP_COST_PCT = {
    "XAUUSD": 0.10,
    "BTCUSD": 0.05,
}

MARKETS = {
    "XAUUSD": {"name": "গোল্ড (XAU/USD)", "symbol": "XAU/USD"},
    "BTCUSD": {"name": "বিটকয়েন (BTC/USD)", "symbol": "BTC/USD"},
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


def wilder_avg(values, period):
    n = len(values)
    out = [None] * n
    if n <= period:
        return out
    out[period] = sum(values[1:period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


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


def find_confirmed_swings(c4, lookback=SWING_LOOKBACK):
    """ফ্র্যাক্টাল সুইং হাই/লো বের করে, কিন্তু প্রতিটা ইনডেক্সের জন্য রিটার্ন করে শুধু সেই
    সুইং পয়েন্ট যেটা ততদিনে "কনফার্মড" (অর্থাৎ তার ডানপাশের lookback ক্যান্ডেলও পার হয়ে
    গেছে) — যাতে ব্যাকটেস্টে ভবিষ্যৎ ডেটা ব্যবহার না হয়ে যায়।"""
    n = len(c4)
    raw_high = [None] * n
    raw_low = [None] * n
    for i in range(lookback, n - lookback):
        hi_window = [c4[j][2] for j in range(i - lookback, i + lookback + 1)]
        if c4[i][2] == max(hi_window):
            raw_high[i] = c4[i][2]
        lo_window = [c4[j][3] for j in range(i - lookback, i + lookback + 1)]
        if c4[i][3] == min(lo_window):
            raw_low[i] = c4[i][3]

    confirmed_high = [None] * n
    confirmed_low = [None] * n
    last_high = None
    last_low = None
    for idx in range(n):
        check_idx = idx - lookback
        if check_idx >= 0:
            if raw_high[check_idx] is not None:
                last_high = raw_high[check_idx]
            if raw_low[check_idx] is not None:
                last_low = raw_low[check_idx]
        confirmed_high[idx] = last_high
        confirmed_low[idx] = last_low
    return confirmed_high, confirmed_low


def trend_regime(ema_fast, ema_slow, idx, slope_lookback=TREND_SLOPE_LOOKBACK):
    if idx < slope_lookback:
        return None
    ef, es = ema_fast[idx], ema_slow[idx]
    ef_prev = ema_fast[idx - slope_lookback]
    if None in (ef, es, ef_prev):
        return None
    if ef > es and ef > ef_prev:
        return "up"
    if ef < es and ef < ef_prev:
        return "down"
    return "range"


def trend_pullback_signal(c4, ema_fast, regime, idx):
    ef = ema_fast[idx]
    if ef is None:
        return None
    o, hi, lo, cl = c4[idx][1], c4[idx][2], c4[idx][3], c4[idx][4]
    if regime == "up":
        if lo <= ef and cl > ef and (not PULLBACK_BODY_CONFIRM or cl > o):
            return "bull"
    elif regime == "down":
        if hi >= ef and cl < ef and (not PULLBACK_BODY_CONFIRM or cl < o):
            return "bear"
    return None


def range_rsi_signal(c4, rsi, idx):
    if idx < 1:
        return None
    r0, r1 = rsi[idx - 1], rsi[idx]
    if r0 is None or r1 is None:
        return None
    o, cl = c4[idx][1], c4[idx][4]
    if r0 < RSI_OVERSOLD <= r1 and cl > o:
        return "bull"
    if r0 > RSI_OVERBOUGHT >= r1 and cl < o:
        return "bear"
    return None


def trade_return_pct(direction, entry, exit_price):
    raw = (exit_price - entry) / entry * 100
    return raw if direction == "bull" else -raw


def simulate_fixed_exit(c4, idx, direction, entry, sl, tp, max_hold=MAX_HOLD_CANDLES):
    """SL/TP ফিক্সড রাখা হয় (কোনো ট্রেইলিং/ব্রেক-ইভেন নেই — সুইং-লেভেল স্ট্রাকচার একবার
    ঠিক হলে সেটাই ধরে রাখা হয়)। একই ক্যান্ডেলে SL ও TP দুটোই সম্ভব হলে রক্ষণাত্মকভাবে
    SL আগে ধরা হয়।"""
    end = min(idx + max_hold, len(c4) - 1)
    for j in range(idx + 1, end + 1):
        hi, lo = c4[j][2], c4[j][3]
        if direction == "bull":
            if lo <= sl:
                return "SL", sl, j - idx
            if hi >= tp:
                return "TP", tp, j - idx
        else:
            if hi >= sl:
                return "SL", sl, j - idx
            if lo <= tp:
                return "TP", tp, j - idx
    return "TIMEOUT", c4[end][4], end - idx


def backtest_market(key, cfg):
    name = cfg["name"]
    print(f"\n{'=' * 60}\n{name} — ডেটা আনা হচ্ছে...")
    c4 = fetch_candles(cfg["symbol"], CANDLE_INTERVAL, OUTPUT_SIZE)
    print(f"{name}: {len(c4)}টা দৈনিক ক্যান্ডেল পাওয়া গেছে")

    closes = [c[4] for c in c4]
    ema_f = ema_series(closes, EMA_FAST)
    ema_s = ema_series(closes, EMA_SLOW)
    rsi = calc_rsi(closes, RSI_PERIOD)
    swing_high, swing_low = find_confirmed_swings(c4, SWING_LOOKBACK)

    warmup = max(EMA_SLOW, RSI_PERIOD) + TREND_SLOPE_LOOKBACK + 10
    scan_range = range(warmup, len(c4) - MAX_HOLD_CANDLES)

    trades = []
    trend_signal_count = 0
    range_signal_count = 0
    skipped_bad_rr = 0
    next_free_idx = 0

    for idx in scan_range:
        if idx < next_free_idx:
            continue

        regime = trend_regime(ema_f, ema_s, idx)
        if regime is None:
            continue

        direction = None
        mode = None
        sl = tp = None
        entry = c4[idx][4]

        if regime in ("up", "down"):
            direction = trend_pullback_signal(c4, ema_f, regime, idx)
            if direction:
                mode = "ট্রেন্ড-পুলব্যাক"
                if direction == "bull":
                    sl, tp = swing_low[idx], swing_high[idx]
                else:
                    sl, tp = swing_high[idx], swing_low[idx]
        else:  # range
            direction = range_rsi_signal(c4, rsi, idx)
            if direction:
                mode = "রেঞ্জ-RSI"
                if direction == "bull":
                    sl = swing_low[idx] * (1 - RANGE_SL_BUFFER_PCT / 100) if swing_low[idx] else None
                    tp = swing_high[idx]
                else:
                    sl = swing_high[idx] * (1 + RANGE_SL_BUFFER_PCT / 100) if swing_high[idx] else None
                    tp = swing_low[idx]

        if not direction:
            continue
        if mode == "ট্রেন্ড-পুলব্যাক":
            trend_signal_count += 1
        else:
            range_signal_count += 1

        if sl is None or tp is None:
            continue
        # বৈধতা: SL/TP সঠিক দিকে আছে কিনা, আর reward:risk যথেষ্ট কিনা
        if direction == "bull":
            if not (sl < entry < tp):
                continue
            risk = entry - sl
            reward = tp - entry
        else:
            if not (tp < entry < sl):
                continue
            risk = sl - entry
            reward = entry - tp
        if risk <= 0 or reward / risk < MIN_REWARD_RISK_RATIO:
            skipped_bad_rr += 1
            continue

        outcome, exit_price, bars = simulate_fixed_exit(c4, idx, direction, entry, sl, tp)
        ret = trade_return_pct(direction, entry, exit_price) - ROUND_TRIP_COST_PCT.get(key, 0.0)
        entry_time = dt.datetime.fromtimestamp(c4[idx][0], tz=dt.timezone.utc)

        trades.append({
            "time": entry_time, "market": key, "direction": direction, "mode": mode,
            "entry": entry, "sl": sl, "tp": tp, "outcome": outcome, "ret": ret, "bars": bars,
        })
        next_free_idx = idx + bars + 1

    print(f"{name}: ট্রেন্ড-সিগন্যাল {trend_signal_count} | রেঞ্জ-সিগন্যাল {range_signal_count} | "
          f"খারাপ R:R বাদ {skipped_bad_rr} | মোট ট্রেড নেওয়া হয়েছে {len(trades)}\n")
    for t in trades:
        arrow = "🟢 বাই" if t["direction"] == "bull" else "🔴 সেল"
        print(f"  {t['time'].strftime('%Y-%m-%d')} — {arrow} [{t['mode']}] @ {t['entry']:,.4f} "
              f"(SL {t['sl']:,.4f} / TP {t['tp']:,.4f}) — {t['outcome']} — রিটার্ন: {t['ret']:+.2f}% "
              f"({t['bars']} দিন পরে)")

    if trades:
        wins = [t for t in trades if t["ret"] > 0]
        total_ret = sum(t["ret"] for t in trades)
        print(f"\n  মোট ট্রেড: {len(trades)} | win rate {len(wins)}/{len(trades)} "
              f"({len(wins)/len(trades)*100:.0f}%) | গড় রিটার্ন {total_ret/len(trades):+.2f}% "
              f"| মোট রিটার্ন {total_ret:+.2f}%")
        for label in ("ট্রেন্ড-পুলব্যাক", "রেঞ্জ-RSI"):
            subset = [t for t in trades if t["mode"] == label]
            if subset:
                w = sum(1 for t in subset if t["ret"] > 0)
                print(f"    শুধু {label}: {len(subset)}টা, win {w}/{len(subset)} "
                      f"({w/len(subset)*100:.0f}%), গড় রিটার্ন {sum(t['ret'] for t in subset)/len(subset):+.2f}%")

        equity, peak, max_dd = 0.0, 0.0, 0.0
        for t in trades:
            equity += t["ret"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        print(f"  সর্বোচ্চ ড্রডাউন: -{max_dd:.2f}%")
    return trades


def main():
    all_trades = {}
    for key, cfg in MARKETS.items():
        try:
            all_trades[key] = backtest_market(key, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"{cfg['name']}: ত্রুটি - {e}")

    print(f"\n{'=' * 60}\nসারসংক্ষেপ")
    flat = []
    for key, cfg in MARKETS.items():
        trades = all_trades.get(key, [])
        if not trades:
            print(f"  {cfg['name']}: 0টা ট্রেড")
            continue
        wins = sum(1 for t in trades if t["ret"] > 0)
        total = sum(t["ret"] for t in trades)
        print(f"  {cfg['name']}: {len(trades)}টা ট্রেড, win rate {wins}/{len(trades)}, মোট রিটার্ন {total:+.2f}%")
        flat.extend(trades)

    if flat:
        flat.sort(key=lambda t: t["time"])
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for t in flat:
            equity += t["ret"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        print(f"\n  কম্বাইন্ড (XAU+BTC) মোট রিটার্ন: {equity:+.2f}% | কম্বাইন্ড সর্বোচ্চ ড্রডাউন: -{max_dd:.2f}%")


if __name__ == "__main__":
    main()
