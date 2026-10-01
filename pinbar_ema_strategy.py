
import os
import json
import datetime as dt
import requests
from pathlib import Path

# ============================================================
# PINBAR + EMA 9/15 TREND REJECTION SCANNER
# Revised version
# ============================================================

# ---------------- CONFIGURATION ----------------

SYMBOLS = [
    {
        "name": "XAUUSD",
        "td_symbol": "XAU/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
        "max_entry_deviation_atr": 0.50,
    },
    {
        "name": "BTCUSD",
        "td_symbol": "BTC/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
        "max_entry_deviation_atr": 0.50,
    },
]

INTERVAL = "15min"
INTERVAL_MINUTES = 15

EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14

CANDLES_NEEDED = 150

PINBAR_WICK_RATIO = 2.0
PINBAR_NOSE_MAX_RATIO = 0.40

# Candle close should be near the extreme
CLOSE_POSITION_LIMIT = 0.30

SL_BUFFER_ATR = 0.10
RISK_REWARD = 2.0

EMA_SLOPE_LOOKBACK = 3

MAX_SIGNAL_DELAY_MIN = 10

HEARTBEAT_INTERVAL_MINUTES = 60

STATE_FILE = Path("last_signal_state.json")

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "EMA-Pinbar-Scanner/2.0"})


# ---------------- TIME ----------------

def now_utc():
    return dt.datetime.now(UTC)


# ---------------- DATA FETCHING ----------------

def get_candles(td_symbol):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY")

    response = SESSION.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": td_symbol,
            "interval": INTERVAL,
            "outputsize": CANDLES_NEEDED,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=20,
    )

    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")

    candles = []

    for row in reversed(data["values"]):
        timestamp = dt.datetime.strptime(
            row["datetime"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)

        candles.append({
            "time": row["datetime"],
            "dt": timestamp,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    return candles


def drop_unclosed(candles):
    """
    Assumes Twelve Data timestamps represent candle OPEN time.
    Verify this convention for your selected data feed.
    """

    if not candles:
        return []

    last = candles[-1]
    close_time = last["dt"] + dt.timedelta(minutes=INTERVAL_MINUTES)

    if close_time > now_utc():
        return candles[:-1]

    return candles


# ---------------- INDICATORS ----------------

def ema_series(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    k = 2 / (period + 1)

    result[period - 1] = sum(values[:period]) / period

    for i in range(period, len(values)):
        result[i] = (
            values[i] * k
            + result[i - 1] * (1 - k)
        )

    return result


def atr_series(candles, period):
    if not candles:
        return []

    true_ranges = []

    for i, candle in enumerate(candles):
        if i == 0:
            tr = candle["high"] - candle["low"]
        else:
            previous_close = candles[i - 1]["close"]

            tr = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )

        true_ranges.append(tr)

    result = [None] * len(candles)

    if len(candles) < period:
        return result

    result[period - 1] = sum(true_ranges[:period]) / period

    for i in range(period, len(candles)):
        result[i] = (
            result[i - 1] * (period - 1) + true_ranges[i]
        ) / period

    return result


# ---------------- PINBAR DETECTION ----------------

def classify_pinbar(candle):
    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    candle_range = h - l

    if candle_range <= 0:
        return None

    body = abs(c - o)

    # Avoid division by zero for doji candles
    body_for_ratio = max(body, candle_range * 0.01)

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    close_position = (c - l) / candle_range

    # Bullish rejection
    if (
        lower_wick >= PINBAR_WICK_RATIO * body_for_ratio
        and upper_wick <= PINBAR_NOSE_MAX_RATIO * lower_wick
        and close_position >= 1 - CLOSE_POSITION_LIMIT
    ):
        return "bullish"

    # Bearish rejection
    if (
        upper_wick >= PINBAR_WICK_RATIO * body_for_ratio
        and lower_wick <= PINBAR_NOSE_MAX_RATIO * upper_wick
        and close_position <= CLOSE_POSITION_LIMIT
    ):
        return "bearish"

    return None


# ---------------- EMA TOUCH ----------------

def touches_ema(candle, ema_value, atr_value, tolerance_multiplier):
    tolerance = atr_value * tolerance_multiplier

    return (
        candle["low"] <= ema_value + tolerance
        and candle["high"] >= ema_value - tolerance
    )


# ---------------- SIGNAL ENGINE ----------------

def check_signal(candles, config):

    minimum_candles = (
        max(ATR_PERIOD, EMA_SLOW)
        + EMA_SLOPE_LOOKBACK
        + 2
    )

    if len(candles) < minimum_candles:
        return None

    closes = [c["close"] for c in candles]

    ema9 = ema_series(closes, EMA_FAST)
    ema15 = ema_series(closes, EMA_SLOW)
    atr = atr_series(candles, ATR_PERIOD)

    i = len(candles) - 1
    p = i - 1
    j = i - EMA_SLOPE_LOOKBACK

    if any(x is None for x in (
        ema9[i], ema15[i], atr[i], ema15[p], ema15[j],
        ema9[j]
    )):
        return None

    candle = candles[i]
    previous = candles[p]

    atr_now = atr[i]

    if atr_now <= 0:
        return None

    pinbar_type = classify_pinbar(candle)

    if pinbar_type is None:
        return None

    if not touches_ema(
        candle,
        ema15[i],
        atr_now,
        config["touch_atr"],
    ):
        return None

    # Trend filters
    trend_up = (
        ema9[i] > ema15[i]
        and ema15[i] > ema15[j]
        and ema9[i] > ema9[j]
    )

    trend_down = (
        ema9[i] < ema15[i]
        and ema15[i] < ema15[j]
        and ema9[i] < ema9[j]
    )

    buffer_distance = SL_BUFFER_ATR * atr_now

    # ---------------- BUY ----------------

    if pinbar_type == "bullish" and trend_up:

        # Previous candle should be on trend side of EMA15
        if previous["close"] <= ema15[p]:
            return None

        entry = candle["close"]
        sl = candle["low"] - buffer_distance

        risk = entry - sl

        if risk <= 0:
            return None

        risk_atr = risk / atr_now

        if risk_atr < config["min_sl_atr"]:
            return None

        if risk_atr > config["max_sl_atr"]:
            return None

        tp = entry + RISK_REWARD * risk
        side = "BUY"

    # ---------------- SELL ----------------

    elif pinbar_type == "bearish" and trend_down:

        if previous["close"] >= ema15[p]:
            return None

        entry = candle["close"]
        sl = candle["high"] + buffer_distance

        risk = sl - entry

        if risk <= 0:
            return None

        risk_atr = risk / atr_now

        if risk_atr < config["min_sl_atr"]:
            return None

        if risk_atr > config["max_sl_atr"]:
            return None

        tp = entry - RISK_REWARD * risk
        side = "SELL"

    else:
        return None

    close_dt = candle["dt"] + dt.timedelta(
        minutes=INTERVAL_MINUTES
    )

    return {
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
        "time": candle["time"],
        "close_dt": close_dt,
        "candle": candle,
        "ema9": ema9[i],
        "ema15": ema15[i],
        "atr": atr_now,
    }


# ---------------- TELEGRAM ----------------

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing Telegram credentials")

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = SESSION.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        },
        timeout=20,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {result}")

    return True


# ---------------- STATE ----------------

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    temp_file.replace(STATE_FILE)


# ---------------- HEARTBEAT ----------------

def should_send_heartbeat(state):
    last = state.get("_last_heartbeat")

    if not last:
        return True

    last_time = dt.datetime.fromisoformat(last)

    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=UTC)

    elapsed = (now_utc() - last_time).total_seconds() / 60

    return elapsed >= HEARTBEAT_INTERVAL_MINUTES


def send_heartbeat(prices):
    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"✅ <b>বট চালু আছে</b> ({stamp})"
    ]

    for name, price in prices.items():
        if price is None:
            lines.append(f"{name}: ডেটা আনতে ব্যর্থ")
        else:
            lines.append(f"{name}: {price:.3f}")

    return send_telegram("\n".join(lines))


# ---------------- MAIN ----------------

def main():

    state = load_state()
    last_prices = {}

    for config in SYMBOLS:

        name = config["name"]

        try:
            raw = get_candles(config["td_symbol"])

            if not raw:
                print(f"[{name}] Empty candle response")
                last_prices[name] = None
                continue

            # Latest returned close is a data-feed reference,
            # not guaranteed to be a live executable quote.
            last_prices[name] = raw[-1]["close"]

            candles = drop_unclosed(raw)

            if not candles:
                print(f"[{name}] No closed candles")
                continue

            signal = check_signal(candles, config)

            if not signal:
                print(f"[{name}] No signal")
                continue

            delay_min = (
                now_utc() - signal["close_dt"]
            ).total_seconds() / 60

            if delay_min < 0:
                print(f"[{name}] Candle close time is in future")
                continue

            if delay_min > MAX_SIGNAL_DELAY_MIN:
                print(
                    f"[{name}] Stale signal skipped "
                    f"({delay_min:.1f} minutes)"
                )
                continue

            if state.get(name) == signal["time"]:
                print(f"[{name}] Duplicate signal skipped")
                continue

            candle = signal["candle"]

            message = (
                f"📢 <b>{signal['side']} SIGNAL — {name}</b>\n"
                f"Timeframe: 15M\n"
                f"Strategy: EMA 9/15 + Pin Bar Rejection\n\n"
                f"Entry: {signal['entry']:.3f}\n"
                f"SL: {signal['sl']:.3f}\n"
                f"TP: {signal['tp']:.3f}\n"
                f"Risk: {signal['risk']:.3f}\n"
                f"Risk in ATR: {signal['risk_atr']:.2f}\n"
                f"RR: 1:{RISK_REWARD:.1f}\n\n"
                f"Candle time UTC: {signal['time']}\n"
                f"Signal delay: {delay_min:.1f} min\n\n"
                f"<b>Confirmation Data</b>\n"
                f"O: {candle['open']:.3f}\n"
                f"H: {candle['high']:.3f}\n"
                f"L: {candle['low']:.3f}\n"
                f"C: {candle['close']:.3f}\n"
                f"EMA9: {signal['ema9']:.3f}\n"
                f"EMA15: {signal['ema15']:.3f}\n"
                f"ATR14: {signal['atr']:.3f}\n\n"
                f"⚠️ Verify current market price, spread "
                f"and slippage before entry.\n"
                f"<i>Data: Twelve Data. Broker prices may differ.</i>"
            )

            # Save state ONLY after successful Telegram delivery
            send_telegram(message)

            state[name] = signal["time"]

            print(
                f"[{name}] SIGNAL SENT: "
                f"{signal['side']} @ {signal['entry']}"
            )

        except Exception as error:
            print(f"[{name}] ERROR: {error}")
            last_prices[name] = None

    try:
        if should_send_heartbeat(state):
            send_heartbeat(last_prices)
            state["_last_heartbeat"] = now_utc().isoformat()

    except Exception as error:
        print(f"Heartbeat error: {error}")

    save_state(state)


if __name__ == "__main__":
    main()
