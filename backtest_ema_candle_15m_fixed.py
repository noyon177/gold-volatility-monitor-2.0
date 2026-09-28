"""EMA9/EMA15 crossover + strong candle - 15 minute backtest (fixed version)

Strategy:
  - Bullish cross (EMA9 crosses above EMA15): within CROSS_WINDOW candles, a bullish
    Marubozu or Hammer gives a BUY
  - Bearish cross: within CROSS_WINDOW candles, a bearish Marubozu or Shooting Star
    (HangingMan selectable via BEAR_WICK_PATTERN) gives a SELL
  - Angle filter: EMA9 slope is measured in ATR units, so the scale is fixed
    (it no longer depends on the last 100 candles' range)
  - S/R filter: only confirmed swing levels that have not been broken
  - SL = signal candle low/high, TP = RR x risk
  - Entry = open of the candle after the signal; if SL and TP are both hit in the
    same candle, SL is assumed first
  - If price gaps through SL, the exit is at the open price (bad fill is counted)
  - Costs are applied in both % and R; equity/drawdown are R-based (equal risk per trade)
  - The last (still forming) candle is dropped
  - In-sample / out-of-sample reported separately

Run:
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
TOTAL_CANDLES = 15000          # about 156 days of 15m candles
CHUNK = 5000                   # max per request

EMA_FAST = 9
EMA_SLOW = 15
CROSS_WINDOW = 10              # candles after a cross in which a signal is valid
ONE_TRADE_PER_CROSS = True

# Angle filter (ATR-based scale)
USE_ANGLE_FILTER = True
ANGLE_MIN = 60                 # degrees
ANGLE_MAX = 90                 # 90 = no upper cap, so the strongest moves are kept
ANGLE_LOOKBACK = 3             # candles over which the slope is measured
ATR_PERIOD = 14
ANGLE_SCALE = 4.0              # higher = larger degrees for the same slope
                               # tan(angle) = slope per candle in ATR units x ANGLE_SCALE

# Support / resistance
USE_SR_FILTER = True
SR_SWING_LOOKBACK = 5          # candles on each side to confirm a swing high/low
SR_LOOKBACK_CANDLES = 500      # how far back to look for levels
SR_TOUCH_TOL_PCT = 0.30        # +/- this % around a level counts as a touch
SR_BLOCK_TP = False            # True: skip trade if an opposing level sits between entry and TP

# Candle patterns
MARUBOZU_BODY_RATIO = 0.90     # body >= 90% of full range
WICK_LONG_X = 2.0              # long wick >= 2x body
WICK_SHORT_MAX = 0.3           # opposite wick <= 30% of body
MIN_BODY_RATIO = 0.10          # body at least 10% of range (skip dojis)
BEAR_WICK_PATTERN = "ShootingStar"   # "ShootingStar" (logical) or "HangingMan" (original behavior)

RR = 2.0                       # reward : risk
MIN_RISK_PCT = 0.05            # skip if SL is closer than this % of entry
MAX_HOLD_CANDLES = None        # None = no time limit, exit on SL or TP
ROUND_TRIP_COST_PCT = 0.05     # spread + fees (% of entry)

IS_FRACTION = 0.7              # first 70% in-sample, last 30% out-of-sample

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
    """(time, open, high, low, close), oldest -> newest. Drops the still-forming last candle."""
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
                print("WARNING: data fetch failed, continuing with what was received")
                break
            raise
        if data.get("status") == "error":
            if data_map:  # an error is returned when older data runs out
                print("INFO: no more older data:", str(data.get("message", ""))[:100])
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
        time.sleep(8)  # free plan rate limit
    out = []
    for k in sorted(data_map):
        r = data_map[k]
        t = dt.datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        out.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    out = out[:-1]  # the last candle may still be forming
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
    """Wilder ATR."""
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
    """Fractal swing highs/lows. Only confirmed points are used later
    (the right-side lookback candles have closed), so there is no future leak."""
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
    """Confirmed and unbroken levels known at candle i.
    Broken = after the swing formed, some candle closed beyond the level (with tolerance)."""
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
    """Bullish: candle low entered a support zone and the close is not below the zone."""
    _, _, _, lo, cl = candle
    tol = SR_TOUCH_TOL_PCT / 100
    return any(lo <= s * (1 + tol) and cl > s * (1 - tol) and lo >= s * (1 - 3 * tol) for s in sup)


def touches_resistance(candle, res):
    """Bearish: candle high entered a resistance zone and the close is not above the zone."""
    _, _, hi, _, cl = candle
    tol = SR_TOUCH_TOL_PCT / 100
    return any(hi >= r * (1 - tol) and cl < r * (1 + tol) and hi <= r * (1 + 3 * tol) for r in res)


def ema_angle(ema, atr, i):
    """EMA slope in degrees, measured in ATR units so the scale is always the same.
    Rising = +, falling = -."""
    if i < ANGLE_LOOKBACK or ema[i] is None or ema[i - ANGLE_LOOKBACK] is None:
        return None
    if atr[i] is None or atr[i] <= 0:
        return None
    slope = (ema[i] - ema[i - ANGLE_LOOKBACK]) / ANGLE_LOOKBACK / atr[i]
    return math.degrees(math.atan(slope * ANGLE_SCALE))


def candle_patterns(c):
    """Returns [(name, direction), ...]. 'bull' supports BUY, 'bear' supports SELL."""
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
        # Long lower wick: rejection of lower prices -> Hammer (BUY)
        if lower >= WICK_LONG_X * body and upper <= WICK_SHORT_MAX * body:
            found.append(("Hammer", "bull"))
            if BEAR_WICK_PATTERN == "HangingMan":
                found.append(("HangingMan", "bear"))
        # Long upper wick: rejection of higher prices -> Shooting Star (SELL)
        if BEAR_WICK_PATTERN == "ShootingStar" and \
                upper >= WICK_LONG_X * body and lower <= WICK_SHORT_MAX * body:
            found.append(("ShootingStar", "bear"))
    return found


def simulate(c, entry_idx, direction, entry, sl, tp):
    """Entry at the open of entry_idx; checking starts from that same candle."""
    end = len(c) - 1 if MAX_HOLD_CANDLES is None else min(entry_idx + MAX_HOLD_CANDLES, len(c) - 1)
    for j in range(entry_idx, end + 1):
        _, o, h, low, cl = c[j]
        if direction == "bull":
            if low <= sl:
                return "SL", min(sl, o), j - entry_idx   # gap -> exit at open
            if h >= tp:
                return "TP", tp, j - entry_idx
        else:
            if h >= sl:
                return "SL", max(sl, o), j - entry_idx
            if low <= tp:
                return "TP", tp, j - entry_idx
    if MAX_HOLD_CANDLES is None:
        return "OPEN", c[end][4], end - entry_idx  # data ended, trade still open
    return "TIMEOUT", c[end][4], end - entry_idx


def report(title, trades):
    if not trades:
        print(f"{title}: 0 trades")
        return
    wins = [t for t in trades if t["r"] > 0]
    gw = sum(t["r"] for t in trades if t["r"] > 0)
    gl = -sum(t["r"] for t in trades if t["r"] < 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"{title}: {len(trades)} trades | win {len(wins)} ({len(wins)/len(trades)*100:.0f}%) | "
          f"total {sum(t['r'] for t in trades):+.1f}R | avg {sum(t['r'] for t in trades)/len(trades):+.3f}R | "
          f"PF {pf:.2f} | total (1 unit capital/trade) {sum(t['ret'] for t in trades):+.1f}%")


def max_drawdown_r(trades):
    equity = peak = dd = 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def main():
    print(f"Fetching {SYMBOL} {INTERVAL} data...")
    c = fetch_candles(SYMBOL, INTERVAL, TOTAL_CANDLES)
    print(f"{len(c)} candles | {c[0][0]:%Y-%m-%d} to {c[-1][0]:%Y-%m-%d}")

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
        # Cross detection
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

        # Signal candle: pattern must match the cross direction
        pats = [name for name, d in candle_patterns(c[i]) if d == last_cross_dir]
        if not pats:
            continue

        # Angle filter: bullish +ANGLE_MIN..+ANGLE_MAX, bearish -ANGLE_MIN..-ANGLE_MAX
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

        # S/R filter: BUY must touch support, SELL must touch resistance
        res_levels, sup_levels = sr_levels(raw_high, raw_low, closes, i)
        if USE_SR_FILTER:
            ok = touches_support(c[i], sup_levels) if direction == "bull" \
                else touches_resistance(c[i], res_levels)
            if not ok:
                skipped_sr += 1
                continue

        entry_idx = i + 1
        entry = c[entry_idx][1]  # next candle's open
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

        # Optional: an opposing level between entry and TP may block the target
        if USE_SR_FILTER and SR_BLOCK_TP:
            if direction == "bull" and any(entry < lv < tp for lv in res_levels):
                skipped_sr += 1
                continue
            if direction == "bear" and any(tp < lv < entry for lv in sup_levels):
                skipped_sr += 1
                continue

        outcome, exit_price, bars = simulate(c, entry_idx, direction, entry, sl, tp)
        if outcome == "OPEN":
            print(f"\nINFO: last trade ({c[entry_idx][0]:%Y-%m-%d %H:%M}, {direction}) "
                  f"had no SL/TP by the end of data, excluded")
            break

        raw = (exit_price - entry) / entry * 100
        ret = (raw if direction == "bull" else -raw) - ROUND_TRIP_COST_PCT
        risk_pct = risk / entry * 100
        gross_r = ((exit_price - entry) if direction == "bull" else (entry - exit_price)) / risk
        r_mult = gross_r - ROUND_TRIP_COST_PCT / risk_pct   # net R after costs

        trades.append({"time": c[entry_idx][0], "direction": direction, "pattern": "+".join(pats),
                       "entry": entry, "sl": sl, "tp": tp, "outcome": outcome,
                       "ret": ret, "r": r_mult, "bars": bars})
        used_cross = last_cross_idx
        next_free = entry_idx + bars + 1

    print(f"\n{'=' * 60}")
    for t in trades:
        side = "BUY " if t["direction"] == "bull" else "SELL"
        print(f"{t['time']:%Y-%m-%d %H:%M} {side} [{t['pattern']}] @ {t['entry']:,.2f} "
              f"SL {t['sl']:,.2f} TP {t['tp']:,.2f} -> {t['outcome']} {t['ret']:+.2f}% ({t['r']:+.2f}R)")

    print(f"\n{'=' * 60}\nSUMMARY (R = net of costs)")
    if angles_seen:
        s = sorted(angles_seen)
        pick = lambda p: s[min(len(s) - 1, int(len(s) * p))]  # noqa: E731
        print(f"Angle diagnostics ({len(s)} pattern signals): "
              f"min {s[0]:.0f} | 25% {pick(0.25):.0f} | median {pick(0.5):.0f} | "
              f"75% {pick(0.75):.0f} | 90% {pick(0.9):.0f} | max {s[-1]:.0f} (degrees)")
        in_range = sum(1 for a in s if ANGLE_MIN <= a <= ANGLE_MAX)
        print(f"  {in_range} in {ANGLE_MIN}-{ANGLE_MAX} deg | skipped by angle: {skipped_angle} | "
              f"skipped by S/R: {skipped_sr}")
        if in_range < 30:
            print("  WARNING: few signals - tune ANGLE_SCALE / ANGLE_LOOKBACK to fit the degree scale")

    report("Total", trades)
    for p in ("Marubozu", "Hammer", "ShootingStar", "HangingMan"):
        sub = [t for t in trades if p in t["pattern"].split("+")]
        if sub:
            report(f"  {p}", sub)
    report("  BUY only", [t for t in trades if t["direction"] == "bull"])
    report("  SELL only", [t for t in trades if t["direction"] == "bear"])

    split_time = c[int(len(c) * IS_FRACTION)][0]
    print(f"\nIn-sample / out-of-sample (split: {split_time:%Y-%m-%d})")
    report("  In-sample", [t for t in trades if t["time"] < split_time])
    report("  Out-of-sample", [t for t in trades if t["time"] >= split_time])

    print(f"\nMax drawdown: -{max_drawdown_r(trades):.1f}R | "
          f"breakeven win rate (1:{RR:g}, before costs): {100/(1+RR):.0f}%")


if __name__ == "__main__":
    main()
