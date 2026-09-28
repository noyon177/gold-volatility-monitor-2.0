"""EMA9/EMA15 ক্রসওভার + স্ট্রং ক্যান্ডেল — ১৫ মিনিট ব্যাকটেস্ট (সংশোধিত সংস্করণ)

কৌশল:
  - EMA9 উপরে ক্রস (বুলিশ) → CROSS_WINDOW ক্যান্ডেলের মধ্যে বুলিশ Marubozu বা Hammer এলে BUY
  - EMA9 নিচে ক্রস (বেয়ারিশ) → CROSS_WINDOW ক্যান্ডেলের মধ্যে বেয়ারিশ Marubozu বা
    Shooting Star (BEAR_WICK_PATTERN দিয়ে HangingMan-ও বেছে নেওয়া যায়) এলে SELL
  - অ্যাঙ্গেল ফিল্টার: EMA9-এর ঢাল ATR-এর সাপেক্ষে মাপা হয় (স্কেল স্থির, ১০০ ক্যান্ডেলের রেঞ্জের ওপর নির্ভর করে না)
  - SR ফিল্টার: শুধু অভাঙা (unbroken) কনফার্মড সুইং লেভেল
  - SL = সিগন্যাল ক্যান্ডেলের low/high, TP = ঝুঁকির RR গুণ
  - এন্ট্রি = পরের ক্যান্ডেলের ওপেন; একই ক্যান্ডেলে SL ও TP দুটো ছুঁলে SL আগে
  - গ্যাপে SL পার হলে ওপেন প্রাইসে বের হয় (খারাপ ফিল ধরা হয়)
  - খরচ % ও R দুটোতেই ধরা হয়; equity/drawdown R-ভিত্তিক (প্রতি ট্রেডে সমান ঝুঁকি)
  - শেষের চলমান (অসম্পূর্ণ) ক্যান্ডেল বাদ
  - ইন-স্যাম্পল / আউট-অব-স্যাম্পল আলাদা রিপোর্ট

চালানো:
  TWELVE_DATA_API_KEY=xxxx python backtest_ema_candle_15m_fixed.py
"""
import datetime as dt
import math
import os
import time

import requests

TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]

SYMBOL = "BTC/USD"
INTERVAL = "15min"
TOTAL_CANDLES = 15000
CHUNK = 5000

EMA_FAST = 9
EMA_SLOW = 15
CROSS_WINDOW = 10
ONE_TRADE_PER_CROSS = True

# অ্যাঙ্গেল ফিল্টার (ATR-ভিত্তিক স্কেল)
USE_ANGLE_FILTER = True
ANGLE_MIN = 60
ANGLE_MAX = 90                 # ৭৫°-এর ওপরের শক্তিশালী মুভও ধরা হবে
ANGLE_LOOKBACK = 3
ATR_PERIOD = 14
ANGLE_SCALE = 4.0              # বড় করলে একই ঢালে ডিগ্রি বাড়ে। ডায়াগনস্টিক দেখে ঠিক করুন
                               # (tan(অ্যাঙ্গেল) = ATR-এককে প্রতি ক্যান্ডেল ঢাল × ANGLE_SCALE)

# সাপোর্ট / রেজিস্ট্যান্স
USE_SR_FILTER = True
SR_SWING_LOOKBACK = 5
SR_LOOKBACK_CANDLES = 500
SR_TOUCH_TOL_PCT = 0.30
SR_BLOCK_TP = False

# ক্যান্ডেল প্যাটার্ন
MARUBOZU_BODY_RATIO = 0.90
WICK_LONG_X = 2.0              # লম্বা উইক >= বডির ২ গুণ
WICK_SHORT_MAX = 0.3           # উল্টো দিকের উইক <= বডির ৩০%
MIN_BODY_RATIO = 0.10
BEAR_WICK_PATTERN = "ShootingStar"   # "ShootingStar" (যুক্তিসঙ্গত) বা "HangingMan" (আগের আচরণ)

RR = 2.0
MIN_RISK_PCT = 0.05
MAX_HOLD_CANDLES = None
ROUND_TRIP_COST_PCT = 0.05

IS_FRACTION = 0.7              # প্রথম ৭০% ইন-স্যাম্পল, বাকি ৩০% আউট-অব-স্যাম্পল

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
    """(time, open, high, low, close), পুরোনো → নতুন। শেষের চলমান ক্যান্ডেল বাদ।"""
    data_map = {}
    end_date = None
    while len(data_map) < total:
        params = {"symbol": symbol, "interval": interval, "outputsize": chunk,
                  "timezone": "UTC", "apikey": TWELVE_DATA_KEY}
        if end_date:
            params["end_date"] = end_date
        try:
            data = get_json("https://api.twelvedata.com/time_series", params)
        except RuntimeError:
            if data_map:
                print("⚠ ডেটা আনতে সমস্যা, এ পর্যন্ত যা এসেছে তা নিয়ে চলছে")
                break
            raise
        if data.get("status") == "error":
            if data_map:  # পুরোনো ডেটা ফুরিয়ে গেলে এরর আসে
                print("ℹ আর পুরোনো ডেটা নেই:", str(data.get("message", ""))[:100])
                break
            raise RuntimeError("Twelve Data: " + str(data.get("message", "error"))[:150])
        rows = data.get("values", [])
        if not rows:
            break
        before = len(data_map)
        for r in rows:
            data_map[r["datetime"]] = r
        if len(data_map) == before:
            break
        end_date = min(r["datetime"] for r in rows)
        time.sleep(8)
    out = []
    for k in sorted(data_map):
        r = data_map[k]
        t = dt.datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        out.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    out = out[:-1]  # শেষ ক্যান্ডেল এখনো চলমান থাকতে পারে
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


def atr_series(c, period=ATR_PERIOD):
    """Wilder ATR।"""
    n = len(c)
    out = [None] * n
    if n <= period:
        return out
    tr = [None] * n
    for i in range(1, n):
        h, lo, pc = c[i][2], c[i][3], c[i - 1][4]
        tr[i] = max(h - lo, abs(h - pc), abs(lo - pc))
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def find_raw_swings(c, lookback=SR_SWING_LOOKBACK):
    n = len(c)
    raw_high = [None] * n
    raw_low = [None] * n
    for i in range(lookback, n - lookback):
        if c[i][2] == max(c[j][2] for j in range(i - lookback, i + lookback + 1)):
            raw_high[i] = c[i][2]
        if c[i][3] == min(c[j][3] for j in range(i - lookback, i + lookback + 1)):
            raw_low[i] = c[i][3]
    return raw_high, raw_low


def sr_levels(raw_high, raw_low, closes, i):
    """ক্যান্ডেল i-তে জানা কনফার্মড এবং অভাঙা লেভেল।
    ভাঙা = সুইং তৈরির পর কোনো ক্যান্ডেল টলারেন্সসহ লেভেলের ওপারে ক্লোজ করেছে।"""
    tol = SR_TOUCH_TOL_PCT / 100
    start = max(0, i - SR_LOOKBACK_CANDLES)
    end = i - SR_SWING_LOOKBACK
    res, sup = [], []
    for k in range(start, end + 1):
        after = closes[k + 1:i]
        lv = raw_high[k]
        if lv is not None and not (after and max(after) > lv * (1 + tol)):
            res.append(lv)
        lv = raw_low[k]
        if lv is not None and not (after and min(after) < lv * (1 - tol)):
            sup.append(lv)
    return res, sup


def touches_support(candle, sup):
    _, _, _, lo, cl = candle
    tol = SR_TOUCH_TOL_PCT / 100
    return any(lo <= s * (1 + tol) and cl > s * (1 - tol) and lo >= s * (1 - 3 * tol) for s in sup)


def touches_resistance(candle, res):
    _, _, hi, _, cl = candle
    tol = SR_TOUCH_TOL_PCT / 100
    return any(hi >= r * (1 - tol) and cl < r * (1 + tol) and hi <= r * (1 + 3 * tol) for r in res)


def ema_angle(ema, atr, i):
    """EMA-র ঢাল ডিগ্রিতে। ঢাল ATR-এককে মাপা, তাই স্কেল সব সময় একই। উঠলে +, নামলে −।"""
    if i < ANGLE_LOOKBACK or ema[i] is None or ema[i - ANGLE_LOOKBACK] is None:
        return None
    if atr[i] is None or atr[i] <= 0:
        return None
    slope = (ema[i] - ema[i - ANGLE_LOOKBACK]) / ANGLE_LOOKBACK / atr[i]
    return math.degrees(math.atan(slope * ANGLE_SCALE))


def candle_patterns(c):
    """[(নাম, দিক), ...]। 'bull' = BUY-সমর্থক, 'bear' = SELL-সমর্থক।"""
    _, o, h, low, cl = c
    rng = h - low
    if rng <= 0:
        return []
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - low
    found = []

    if body / rng >= MARUBOZU_BODY_RATIO:
        found.append(("Marubozu", "bull" if cl > o else "bear"))

    if body / rng >= MIN_BODY_RATIO:
        # লম্বা নিচের উইক: নিচে প্রত্যাখ্যান → Hammer (BUY)
        if lower >= WICK_LONG_X * body and upper <= WICK_SHORT_MAX * body:
            found.append(("Hammer", "bull"))
            if BEAR_WICK_PATTERN == "HangingMan":
                found.append(("HangingMan", "bear"))
        # লম্বা ওপরের উইক: ওপরে প্রত্যাখ্যান → Shooting Star (SELL)
        if BEAR_WICK_PATTERN == "ShootingStar" and \
                upper >= WICK_LONG_X * body and lower <= WICK_SHORT_MAX * body:
            found.append(("ShootingStar", "bear"))
    return found


def simulate(c, entry_idx, direction, entry, sl, tp):
    end = len(c) - 1 if MAX_HOLD_CANDLES is None else min(entry_idx + MAX_HOLD_CANDLES, len(c) - 1)
    for j in range(entry_idx, end + 1):
        _, o, h, low, cl = c[j]
        if direction == "bull":
            if low <= sl:
                return "SL", min(sl, o), j - entry_idx   # গ্যাপ হলে ওপেনে
            if h >= tp:
                return "TP", tp, j - entry_idx
        else:
            if h >= sl:
                return "SL", max(sl, o), j - entry_idx
            if low <= tp:
                return "TP", tp, j - entry_idx
    if MAX_HOLD_CANDLES is None:
        return "OPEN", c[end][4], end - entry_idx
    return "TIMEOUT", c[end][4], end - entry_idx


def report(title, trades):
    if not trades:
        print(f"{title}: 0টা ট্রেড")
        return
    wins = [t for t in trades if t["r"] > 0]
    gw = sum(t["r"] for t in trades if t["r"] > 0)
    gl = -sum(t["r"] for t in trades if t["r"] < 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"{title}: {len(trades)}টা | win {len(wins)} ({len(wins)/len(trades)*100:.0f}%) | "
          f"মোট {sum(t['r'] for t in trades):+.1f}R | গড় {sum(t['r'] for t in trades)/len(trades):+.3f}R | "
          f"PF {pf:.2f} | মোট (১ ইউনিট পুঁজি) {sum(t['ret'] for t in trades):+.1f}%")


def max_drawdown_r(trades):
    equity = peak = dd = 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def main():
    print(f"{SYMBOL} {INTERVAL} ডেটা আনা হচ্ছে...")
    c = fetch_candles(SYMBOL, INTERVAL, TOTAL_CANDLES)
    print(f"{len(c)}টা ক্যান্ডেল | {c[0][0]:%Y-%m-%d} থেকে {c[-1][0]:%Y-%m-%d}")

    closes = [x[4] for x in c]
    ef = ema_series(closes, EMA_FAST)
    es = ema_series(closes, EMA_SLOW)
    atr = atr_series(c)
    raw_high, raw_low = find_raw_swings(c, SR_SWING_LOOKBACK)
    warmup = max(EMA_SLOW, ATR_PERIOD) + 5

    trades = []
    last_cross_idx = None
    last_cross_dir = None
    used_cross = None
    next_free = 0
    angles_seen = []
    skipped_angle = 0
    skipped_sr = 0

    for i in range(warmup, len(c) - 1):
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

        pats = [name for name, d in candle_patterns(c[i]) if d == last_cross_dir]
        if not pats:
            continue

        ang = ema_angle(ef, atr, i)
        if ang is not None:
            angles_seen.append(ang if last_cross_dir == "bull" else -ang)
        if USE_ANGLE_FILTER:
            if ang is None:
                continue
            signed = ang if last_cross_dir == "bull" else -ang
            if not (ANGLE_MIN <= signed <= ANGLE_MAX):
                skipped_angle += 1
                continue

        direction = last_cross_dir

        res_levels, sup_levels = sr_levels(raw_high, raw_low, closes, i)
        if USE_SR_FILTER:
            ok = touches_support(c[i], sup_levels) if direction == "bull" \
                else touches_resistance(c[i], res_levels)
            if not ok:
                skipped_sr += 1
                continue

        entry_idx = i + 1
        entry = c[entry_idx][1]
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

        if USE_SR_FILTER and SR_BLOCK_TP:
            if direction == "bull" and any(entry < lv < tp for lv in res_levels):
                skipped_sr += 1
                continue
            if direction == "bear" and any(tp < lv < entry for lv in sup_levels):
                skipped_sr += 1
                continue

        outcome, exit_price, bars = simulate(c, entry_idx, direction, entry, sl, tp)
        if outcome == "OPEN":
            print(f"\nℹ শেষ ট্রেড ({c[entry_idx][0]:%Y-%m-%d %H:%M}, {direction}) ডেটার শেষেও SL/TP লাগেনি, হিসাবের বাইরে")
            break

        raw = (exit_price - entry) / entry * 100
        ret = (raw if direction == "bull" else -raw) - ROUND_TRIP_COST_PCT
        risk_pct = risk / entry * 100
        gross_r = ((exit_price - entry) if direction == "bull" else (entry - exit_price)) / risk
        r_mult = gross_r - ROUND_TRIP_COST_PCT / risk_pct   # খরচসহ নিট R

        trades.append({"time": c[entry_idx][0], "direction": direction, "pattern": "+".join(pats),
                       "entry": entry, "sl": sl, "tp": tp, "outcome": outcome,
                       "ret": ret, "r": r_mult, "bars": bars})
        used_cross = last_cross_idx
        next_free = entry_idx + bars + 1

    print(f"\n{'=' * 60}")
    for t in trades:
        arrow = "🟢 বাই" if t["direction"] == "bull" else "🔴 সেল"
        print(f"{t['time']:%Y-%m-%d %H:%M} {arrow} [{t['pattern']}] @ {t['entry']:,.2f} "
              f"SL {t['sl']:,.2f} TP {t['tp']:,.2f} → {t['outcome']} {t['ret']:+.2f}% ({t['r']:+.2f}R)")

    print(f"\n{'=' * 60}\nসারসংক্ষেপ (R = খরচ-পরবর্তী নিট)")
    if angles_seen:
        s = sorted(angles_seen)
        pick = lambda p: s[min(len(s) - 1, int(len(s) * p))]  # noqa: E731
        print(f"অ্যাঙ্গেল ডায়াগনস্টিক ({len(s)}টা প্যাটার্ন-সিগন্যাল): "
              f"মিন {s[0]:.0f}° | ২৫% {pick(0.25):.0f}° | মিডিয়ান {pick(0.5):.0f}° | "
              f"৭৫% {pick(0.75):.0f}° | ৯০% {pick(0.9):.0f}° | ম্যাক্স {s[-1]:.0f}°")
        in_range = sum(1 for a in s if ANGLE_MIN <= a <= ANGLE_MAX)
        print(f"  {ANGLE_MIN}–{ANGLE_MAX}° রেঞ্জে {in_range}টা | অ্যাঙ্গেলের কারণে বাদ {skipped_angle}টা | SR-এর কারণে বাদ {skipped_sr}টা")
        if in_range < 30:
            print("  ⚠ সিগন্যাল কম — ANGLE_SCALE/ANGLE_LOOKBACK বদলে স্কেল মিলিয়ে নিন")

    report("মোট", trades)
    for p in ("Marubozu", "Hammer", "ShootingStar", "HangingMan"):
        sub = [t for t in trades if p in t["pattern"].split("+")]
        if sub:
            report(f"  {p}", sub)
    report("  শুধু BUY", [t for t in trades if t["direction"] == "bull"])
    report("  শুধু SELL", [t for t in trades if t["direction"] == "bear"])

    split_time = c[int(len(c) * IS_FRACTION)][0]
    print(f"\nইন-স্যাম্পল / আউট-অব-স্যাম্পল (ভাগ: {split_time:%Y-%m-%d})")
    report("  ইন-স্যাম্পল", [t for t in trades if t["time"] < split_time])
    report("  আউট-অব-স্যাম্পল", [t for t in trades if t["time"] >= split_time])

    print(f"\nসর্বোচ্চ ড্রডাউন: -{max_drawdown_r(trades):.1f}R | ব্রেকইভেন win rate (1:{RR:g}, খরচ ছাড়া): {100/(1+RR):.0f}%")


if __name__ == "__main__":
    main()
