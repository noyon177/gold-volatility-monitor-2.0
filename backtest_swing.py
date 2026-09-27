"""EMA ট্রেন্ড রেজিম + ATR ব্রেক-ইভেন/ট্রেইলিং স্টপ — পোর্টফোলিও-লেভেল ব্যাকটেস্ট (v3)

আগের ভার্সনে (v2) প্রতিটা মার্কেট আলাদা-আলাদা ব্যাকটেস্ট করে শেষে শুধু রিটার্ন যোগ করা
হতো — কিন্তু কম্বাইন্ড রিপোর্টে দেখা গিয়েছিল পোর্টফোলিও ড্রডাউন (-৭৫%) একক-মার্কেট
ড্রডাউনের (~-১৫%) চেয়ে অনেক বেশি, কারণ মার্কেটগুলোর লস একসাথে ক্লাস্টার করছিল।

এই ভার্সনে (v3) পরিবর্তন:
  1. শুধু ২টা মার্কেট — গোল্ড (XAU/USD) আর বিটকয়েন (BTC/USD)। বাকিগুলো বাদ।
  2. সিমুলেশন এখন সত্যিকারের পোর্টফোলিও-লেভেল, দিন-ধরে-দিন (event-driven): দুটো মার্কেটের
     ক্যান্ডেল একসাথে তারিখ অনুযায়ী প্রসেস করা হয়, কারণ নতুন এন্ট্রি নেওয়া যাবে কিনা সেটা
     এখন অন্য মার্কেটে কী চলছে তার ওপর নির্ভর করে (আগের ভার্সনে প্রতিটা মার্কেট স্বাধীনভাবে
     চলত, তাই এই নির্ভরতা মডেল করা যেত না)।
  3. MAX_CONCURRENT_POSITIONS — একই সময়ে সর্বোচ্চ এতগুলো পজিশন খোলা থাকতে পারবে।
  4. CORRELATION_FILTER — অন্য মার্কেটে ইতিমধ্যে একই দিকে (বাই/সেল) পজিশন খোলা থাকলে নতুন
     একই-দিকের এন্ট্রি নেওয়া হবে না (ক্লাস্টারড লস কমানোর জন্য)।
  5. Equity-drawdown cooldown — পোর্টফোলিও রিয়েলাইজড ইকুইটি peak থেকে COOLDOWN_TRIGGER_DD_PCT
     শতাংশ নিচে নামলে নতুন এন্ট্রি বন্ধ থাকে, যতক্ষণ না ড্রডাউন কমে COOLDOWN_RESUME_DD_PCT-এ
     ফিরে আসে (hysteresis, যাতে সীমানার কাছে বারবার চালু-বন্ধ না হয়)।

চালানোর নিয়ম: TWELVE_DATA_API_KEY=xxxx python backtest_swing_v3.py
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
ADX_THRESHOLD = 25
ADX_THRESHOLD_OVERRIDE = {}   # XAU/BTC দুটোই ট্রেন্ডি মার্কেট বলে ডিফল্টই রাখা হলো
ADX_RISING_LOOKBACK = 5
TREND_ENTRY_MODE = "pullback"
PULLBACK_BODY_CONFIRM = True
ATR_PERIOD = 14
SL_ATR_MULT = 2.0
BE_TRIGGER_ATR_MULT = 1.5
TRAIL_ATR_MULT = 2.5
MAX_HOLD_CANDLES = 40

# ---- নতুন: পোর্টফোলিও-লেভেল রিস্ক কন্ট্রোল ----
MAX_CONCURRENT_POSITIONS = 2     # ২টা মার্কেট বলে এটা কার্যত "উভয়ই একসাথে চলতে পারবে";
                                  # ভবিষ্যতে মার্কেট বাড়ালে এটা আসল সীমা হিসেবে কাজ করবে
CORRELATION_FILTER = True        # একই দিকে একাধিক মার্কেটে একসাথে পজিশন নিষেধ
COOLDOWN_TRIGGER_DD_PCT = 15.0   # পোর্টফোলিও ড্রডাউন এই % ছাড়ালে নতুন এন্ট্রি বন্ধ
COOLDOWN_RESUME_DD_PCT = 5.0     # ড্রডাউন এই %-এ নেমে এলে আবার এন্ট্রি চালু

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


def trade_return_pct(direction, entry, exit_price):
    raw = (exit_price - entry) / entry * 100
    return raw if direction == "bull" else -raw


def load_market_data(key, cfg):
    print(f"{cfg['name']} — ডেটা আনা হচ্ছে...")
    c4 = fetch_candles(cfg["symbol"], CANDLE_INTERVAL, OUTPUT_SIZE)
    closes = [c[4] for c in c4]
    ema_f = ema_series(closes, EMA_FAST)
    ema_s = ema_series(closes, EMA_SLOW)
    adx = calc_adx(c4, ADX_PERIOD)
    atr = calc_atr(c4, ATR_PERIOD)
    warmup = max(EMA_SLOW, ADX_PERIOD, ATR_PERIOD) + 10
    date_idx = {}
    for i in range(warmup, len(c4)):
        date_str = dt.datetime.fromtimestamp(c4[i][0], tz=dt.timezone.utc).strftime("%Y-%m-%d")
        date_idx[date_str] = i
    print(f"{cfg['name']}: {len(c4)}টা ক্যান্ডেল, স্ক্যান শুরু {date_idx and sorted(date_idx)[0]} থেকে")
    return {"c4": c4, "ema_f": ema_f, "ema_s": ema_s, "adx": adx, "atr": atr, "date_idx": date_idx}


def run_portfolio_backtest(all_data):
    all_dates = sorted(set().union(*[d["date_idx"].keys() for d in all_data.values()]))

    open_trades = {}      # market -> trade dict
    trades_log = []        # বন্ধ হওয়া সব ট্রেড
    equity = 0.0
    peak = 0.0
    in_cooldown = False
    cooldown_activations = 0
    skipped_for_cooldown = 0
    skipped_for_correlation = 0
    skipped_for_max_positions = 0

    for date in all_dates:
        for market, data in all_data.items():
            if date not in data["date_idx"]:
                continue
            idx = data["date_idx"][date]
            c4, adx, atr = data["c4"], data["adx"], data["atr"]

            if market in open_trades:
                trade = open_trades[market]
                hi, lo, cl = c4[idx][2], c4[idx][3], c4[idx][4]
                direction = trade["direction"]
                sl = trade["sl"]
                trade["bars"] += 1
                hit_sl = lo <= sl if direction == "bull" else hi >= sl
                timed_out = trade["bars"] >= MAX_HOLD_CANDLES

                if hit_sl or timed_out:
                    if hit_sl:
                        exit_price = sl
                        outcome = "SL" if not trade["moved_be"] else (
                            "BE" if abs(sl - trade["entry"]) < 1e-9 else "TRAIL")
                    else:
                        exit_price = cl
                        outcome = "TIMEOUT"
                    ret = trade_return_pct(direction, trade["entry"], exit_price) - ROUND_TRIP_COST_PCT.get(market, 0.0)
                    equity += ret
                    peak = max(peak, equity)
                    dd = peak - equity
                    if dd >= COOLDOWN_TRIGGER_DD_PCT and not in_cooldown:
                        in_cooldown = True
                        cooldown_activations += 1
                    elif dd <= COOLDOWN_RESUME_DD_PCT and in_cooldown:
                        in_cooldown = False
                    trades_log.append({
                        "market": market, "direction": direction, "entry": trade["entry"],
                        "entry_time": trade["entry_time"], "exit_time": date,
                        "outcome": outcome, "ret": ret, "bars": trade["bars"],
                    })
                    del open_trades[market]
                    continue
                else:
                    if direction == "bull":
                        if hi > trade["extreme"]:
                            trade["extreme"] = hi
                        if not trade["moved_be"] and trade["extreme"] - trade["entry"] >= BE_TRIGGER_ATR_MULT * trade["atr_val"]:
                            trade["sl"] = max(trade["sl"], trade["entry"])
                            trade["moved_be"] = True
                        if trade["moved_be"]:
                            trade["sl"] = max(trade["sl"], trade["extreme"] - TRAIL_ATR_MULT * trade["atr_val"])
                    else:
                        if lo < trade["extreme"]:
                            trade["extreme"] = lo
                        if not trade["moved_be"] and trade["entry"] - trade["extreme"] >= BE_TRIGGER_ATR_MULT * trade["atr_val"]:
                            trade["sl"] = min(trade["sl"], trade["entry"])
                            trade["moved_be"] = True
                        if trade["moved_be"]:
                            trade["sl"] = min(trade["sl"], trade["extreme"] + TRAIL_ATR_MULT * trade["atr_val"])
                    continue

            # কোনো ওপেন ট্রেড নেই এই মার্কেটে — নতুন এন্ট্রি সিগন্যাল চেক
            if adx[idx] is None or atr[idx] is None:
                continue
            threshold = ADX_THRESHOLD_OVERRIDE.get(market, ADX_THRESHOLD)
            direction = pullback_signal(c4, data["ema_f"], data["ema_s"], adx, idx, threshold)
            if not direction:
                continue

            if in_cooldown:
                skipped_for_cooldown += 1
                continue
            if len(open_trades) >= MAX_CONCURRENT_POSITIONS:
                skipped_for_max_positions += 1
                continue
            if CORRELATION_FILTER and any(t["direction"] == direction for t in open_trades.values()):
                skipped_for_correlation += 1
                continue

            entry = c4[idx][4]
            atr_val = atr[idx]
            sl = entry - SL_ATR_MULT * atr_val if direction == "bull" else entry + SL_ATR_MULT * atr_val
            open_trades[market] = {
                "direction": direction, "entry": entry, "sl": sl, "atr_val": atr_val,
                "moved_be": False, "extreme": entry, "bars": 0, "entry_time": date,
            }

    return trades_log, equity, cooldown_activations, skipped_for_cooldown, skipped_for_correlation, skipped_for_max_positions


def print_report(trades_log, final_equity, cooldown_activations, skip_cd, skip_corr, skip_max):
    print(f"\n{'=' * 60}\nপোর্টফোলিও রিপোর্ট (XAU + BTC, একসাথে, ম্যাক্স {MAX_CONCURRENT_POSITIONS} পজিশন)")
    print(f"মোট ট্রেড: {len(trades_log)}")

    for market in MARKETS:
        subset = [t for t in trades_log if t["market"] == market]
        if not subset:
            print(f"  {MARKETS[market]['name']}: 0টা ট্রেড")
            continue
        wins = sum(1 for t in subset if t["ret"] > 0)
        total = sum(t["ret"] for t in subset)
        avg = total / len(subset)
        print(f"  {MARKETS[market]['name']}: {len(subset)}টা ট্রেড | win rate {wins}/{len(subset)} "
              f"({wins/len(subset)*100:.0f}%) | গড় রিটার্ন {avg:+.2f}% | মোট রিটার্ন {total:+.2f}%")

    # কম্বাইন্ড ইকুইটি কার্ভ ও ড্রডাউন (বন্ধ হওয়ার তারিখ অনুযায়ী)
    ordered = sorted(trades_log, key=lambda t: t["exit_time"])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_start = dd_end = None
    peak_time = ordered[0]["exit_time"] if ordered else None
    for t in ordered:
        equity += t["ret"]
        if equity > peak:
            peak = equity
            peak_time = t["exit_time"]
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            dd_start, dd_end = peak_time, t["exit_time"]

    print(f"\n  কম্বাইন্ড মোট রিটার্ন: {final_equity:+.2f}%")
    print(f"  কম্বাইন্ড সর্বোচ্চ ড্রডাউন: -{max_dd:.2f}%"
          + (f" ({dd_start} থেকে {dd_end})" if dd_start else ""))
    print(f"  Cooldown চালু হয়েছে: {cooldown_activations} বার")
    print(f"  স্কিপ হওয়া এন্ট্রি — cooldown: {skip_cd} | correlation filter: {skip_corr} | max positions: {skip_max}")


def main():
    all_data = {key: load_market_data(key, cfg) for key, cfg in MARKETS.items()}
    trades_log, final_equity, cd_act, skip_cd, skip_corr, skip_max = run_portfolio_backtest(all_data)
    print_report(trades_log, final_equity, cd_act, skip_cd, skip_corr, skip_max)


if __name__ == "__main__":
    main()
          
