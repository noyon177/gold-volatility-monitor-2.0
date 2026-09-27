"""দৈনিক সিগন্যাল-বট — BTC/USD, EMA9/15 পুলব্যাক + সুইং SL/TP (backtest_swing_v4.py-এর
লজিকের সাথে হুবহু মিলিয়ে) — এটা কোনো অর্ডার বসায় না, শুধু সিগন্যাল/এক্সিট শনাক্ত করে
GitHub Actions-এর লগে প্রিন্ট করে। ট্রেড বাস্তবে আপনি নিজে করবেন।

কীভাবে কাজ করে:
  - প্রতিদিন একবার চলে (GitHub Actions cron)
  - সর্বশেষ ক্লোজড দৈনিক ক্যান্ডেল আনে, EMA/RSI/সুইং হিসাব করে
  - state.json ফাইলে ওপেন পজিশন (যদি থাকে) মনে রাখে — একবারে সর্বোচ্চ ১টা পজিশন
  - ওপেন পজিশন থাকলে: আজকের ক্যান্ডেলে SL/TP হিট হয়েছে কিনা, বা MAX_HOLD_CANDLES পার
    হয়ে গেছে কিনা চেক করে; হলে এক্সিট মেসেজ প্রিন্ট করে state ক্লিয়ার করে
  - কোনো পজিশন না থাকলে: নতুন এন্ট্রি সিগন্যাল আছে কিনা চেক করে; থাকলে এন্ট্রি মেসেজ
    প্রিন্ট করে state-এ সেভ করে
  - একই ক্যান্ডেল একাধিকবার প্রসেস না হয় তার জন্য state.json-এ শেষ প্রসেসড তারিখ
    রাখা হয় (idempotent — একই দিনে বারবার চালালেও দ্বিতীয়বার কিছু হবে না)

চালানোর নিয়ম: TWELVE_DATA_API_KEY=xxxx python bot.py
"""
import datetime as dt
import json
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]
STATE_FILE = "state.json"

SYMBOL = "BTC/USD"
MARKET_NAME = "বিটকয়েন (BTC/USD)"
ROUND_TRIP_COST_PCT = 0.05

CANDLE_INTERVAL = "1day"
OUTPUT_SIZE = 300   # লাইভ সিগন্যালের জন্য এতটুকু ইতিহাসই যথেষ্ট (backtest_swing_v4.py-এ পুরো ইতিহাস লাগে)

EMA_FAST = 9
EMA_SLOW = 15
TREND_SLOPE_LOOKBACK = 10
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
SWING_LOOKBACK = 5
PULLBACK_BODY_CONFIRM = True
MIN_REWARD_RISK_RATIO = 1.0
RANGE_SL_BUFFER_PCT = 0.3
MAX_HOLD_CANDLES = 40

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
        {"symbol": symbol, "interval": interval, "outputsize": outputsize,
         "timezone": "UTC", "apikey": TWELVE_DATA_KEY},
        tries=2,
    )
    if data.get("status") == "error":
        raise RuntimeError("Twelve Data: " + str(data.get("message", "error"))[:150])
    rows = sorted(data["values"], key=lambda r: r["datetime"])
    out = []
    for r in rows:
        fmt = "%Y-%m-%d %H:%M:%S" if len(r["datetime"]) > 10 else "%Y-%m-%d"
        t = dt.datetime.strptime(r["datetime"], fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        out.append((t, float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r.get("volume") or 0)))
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
    last_high = last_low = None
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
    ef, es, ef_prev = ema_fast[idx], ema_slow[idx], ema_fast[idx - slope_lookback]
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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"position": None, "last_processed_date": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def trade_return_pct(direction, entry, exit_price):
    raw = (exit_price - entry) / entry * 100
    return raw if direction == "bull" else -raw


def main():
    c4 = fetch_candles(SYMBOL, CANDLE_INTERVAL, OUTPUT_SIZE)
    closes = [c[4] for c in c4]
    ema_f = ema_series(closes, EMA_FAST)
    ema_s = ema_series(closes, EMA_SLOW)
    rsi = calc_rsi(closes, RSI_PERIOD)
    swing_high, swing_low = find_confirmed_swings(c4, SWING_LOOKBACK)

    idx = len(c4) - 1
    today = dt.datetime.fromtimestamp(c4[idx][0], tz=dt.timezone.utc).strftime("%Y-%m-%d")

    state = load_state()
    if state.get("last_processed_date") == today:
        print(f"{today}-এর ক্যান্ডেল ইতিমধ্যে প্রসেস করা হয়েছে, আবার করা হচ্ছে না।")
        return

    print(f"{MARKET_NAME} — {today}-এর ক্যান্ডেল প্রসেস করা হচ্ছে (close: {c4[idx][4]:,.2f})")

    pos = state.get("position")
    if pos:
        hi, lo, cl = c4[idx][2], c4[idx][3], c4[idx][4]
        direction, sl, tp, entry = pos["direction"], pos["sl"], pos["tp"], pos["entry"]
        bars = pos["bars"] + 1
        hit_sl = lo <= sl if direction == "bull" else hi >= sl
        hit_tp = hi >= tp if direction == "bull" else lo <= tp
        timed_out = bars >= MAX_HOLD_CANDLES

        if hit_sl or hit_tp or timed_out:
            if hit_sl:
                exit_price, outcome = sl, "SL"
            elif hit_tp:
                exit_price, outcome = tp, "TP"
            else:
                exit_price, outcome = cl, "TIMEOUT"
            ret = trade_return_pct(direction, entry, exit_price) - ROUND_TRIP_COST_PCT
            arrow = "🟢 বাই" if direction == "bull" else "🔴 সেল"
            print(f"\n🔔 এক্সিট সিগন্যাল — {arrow} পজিশন বন্ধ করুন")
            print(f"   এন্ট্রি: {entry:,.2f} ({pos['entry_date']}) | এক্সিট: {exit_price:,.2f} ({outcome})")
            print(f"   রিটার্ন: {ret:+.2f}% ({bars} দিন পরে)")
            state["position"] = None
        else:
            pos["bars"] = bars
            state["position"] = pos
            print(f"   ওপেন পজিশন চলছে ({direction}, এন্ট্রি {entry:,.2f}, SL {sl:,.2f}, TP {tp:,.2f}, "
                  f"{bars} দিন হলো)। কোনো নতুন সিগন্যাল না।")
    else:
        regime = trend_regime(ema_f, ema_s, idx)
        direction = mode = sl = tp = None
        if regime in ("up", "down"):
            direction = trend_pullback_signal(c4, ema_f, regime, idx)
            if direction:
                mode = "ট্রেন্ড-পুলব্যাক"
                sl, tp = (swing_low[idx], swing_high[idx]) if direction == "bull" else (swing_high[idx], swing_low[idx])
        elif regime == "range":
            direction = range_rsi_signal(c4, rsi, idx)
            if direction:
                mode = "রেঞ্জ-RSI"
                if direction == "bull":
                    sl = swing_low[idx] * (1 - RANGE_SL_BUFFER_PCT / 100) if swing_low[idx] else None
                    tp = swing_high[idx]
                else:
                    sl = swing_high[idx] * (1 + RANGE_SL_BUFFER_PCT / 100) if swing_high[idx] else None
                    tp = swing_low[idx]

        entry = c4[idx][4]
        valid = False
        if direction and sl is not None and tp is not None:
            if direction == "bull" and sl < entry < tp:
                risk, reward = entry - sl, tp - entry
                valid = risk > 0 and reward / risk >= MIN_REWARD_RISK_RATIO
            elif direction == "bear" and tp < entry < sl:
                risk, reward = sl - entry, entry - tp
                valid = risk > 0 and reward / risk >= MIN_REWARD_RISK_RATIO

        if valid:
            arrow = "🟢 বাই" if direction == "bull" else "🔴 সেল"
            print(f"\n🔔 নতুন এন্ট্রি সিগন্যাল — {arrow} [{mode}]")
            print(f"   এন্ট্রি (আজকের ক্লোজ): {entry:,.2f}")
            print(f"   SL: {sl:,.2f} | TP: {tp:,.2f} | R:R = {reward/risk:.2f}")
            state["position"] = {
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "entry_date": today, "bars": 0, "mode": mode,
            }
        else:
            print("   কোনো এন্ট্রি সিগন্যাল নেই আজ।")

    state["last_processed_date"] = today
    save_state(state)


if __name__ == "__main__":
    main()
