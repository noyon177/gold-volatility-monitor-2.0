
import os
import json
import datetime as dt
import requests
from pathlib import Path

# ============================================================
# EMA 9/15 + PINBAR + 30M SUPPORT / RESISTANCE SCANNER
# Version 3.0
# ============================================================

# ---------------- CONFIGURATION ----------------

SYMBOLS = [
    {
        "name": "XAUUSD",
        "td_symbol": "XAU/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
    },
    {
        "name": "BTCUSD",
        "td_symbol": "BTC/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
    },
]

# Entry timeframe
INTERVAL = "15min"
INTERVAL_MINUTES = 15

# Support / Resistance timeframe
SR_INTERVAL = "30min"
SR_INTERVAL_MINUTES = 30

# Indicators
EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14
EMA_SLOPE_LOOKBACK = 3

# Candle history
CANDLES_NEEDED = 150
SR_LOOKBACK = 100

# Pinbar settings
PINBAR_WICK_RATIO = 2.0
PINBAR_NOSE_MAX_RATIO = 0.40
CLOSE_POSITION_LIMIT = 0.30

# Risk management
SL_BUFFER_ATR = 0.10
RISK_REWARD = 2.0

# Signal timing
MAX_SIGNAL_DELAY_MIN = 10

# Support / Resistance settings
SR_PIVOT_LEFT = 2
SR_PIVOT_RIGHT = 2
SR_CLUSTER_ATR = 0.25
SR_MAX_LEVELS = 3

# Heartbeat
HEARTBEAT_INTERVAL_MINUTES = 0

STATE_FILE = Path("last_signal_state.json")

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "EMA-Pinbar-SR-Scanner/3.0"
})


# ============================================================
# TIME
# ============================================================

def now_utc():
    return dt.datetime.now(UTC)


# ============================================================
# DATA FETCHING
# ============================================================

def get_candles(td_symbol, interval, outputsize):

    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY")

    response = SESSION.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": td_symbol,
            "interval": interval,
            "outputsize": outputsize,
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
            row["datetime"],
            "%Y-%m-%d %H:%M:%S"
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


def drop_unclosed(candles, interval_minutes):

    if not candles:
        return []

    current_time = now_utc()
    closed = []

    for candle in candles:

        close_time = candle["dt"] + dt.timedelta(
            minutes=interval_minutes
        )

        if close_time <= current_time:
            closed.append(candle)

    return closed


# ============================================================
# INDICATORS
# ============================================================

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

    result[period - 1] = (
        sum(true_ranges[:period]) / period
    )

    for i in range(period, len(candles)):

        result[i] = (
            result[i - 1] * (period - 1)
            + true_ranges[i]
        ) / period

    return result


# ============================================================
# PINBAR DETECTION
# ============================================================

def classify_pinbar(candle):

    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    candle_range = h - l

    if candle_range <= 0:
        return None

    body = abs(c - o)
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


# ============================================================
# EMA TOUCH
# ============================================================

def touches_ema(candle, ema_value, atr_value, tolerance_multiplier):

    tolerance = atr_value * tolerance_multiplier

    return (
        candle["low"] <= ema_value + tolerance
        and candle["high"] >= ema_value - tolerance
    )


# ============================================================
# 30M SUPPORT / RESISTANCE ENGINE
# ============================================================

def calculate_sr_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        trs.append(tr)

    return sum(trs[-period:]) / period


def identify_sr_levels(candles, current_price):

    candles = candles[-SR_LOOKBACK:]

    empty_result = {
        "support": [],
        "resistance": [],
        "nearest_support": None,
        "nearest_resistance": None,
        "atr": None,
    }

    if len(candles) < 30:
        return empty_result

    atr = calculate_sr_atr(candles)

    if atr is None or atr <= 0:
        return empty_result

    tolerance = atr * SR_CLUSTER_ATR

    support_candidates = []
    resistance_candidates = []

    left = SR_PIVOT_LEFT
    right = SR_PIVOT_RIGHT

    # Confirmed swing points
    for i in range(left, len(candles) - right):

        candle = candles[i]

        left_candles = candles[i-left:i]
        right_candles = candles[i+1:i+right+1]

        # Swing Low
        is_swing_low = all(
            candle["low"] < x["low"]
            for x in left_candles + right_candles
        )

        if is_swing_low:

            support_candidates.append({
                "price": candle["low"],
                "time": candle["time"],
            })

        # Swing High
        is_swing_high = all(
            candle["high"] > x["high"]
            for x in left_candles + right_candles
        )

        if is_swing_high:

            resistance_candidates.append({
                "price": candle["high"],
                "time": candle["time"],
            })

    def cluster_levels(candidates):

        if not candidates:
            return []

        candidates = sorted(
            candidates,
            key=lambda x: x["price"]
        )

        clusters = []

        for candidate in candidates:

            if not clusters:
                clusters.append([candidate])
                continue

            last_cluster = clusters[-1]

            average = sum(
                x["price"] for x in last_cluster
            ) / len(last_cluster)

            if abs(candidate["price"] - average) <= tolerance:
                last_cluster.append(candidate)

            else:
                clusters.append([candidate])

        levels = []

        for cluster in clusters:

            price = sum(
                x["price"] for x in cluster
            ) / len(cluster)

            levels.append({
                "price": price,
                "touches": len(cluster),
                "time": cluster[-1]["time"],
            })

        return levels

    supports = cluster_levels(support_candidates)
    resistances = cluster_levels(resistance_candidates)

    # Keep only levels on the relevant side of price
    supports = [
        x for x in supports
        if x["price"] < current_price
    ]

    resistances = [
        x for x in resistances
        if x["price"] > current_price
    ]

    supports.sort(
        key=lambda x: current_price - x["price"]
    )

    resistances.sort(
        key=lambda x: x["price"] - current_price
    )

    return {
        "support": supports[:SR_MAX_LEVELS],
        "resistance": resistances[:SR_MAX_LEVELS],
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "atr": atr,
    }


def format_sr_message(sr, current_price):

    lines = [
        "\n📊 <b>30M SUPPORT / RESISTANCE</b>"
    ]

    support = sr["nearest_support"]
    resistance = sr["nearest_resistance"]

    if support:

        distance = current_price - support["price"]

        lines.extend([
            f"🟢 Support: <b>{support['price']:.3f}</b>",
            f"Distance: {distance:.3f}",
            f"Swing points: {support['touches']}",
        ])

    else:
        lines.append("🟢 Support: পাওয়া যায়নি")

    if resistance:

        distance = resistance["price"] - current_price

        lines.extend([
            f"🔴 Resistance: <b>{resistance['price']:.3f}</b>",
            f"Distance: {distance:.3f}",
            f"Swing points: {resistance['touches']}",
        ])

    else:
        lines.append("🔴 Resistance: পাওয়া যায়নি")

    # Additional levels
    if len(sr["support"]) > 1:

        lines.append("\n<b>Other Support Levels</b>")

        for level in sr["support"][1:]:

            lines.append(
                f"• {level['price']:.3f} "
                f"({level['touches']} swing points)"
            )

    if len(sr["resistance"]) > 1:

        lines.append("\n<b>Other Resistance Levels</b>")

        for level in sr["resistance"][1:]:

            lines.append(
                f"• {level['price']:.3f} "
                f"({level['touches']} swing points)"
            )

    lines.append(
        "\n<i>Calculated from closed 30M candles.</i>"
    )

    return "\n".join(lines)


# ============================================================
# SIGNAL ENGINE
# ============================================================

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
        ema9[i],
        ema15[i],
        atr[i],
        ema15[p],
        ema15[j],
        ema9[j],
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

    # BUY
    if pinbar_type == "bullish" and trend_up:

        if previous["close"] <= ema15[p]:
            return None

        entry = candle["close"]
        sl = candle["low"] - buffer_distance

        risk = entry - sl

        if risk <= 0:
            return None

        risk_atr = risk / atr_now

        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):
            return None

        tp = entry + RISK_REWARD * risk
        side = "BUY"

    # SELL
    elif pinbar_type == "bearish" and trend_down:

        if previous["close"] >= ema15[p]:
            return None

        entry = candle["close"]
        sl = candle["high"] + buffer_distance

        risk = sl - entry

        if risk <= 0:
            return None

        risk_atr = risk / atr_now

        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):
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


# ============================================================
# TELEGRAM
# ============================================================

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
        raise RuntimeError(
            f"Telegram rejected message: {result}"
        )

    return True


# ============================================================
# STATE MANAGEMENT
# ============================================================

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


# ============================================================
# HEARTBEAT
# ============================================================

def should_send_heartbeat(state):

    last = state.get("_last_heartbeat")

    if not last:
        return True

    last_time = dt.datetime.fromisoformat(last)

    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=UTC)

    elapsed = (
        now_utc() - last_time
    ).total_seconds() / 60

    return elapsed >= HEARTBEAT_INTERVAL_MINUTES


def send_heartbeat(market_data):

    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"✅ <b>বট চালু আছে</b>",
        f"সময়: {stamp}",
    ]

    for name, data in market_data.items():

        lines.append(f"\n<b>{name}</b>")

        if data is None:
            lines.append("ডেটা আনতে ব্যর্থ")
            continue

        price = data["price"]
        sr = data["sr"]

        lines.append(f"Price: {price:.3f}")

        support = sr["nearest_support"]
        resistance = sr["nearest_resistance"]

        if support:
            lines.append(
                f"🟢 Support: {support['price']:.3f}"
            )
        else:
            lines.append("🟢 Support: পাওয়া যায়নি")

        if resistance:
            lines.append(
                f"🔴 Resistance: {resistance['price']:.3f}"
            )
        else:
            lines.append("🔴 Resistance: পাওয়া যায়নি")

    return send_telegram("\n".join(lines))


# ============================================================
# MAIN
# ============================================================

def main():

    state = load_state()
    market_data = {}

    for config in SYMBOLS:

        name = config["name"]

        try:

            # ---------------- 15M DATA ----------------

            raw = get_candles(
                config["td_symbol"],
                interval=INTERVAL,
                outputsize=CANDLES_NEEDED,
            )

            candles = drop_unclosed(
                raw,
                INTERVAL_MINUTES,
            )

            if not candles:
                print(f"[{name}] No closed 15M candles")
                market_data[name] = None
                continue

            current_price = candles[-1]["close"]

            # ---------------- 30M DATA ----------------

            sr_raw = get_candles(
                config["td_symbol"],
                interval=SR_INTERVAL,
                outputsize=SR_LOOKBACK + 20,
            )

            sr_candles = drop_unclosed(
                sr_raw,
                SR_INTERVAL_MINUTES,
            )

            sr_data = identify_sr_levels(
                sr_candles,
                current_price,
            )

            market_data[name] = {
                "price": current_price,
                "sr": sr_data,
            }

            print(
                f"[{name}] Price: {current_price:.3f}"
            )

            if sr_data["nearest_support"]:
                print(
                    f"[{name}] Support: "
                    f"{sr_data['nearest_support']['price']:.3f}"
                )

            if sr_data["nearest_resistance"]:
                print(
                    f"[{name}] Resistance: "
                    f"{sr_data['nearest_resistance']['price']:.3f}"
                )

            # ---------------- SIGNAL ----------------

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

            sr_message = format_sr_message(
                sr_data,
                signal["entry"],
            )

            message = (
                f"📢 <b>{signal['side']} SIGNAL — {name}</b>\n"
                f"Time
