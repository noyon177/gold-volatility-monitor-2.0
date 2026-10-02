
import os
import json
import datetime as dt
import requests
from pathlib import Path

# ============================================================
# EMA 9/15 + PINBAR + DYNAMIC MULTI-TF SUPPORT / RESISTANCE
# Version 4.0
#
# Main changes:
# - No hard-coded XAUUSD/BTCUSD S/R prices.
# - 30M S/R uses confirmed swing pivots + ATR clustering.
# - Repeated + recent touches receive more weight.
# - 4H S/R is added as higher-timeframe context.
# - Existing EMA 9/15 + pinbar signal logic is retained.
# ============================================================

SYMBOLS = [
    {
        "name": "XAUUSD",
        "td_symbol": "XAU/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
        "digits": 3,
    },
    {
        "name": "BTCUSD",
        "td_symbol": "BTC/USD",
        "touch_atr": 0.15,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
        "digits": 3,
    },
]

INTERVAL = "15min"
INTERVAL_MINUTES = 15

SR_INTERVAL = "30min"
SR_INTERVAL_MINUTES = 30

# Higher-timeframe S/R context
HTF_SR_ENABLED = True
HTF_SR_INTERVAL = "4h"
HTF_SR_INTERVAL_MINUTES = 240

EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14
EMA_SLOPE_LOOKBACK = 3

CANDLES_NEEDED = 150
SR_LOOKBACK = 180
HTF_SR_LOOKBACK = 120

PINBAR_WICK_RATIO = 2.0
PINBAR_NOSE_MAX_RATIO = 0.40
CLOSE_POSITION_LIMIT = 0.30

SL_BUFFER_ATR = 0.10
RISK_REWARD = 2.0

MAX_SIGNAL_DELAY_MIN = 10

# Dynamic S/R
SR_PIVOT_LEFT = 3
SR_PIVOT_RIGHT = 3
SR_CLUSTER_ATR = 0.35
SR_MAX_LEVELS = 3
SR_MIN_TOUCHES = 2
SR_MIN_DISTANCE_ATR = 0.20

# Recency weighting: after this many bars, a pivot's
# recency contribution is reduced by roughly 50%.
SR_RECENCY_HALF_LIFE_BARS = 48
SR_HTF_WEIGHT = 1.35

# 0 = send heartbeat every run, as in your old version.
HEARTBEAT_INTERVAL_MINUTES = 0

STATE_FILE = Path("last_signal_state.json")

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

UTC = dt.timezone.utc

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "EMA-Pinbar-Dynamic-SR-Scanner/4.0"
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
    current_time = now_utc()

    return [
        candle
        for candle in candles
        if candle["dt"] + dt.timedelta(
            minutes=interval_minutes
        ) <= current_time
    ]


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
# PINBAR
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
    body_for_ratio = max(
        body,
        candle_range * 0.01
    )

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    close_position = (
        (c - l) / candle_range
    )

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

def touches_ema(
    candle,
    ema_value,
    atr_value,
    tolerance_multiplier
):
    tolerance = atr_value * tolerance_multiplier

    return (
        candle["low"] <= ema_value + tolerance
        and candle["high"] >= ema_value - tolerance
    )


# ============================================================
# DYNAMIC SUPPORT / RESISTANCE
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


def find_pivots(candles):
    left = SR_PIVOT_LEFT
    right = SR_PIVOT_RIGHT
    n = len(candles)

    pivots = []

    for i in range(left, n - right):
        candle = candles[i]

        left_candles = candles[i - left:i]
        right_candles = candles[i + 1:i + right + 1]

        is_low = (
            all(
                candle["low"] <= x["low"]
                for x in left_candles
            )
            and all(
                candle["low"] < x["low"]
                for x in right_candles
            )
        )

        is_high = (
            all(
                candle["high"] >= x["high"]
                for x in left_candles
            )
            and all(
                candle["high"] > x["high"]
                for x in right_candles
            )
        )

        if is_low:
            pivots.append({
                "price": candle["low"],
                "idx": i,
                "time": candle["time"],
                "kind": "low",
            })

        if is_high:
            pivots.append({
                "price": candle["high"],
                "idx": i,
                "time": candle["time"],
                "kind": "high",
            })

    return pivots


def cluster_pivots(pivots, tolerance):
    if not pivots:
        return []

    # Price-based clustering.
    pivots = sorted(
        pivots,
        key=lambda x: x["price"]
    )

    clusters = []

    for pivot in pivots:
        if not clusters:
            clusters.append([pivot])
            continue

        average_price = (
            sum(x["price"] for x in clusters[-1])
            / len(clusters[-1])
        )

        if abs(
            pivot["price"] - average_price
        ) <= tolerance:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])

    return clusters


def level_strength(cluster, total_bars):
    """
    Strength combines:
    - number of touches
    - recency of touches
    - a small bonus for a recently active zone
    """

    strength = 0.0

    latest_idx = max(
        x["idx"] for x in cluster
    )

    for pivot in cluster:
        age = max(
            0,
            total_bars - 1 - pivot["idx"]
        )

        decay = (
            0.5 ** (
                age / SR_RECENCY_HALF_LIFE_BARS
            )
        )

        strength += decay

    touch_bonus = min(
        len(cluster),
        8
    ) * 0.75

    recent_bonus = (
        1.0
        if latest_idx >= total_bars * 0.75
        else 0.0
    )

    return (
        strength
        + touch_bonus
        + recent_bonus
    )


def build_sr_levels(
    candles,
    current_price,
    htf=False
):
    lookback = (
        HTF_SR_LOOKBACK
        if htf
        else SR_LOOKBACK
    )

    candles = candles[-lookback:]

    empty = {
        "support": [],
        "resistance": [],
        "nearest_support": None,
        "nearest_resistance": None,
        "atr": None,
    }

    if len(candles) < 30:
        return empty

    atr = calculate_sr_atr(candles)

    if atr is None or atr <= 0:
        return empty

    tolerance = atr * (
        SR_CLUSTER_ATR * (
            1.25 if htf else 1.0
        )
    )

    min_distance = (
        atr * SR_MIN_DISTANCE_ATR
    )

    pivots = find_pivots(candles)

    clusters = cluster_pivots(
        pivots,
        tolerance
    )

    levels = []

    for cluster in clusters:
        price = (
            sum(x["price"] for x in cluster)
            / len(cluster)
        )

        latest = max(
            cluster,
            key=lambda x: x["idx"]
        )

        strength = level_strength(
            cluster,
            len(candles)
        )

        if htf:
            strength *= SR_HTF_WEIGHT

        levels.append({
            "price": price,
            "touches": len(cluster),
            "time": latest["time"],
            "weak": (
                len(cluster)
                < SR_MIN_TOUCHES
            ),
            "strength": strength,
            "timeframe": (
                "4H" if htf else "30M"
            ),
        })

    supports = [
        x for x in levels
        if x["price"]
        < current_price - min_distance
    ]

    resistances = [
        x for x in levels
        if x["price"]
        > current_price + min_distance
    ]

    def selection_score(
        level,
        support
    ):
        distance = (
            current_price - level["price"]
            if support
            else level["price"] - current_price
        )

        distance_atr = (
            distance / atr
        )

        proximity = (
            1 / (
                1 + max(
                    distance_atr,
                    0
                )
            )
        )

        return (
            level["strength"] * 0.65
            + proximity * 4.0
        )

    supports.sort(
        key=lambda x: selection_score(
            x,
            True
        ),
        reverse=True
    )

    resistances.sort(
        key=lambda x: selection_score(
            x,
            False
        ),
        reverse=True
    )

    nearest_support = (
        min(
            supports,
            key=lambda x:
            current_price - x["price"]
        )
        if supports
        else None
    )

    nearest_resistance = (
        min(
            resistances,
            key=lambda x:
            x["price"] - current_price
        )
        if resistances
        else None
    )

    return {
        "support": supports[:SR_MAX_LEVELS],
        "resistance": resistances[:SR_MAX_LEVELS],
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "atr": atr,
    }


def identify_sr_levels(
    candles,
    current_price,
    htf_candles=None
):
    base = build_sr_levels(
        candles,
        current_price,
        htf=False
    )

    if (
        HTF_SR_ENABLED
        and htf_candles
    ):
        htf = build_sr_levels(
            htf_candles,
            current_price,
            htf=True
        )

        base["htf_support"] = (
            htf["nearest_support"]
        )

        base["htf_resistance"] = (
            htf["nearest_resistance"]
        )

        base["htf_supports"] = (
            htf["support"]
        )

        base["htf_resistances"] = (
            htf["resistance"]
        )

    else:
        base["htf_support"] = None
        base["htf_resistance"] = None
        base["htf_supports"] = []
        base["htf_resistances"] = []

    return base


def weak_tag(level):
    return (
        " (দুর্বল)"
        if level.get("weak")
        else ""
    )


def format_level(
    level,
    digits=3
):
    return (
        f"{level['price']:.{digits}f} "
        f"({level['touches']}x, "
        f"{level['timeframe']})"
        f"{weak_tag(level)}"
    )


def format_sr_message(
    sr,
    current_price,
    digits=3
):
    lines = [
        "\n📊 <b>DYNAMIC "
        "SUPPORT / RESISTANCE</b>"
    ]

    support = sr.get(
        "nearest_support"
    )

    resistance = sr.get(
        "nearest_resistance"
    )

    if support:
        lines.extend([
            f"🟢 Support: "
            f"<b>{support['price']:.{digits}f}</b>"
            f"{weak_tag(support)}",
            f"Distance: "
            f"{current_price - support['price']:.{digits}f}",
        ])
    else:
        lines.append(
            "🟢 Support: পাওয়া যায়নি"
        )

    if resistance:
        lines.extend([
            f"🔴 Resistance: "
            f"<b>{resistance['price']:.{digits}f}</b>"
            f"{weak_tag(resistance)}",
            f"Distance: "
            f"{resistance['price'] - current_price:.{digits}f}",
        ])
    else:
        lines.append(
            "🔴 Resistance: পাওয়া যায়নি"
        )

    if sr.get("support"):
        lines.append(
            "\n<b>30M Support Zones</b>"
        )

        for level in sr["support"]:
            lines.append(
                "• "
                + format_level(
                    level,
                    digits
                )
            )

    if sr.get("resistance"):
        lines.append(
            "\n<b>30M Resistance Zones</b>"
        )

        for level in sr["resistance"]:
            lines.append(
                "• "
                + format_level(
                    level,
                    digits
                )
            )

    if sr.get("htf_support"):
        lines.append(
            "\n🟢 4H Context Support: "
            f"<b>{sr['htf_support']['price']:.{digits}f}</b>"
        )

    if sr.get("htf_resistance"):
        lines.append(
            "🔴 4H Context Resistance: "
            f"<b>{sr['htf_resistance']['price']:.{digits}f}</b>"
        )

    lines.append(
        "\n<i>Calculated from closed candles; "
        "levels update automatically.</i>"
    )

    return "\n".join(lines)


# ============================================================
# SIGNAL ENGINE
# ============================================================

def check_signal(
    candles,
    config
):
    minimum_candles = (
        max(
            ATR_PERIOD,
            EMA_SLOW
        )
        + EMA_SLOPE_LOOKBACK
        + 2
    )

    if len(candles) < minimum_candles:
        return None

    closes = [
        c["close"]
        for c in candles
    ]

    ema9 = ema_series(
        closes,
        EMA_FAST
    )

    ema15 = ema_series(
        closes,
        EMA_SLOW
    )

    atr = atr_series(
        candles,
        ATR_PERIOD
    )

    i = len(candles) - 1
    p = i - 1
    j = i - EMA_SLOPE_LOOKBACK

    if any(
        x is None
        for x in (
            ema9[i],
            ema15[i],
            atr[i],
            ema15[p],
            ema15[j],
            ema9[j],
        )
    ):
        return None

    candle = candles[i]
    previous = candles[p]
    atr_now = atr[i]

    if atr_now <= 0:
        return None

    pinbar_type = classify_pinbar(
        candle
    )

    if pinbar_type is None:
        return None

    if not touches_ema(
        candle,
        ema15[i],
        atr_now,
        config["touch_atr"]
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

    buffer_distance = (
        SL_BUFFER_ATR * atr_now
    )

    # BUY
    if (
        pinbar_type == "bullish"
        and trend_up
    ):
        if previous["close"] <= ema15[p]:
            return None

        entry = candle["close"]
        sl = (
            candle["low"]
            - buffer_distance
        )

        risk = entry - sl

        if risk <= 0:
            return None

        risk_atr = (
            risk / atr_now
        )

        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):
            return None

        tp = (
            entry
            + RISK_REWARD * risk
        )

        side = "BUY"

    # SELL
    elif (
        pinbar_type == "bearish"
        and trend_down
    ):
        if previous["close"] >= ema15[p]:
            return None

        entry = candle["close"]
        sl = (
            candle["high"]
            + buffer_distance
        )

        risk = sl - entry

        if risk <= 0:
            return None

        risk_atr = (
            risk / atr_now
        )

        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):
            return None

        tp = (
            entry
            - RISK_REWARD * risk
        )

        side = "SELL"

    else:
        return None

    close_dt = (
        candle["dt"]
        + dt.timedelta(
            minutes=INTERVAL_MINUTES
        )
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
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        raise RuntimeError(
            "Missing Telegram credentials"
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = SESSION.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode":
