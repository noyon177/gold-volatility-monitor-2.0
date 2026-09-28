"""EMA9/EMA15 ক্রসওভার + স্ট্রং ক্যান্ডেল (Marubozu / Hammer / Hanging Man) — ১৫ মিনিট ব্যাকটেস্ট

কৌশল:
  - EMA9 উপরে ক্রস করলে (বুলিশ ক্রস) → এর পর CROSS_WINDOW ক্যান্ডেলের মধ্যে
    বুলিশ Marubozu বা Hammer এলে BUY
  - EMA9 নিচে ক্রস করলে (বেয়ারিশ ক্রস) → এর পর CROSS_WINDOW ক্যান্ডেলের মধ্যে
    বেয়ারিশ Marubozu বা Hanging Man এলে SELL
  - SL = সিগন্যাল ক্যান্ডেলের low (BUY) / high (SELL)
  - TP = ঝুঁকির ২ গুণ (1:2)
  - এন্ট্রি = সিগন্যাল ক্যান্ডেলের পরের ক্যান্ডেলের ওপেনে (বাস্তবসম্মত)
  - একই ক্যান্ডেলে SL ও TP দুটোই ছুঁলে SL আগে ধরা হয় (রক্ষণশীল)

চালানোর নিয়ম:
  TWELVE_DATA_API_KEY=xxxx python backtest_ema_candle_15m.py
"""
import datetime as dt
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

SYMBOL = "BTC/USD"
INTERVAL = "15min"
TOTAL_CANDLES = 15000          # মোট কত ক্যান্ডেল আনবে (১৫০০০ ≈ ১৫৬ দিন)
CHUNK = 5000                   # প্রতি রিকোয়েস্টে সর্বোচ্চ

EMA_FAST = 9
EMA_SLOW = 15
CROSS_WINDOW = 10              # ক্রসের পর কত ক্যান্ডেলের মধ্যে সিগন্যাল বৈধ
ONE_TRADE_PER_CROSS = True     # প্রতিটা ক্রসে সর্বোচ্চ একটা ট্রেড

# ক্যান্ডেল প্যাটার্নের সংজ্ঞা
MARUBOZU_BODY_RATIO = 0.90     # বডি >= পুরো রেঞ্জের ৯০%
HAMMER_LOWER_WICK_X = 2.0      # নিচের উইক >= বডির ২ গুণ
HAMMER_UPPER_WICK_MAX = 0.3    # উপরের উইক <= বডির ৩০% (Hanging Man-এ উল্টো দিক)
MIN_BODY_RATIO = 0.10          # বডি রেঞ্জের অন্তত ১০% (ডোজি বাদ)

RR = 2.0                       # রিওয়ার্ড : রিস্ক
MIN_RISK_PCT = 0.05            # SL খুব কাছে হলে (এন্ট্রির % হিসেবে) ট্রেড বাদ
MAX_HOLD_CANDLES = 96          # ২৪ ঘণ্টা
ROUND_TRIP_COST_PCT = 0.05     # স্প্রেড+ফি (এন্ট্রির %)

HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_json(url, params, tries=3):
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(str(err))


def fetch_candles(symbol, interval, total, chunk=CHUNK):
    """(time, open, high, low, close) লিস্ট, পুরোনো থেকে নতুন। end_date দিয়ে পেছনে পেজিনেশন।"""
    data_map = {}
    end_date = None
    while len(data_map) < total:
        params = {"symbol": symbol, "interval": interval, "outputsize": chunk,
                  "timezone": "UTC", "apikey": TWELVE_DATA_KEY}
        if end_date:
            params["end_date"] = end_date
        data = get_json("https://api.twelvedata.com/time_series", params)
        if data.get("status") == "error":
            raise RuntimeError("Twelve Data: " + str(data.get("message", "error"))[:150])
        rows = data.get("values", [])
        if not rows:
            break
        before = len(data_map)
        for r in rows:
            data_map[r["datetime"]] = r
        if len(data_map) == before:
            break
        earliest = min(r["datetime"] for r in rows)
        end_date = earliest
        time.sleep(8)  # ফ্রি প্ল্যানের রেট লিমিট এড়াতে
    out = []
    for k in sorted(data_map):
        r = data_map[k]
        t = dt.datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        out.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return out[-total:]


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


def candle_patterns(c):
    """একটা ক্যান্ডেল থেকে প্যাটার্ন লিস্ট রিটার্ন করে: [(নাম, দিক), ...]
    দিক: 'bull' মানে বাই-সাপোর্ট, 'bear' মানে সেল-সাপোর্ট।"""
    _, o, h, l, cl = c
    rng = h - l
    if rng <= 0:
        return []
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    found = []

    # Marubozu: বডি প্রায় পুরো ক্যান্ডেল
    if body / rng >= MARUBOZU_BODY_RATIO:
        found.append(("Marubozu", "bull" if cl > o else "bear"))

    # Hammer / Hanging Man: একই আকৃতি (ছোট বডি উপরে, লম্বা নিচের উইক)
    if body / rng >= MIN_BODY_RATIO and lower >= HAMMER_LOWER_WICK_X * body \
            and upper <= HAMMER_UPPER_WICK_MAX * body:
        # বুলিশ ক্রসের পর Hammer → বাই; বেয়ারিশ ক্রসের পর Hanging Man → সেল
        found.append(("Hammer", "bull"))
        found.append(("HangingMan", "bear"))
    return found


def simulate(c, entry_idx, direction, entry, sl, tp):
    """entry_idx ক্যান্ডেলের ওপেনে এন্ট্রি, ওই ক্যান্ডেল থেকেই চেক শুরু।"""
    end = min(entry_idx + MAX_HOLD_CANDLES, len(c) - 1)
    for j in range(entry_idx, end + 1):
        _, o, h, l, cl = c[j]
        if direction == "bull":
            if l <= sl:
                return "SL", min(sl, o) if j == entry_idx else sl, j - entry_idx
            if h >= tp:
                return "TP", tp, j - entry_idx
        else:
            if h >= sl:
                return "SL", max(sl, o) if j == entry_idx else sl, j - entry_idx
            if l <= tp:
                return "TP", tp, j - entry_idx
    return "TIMEOUT", c[end][4], end - entry_idx


def report(title, trades):
    if not trades:
        print(f"{title}: 0টা ট্রেড")
        return
    wins = [t for t in trades if t["ret"] > 0]
    gross_win = sum(t["ret"] for t in trades if t["ret"] > 0)
    gross_loss = -sum(t["ret"] for t in trades if t["ret"] < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    print(f"{title}: {len(trades)}টা | win {len(wins)} ({len(wins)/len(trades)*100:.0f}%) | "
          f"মোট {sum(t['ret'] for t in trades):+.2f}% | গড় {sum(t['ret'] for t in trades)/len(trades):+.3f}% | "
          f"গড় R {sum(t['r'] for t in trades)/len(trades):+.2f} | PF {pf:.2f}")


def main():
    print(f"{SYMBOL} {INTERVAL} ডেটা আনা হচ্ছে...")
    c = fetch_candles(SYMBOL, INTERVAL, TOTAL_CANDLES)
    print(f"{len(c)}টা ক্যান্ডেল | {c[0][0]:%Y-%m-%d} থেকে {c[-1][0]:%Y-%m-%d}")

    closes = [x[4] for x in c]
    ef = ema_series(closes, EMA_FAST)
    es = ema_series(closes, EMA_SLOW)

    trades = []
    last_cross_idx = None
    last_cross_dir = None
    used_cross = None
    next_free = 0
    warmup = EMA_SLOW + 5

    for i in range(warmup, len(c) - 1):
        # ক্রস ডিটেকশন
        if ef[i] is not None and es[i] is not None and ef[i - 1] is not None and es[i - 1] is not None:
            if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
                last_cross_idx, last_cross_dir = i, "bull"
            elif ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
                last_cross_idx, last_cross_dir = i, "bear"

        if last_cross_idx is None or i < next_free:
            continue
        if i - last_cross_idx > CROSS_WINDOW:
            continue
        if ONE_TRADE_PER_CROSS and used_cross == last_cross_idx:
            continue

        # সিগন্যাল ক্যান্ডেল: ক্রসের দিকের সাথে মেলে এমন প্যাটার্ন
        pats = [name for name, d in candle_patterns(c[i]) if d == last_cross_dir]
        if not pats:
            continue

        direction = last_cross_dir
        entry_idx = i + 1
        entry = c[entry_idx][1]  # পরের ক্যান্ডেলের ওপেন
        _, _, hi, lo, _ = c[i]
        if direction == "bull":
            sl = lo
            risk = entry - sl
            tp = entry + RR * risk
        else:
            sl = hi
            risk = sl - entry
            tp = entry - RR * risk
        if risk <= 0 or risk / entry * 100 < MIN_RISK_PCT:
            continue

        outcome, exit_price, bars = simulate(c, entry_idx, direction, entry, sl, tp)
        raw = (exit_price - entry) / entry * 100
        ret = (raw if direction == "bull" else -raw) - ROUND_TRIP_COST_PCT
        r_mult = ((exit_price - entry) if direction == "bull" else (entry - exit_price)) / risk

        trades.append({"time": c[entry_idx][0], "direction": direction, "pattern": pats[0],
                       "entry": entry, "sl": sl, "tp": tp, "outcome": outcome,
                       "ret": ret, "r": r_mult, "bars": bars})
        used_cross = last_cross_idx
        next_free = entry_idx + bars + 1

    print(f"\n{'=' * 60}")
    for t in trades:
        arrow = "🟢 বাই" if t["direction"] == "bull" else "🔴 সেল"
        print(f"{t['time']:%Y-%m-%d %H:%M} {arrow} [{t['pattern']}] @ {t['entry']:,.2f} "
              f"SL {t['sl']:,.2f} TP {t['tp']:,.2f} → {t['outcome']} {t['ret']:+.2f}% ({t['r']:+.2f}R)")

    print(f"\n{'=' * 60}\nসারসংক্ষেপ")
    report("মোট", trades)
    for p in ("Marubozu", "Hammer", "HangingMan"):
        report(f"  {p}", [t for t in trades if t["pattern"] == p])
    report("  শুধু BUY", [t for t in trades if t["direction"] == "bull"])
    report("  শুধু SELL", [t for t in trades if t["direction"] == "bear"])

    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t["ret"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(f"\nসর্বোচ্চ ড্রডাউন: -{max_dd:.2f}% | ব্রেকইভেন win rate (1:{RR:g}): {100/(1+RR):.0f}%")


if __name__ == "__main__":
    main()
