import os
import json
import time
import datetime as dt
import requests
from pathlib import Path

# ============================================================
# EMA 9/15 + PINBAR + 30M S/R SCANNER
# Version 4.0
#
# - Closed 15M candles for EMA + Pinbar signal
# - Closed 30M candles for Support / Resistance
# - High/Low pivots clustered separately
# - Real reaction/touch counting
# - Recency + strength scoring
# - Live price via Twelve Data /price
# - Telegram heartbeat + signal alerts
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

SYMBOLS = [
    {
        "name": "XAUUSD",
        "td_symbol": "XAU/USD",
        "price_decimals": 3,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
    },
    {
        "name": "BTCUSD",
        "td_symbol": "BTC/USD",
        "price_decimals": 3,
        "min_sl_atr": 1.0,
        "max_sl_atr": 2.5,
    },
]


# ============================================================
# TIMEFRAMES
# ============================================================

INTERVAL = "15min"
INTERVAL_MINUTES = 15

SR_INTERVAL = "30min"
SR_INTERVAL_MINUTES = 30


# ============================================================
# INDICATORS
# ============================================================

EMA_FAST = 9
EMA_SLOW = 15

ATR_PERIOD = 14

EMA_SLOPE_LOOKBACK = 3


# ============================================================
# CANDLE HISTORY
# ============================================================

CANDLES_NEEDED = 150

SR_LOOKBACK = 150


# ============================================================
# PINBAR SETTINGS
# ============================================================

PINBAR_WICK_RATIO = 2.0

PINBAR_NOSE_MAX_RATIO = 0.40

CLOSE_POSITION_LIMIT = 0.30


# ============================================================
# RISK MANAGEMENT
# ============================================================

SL_BUFFER_ATR = 0.10

RISK_REWARD = 2.0


# ============================================================
# SIGNAL TIMING
# ============================================================

MAX_SIGNAL_DELAY_MIN = 10


# ============================================================
# SUPPORT / RESISTANCE SETTINGS
# ============================================================

SR_PIVOT_LEFT = 3

SR_PIVOT_RIGHT = 3


# Pivot clustering width
# Smaller = more precise levels
SR_CLUSTER_ATR = 0.35


# Ignore S/R too close to current price
SR_MIN_DISTANCE_ATR = 0.25


# Maximum levels to show
SR_MAX_LEVELS = 3


# Minimum pivot confirmations for normal strength
SR_MIN_PIVOTS = 2


# Historical reaction/touch tolerance
SR_TOUCH_ATR = 0.18


# Minimum zone width
SR_MIN_ZONE_ATR = 0.10


# Maximum zone width
SR_MAX_ZONE_ATR = 0.50


# Recency weighting
# 48 x 30M = approximately 24 hours
RECENCY_HALF_LIFE = 48


# ============================================================
# SCANNER LOOP
# ============================================================

POLL_SECONDS = 60

HEARTBEAT_INTERVAL_MINUTES = 15


# ============================================================
# STATE FILE
# ============================================================

STATE_FILE = Path("last_signal_state.json")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

TWELVE_DATA_API_KEY = os.environ.get(
    "TWELVE_DATA_API_KEY"
)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID"
)


# ============================================================
# GLOBALS
# ============================================================

UTC = dt.timezone.utc

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "EMA-Pinbar-SR-Scanner/4.0"
})


# ============================================================
# TIME
# ============================================================

def now_utc():

    return dt.datetime.now(UTC)


# ============================================================
# DATA FETCHING
# ============================================================

def get_candles(
    td_symbol,
    interval,
    outputsize,
):

    if not TWELVE_DATA_API_KEY:

        raise RuntimeError(
            "Missing TWELVE_DATA_API_KEY"
        )

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

        raise RuntimeError(
            f"Twelve Data error: {data}"
        )

    candles = []

    for row in reversed(data["values"]):

        timestamp = dt.datetime.strptime(
            row["datetime"],
            "%Y-%m-%d %H:%M:%S",
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


# ============================================================
# LIVE PRICE
# ============================================================

def get_live_price(td_symbol):

    if not TWELVE_DATA_API_KEY:

        raise RuntimeError(
            "Missing TWELVE_DATA_API_KEY"
        )

    response = SESSION.get(
        "https://api.twelvedata.com/price",
        params={
            "symbol": td_symbol,
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=15,
    )

    response.raise_for_status()

    data = response.json()

    if "price" not in data:

        raise RuntimeError(
            f"Twelve Data price error: {data}"
        )

    return float(data["price"])


# ============================================================
# REMOVE UNCLOSED CANDLE
# ============================================================

def drop_unclosed(
    candles,
    interval_minutes,
):

    if not candles:

        return []

    current_time = now_utc()

    closed = []

    for candle in candles:

        close_time = (
            candle["dt"]
            + dt.timedelta(
                minutes=interval_minutes
            )
        )

        if close_time <= current_time:

            closed.append(candle)

    return closed


# ============================================================
# EMA
# ============================================================

def ema_series(
    values,
    period,
):

    result = [None] * len(values)

    if len(values) < period:

        return result

    k = 2 / (period + 1)

    result[period - 1] = (
        sum(values[:period])
        / period
    )

    for i in range(
        period,
        len(values),
    ):

        result[i] = (
            values[i] * k
            + result[i - 1]
            * (1 - k)
        )

    return result


# ============================================================
# ATR
# ============================================================

def atr_series(
    candles,
    period,
):

    if not candles:

        return []

    true_ranges = []

    for i, candle in enumerate(candles):

        if i == 0:

            tr = (
                candle["high"]
                - candle["low"]
            )

        else:

            previous_close = (
                candles[i - 1]["close"]
            )

            tr = max(
                candle["high"]
                - candle["low"],

                abs(
                    candle["high"]
                    - previous_close
                ),

                abs(
                    candle["low"]
                    - previous_close
                ),
            )

        true_ranges.append(tr)

    result = [None] * len(candles)

    if len(candles) < period:

        return result

    result[period - 1] = (
        sum(true_ranges[:period])
        / period
    )

    for i in range(
        period,
        len(candles),
    ):

        result[i] = (
            result[i - 1]
            * (period - 1)
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

    body_for_ratio = max(
        body,
        candle_range * 0.01,
    )

    upper_wick = (
        h - max(o, c)
    )

    lower_wick = (
        min(o, c) - l
    )

    close_position = (
        (c - l)
        / candle_range
    )


    # --------------------------------------------------------
    # BULLISH PINBAR
    # --------------------------------------------------------

    if (
        lower_wick
        >= PINBAR_WICK_RATIO
        * body_for_ratio

        and

        upper_wick
        <= PINBAR_NOSE_MAX_RATIO
        * lower_wick

        and

        close_position
        >= 1 - CLOSE_POSITION_LIMIT
    ):

        return "bullish"


    # --------------------------------------------------------
    # BEARISH PINBAR
    # --------------------------------------------------------

    if (
        upper_wick
        >= PINBAR_WICK_RATIO
        * body_for_ratio

        and

        lower_wick
        <= PINBAR_NOSE_MAX_RATIO
        * upper_wick

        and

        close_position
        <= CLOSE_POSITION_LIMIT
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
    tolerance_multiplier,
):

    tolerance = (
        atr_value
        * tolerance_multiplier
    )

    return (
        candle["low"]
        <= ema_value + tolerance

        and

        candle["high"]
        >= ema_value - tolerance
    )


# ============================================================
# S/R ATR
# ============================================================

def calculate_sr_atr(
    candles,
    period=14,
):

    if len(candles) < period + 1:

        return None

    trs = []

    for i in range(
        1,
        len(candles),
    ):

        current = candles[i]

        previous = candles[i - 1]

        tr = max(
            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            ),
        )

        trs.append(tr)

    return (
        sum(trs[-period:])
        / period
    )


# ============================================================
# FIND SWING PIVOTS
# ============================================================

def find_pivots(candles):

    left = SR_PIVOT_LEFT

    right = SR_PIVOT_RIGHT

    n = len(candles)

    highs = []

    lows = []

    for i in range(
        left,
        n - right,
    ):

        candle = candles[i]

        left_candles = (
            candles[
                i - left:i
            ]
        )

        right_candles = (
            candles[
                i + 1:i + right + 1
            ]
        )


        # ----------------------------------------------------
        # SWING LOW
        # ----------------------------------------------------

        is_low = (
            all(
                candle["low"]
                <= x["low"]
                for x in left_candles
            )

            and

            all(
                candle["low"]
                < x["low"]
                for x in right_candles
            )
        )


        # ----------------------------------------------------
        # SWING HIGH
        # ----------------------------------------------------

        is_high = (
            all(
                candle["high"]
                >= x["high"]
                for x in left_candles
            )

            and

            all(
                candle["high"]
                > x["high"]
                for x in right_candles
            )
        )


        if is_low:

            lows.append({
                "price": candle["low"],
                "idx": i,
                "time": candle["time"],
            })


        if is_high:

            highs.append({
                "price": candle["high"],
                "idx": i,
                "time": candle["time"],
            })

    return highs, lows


# ============================================================
# CLUSTER PIVOTS
# ============================================================

def cluster_pivots(
    pivots,
    tolerance,
):

    if not pivots:

        return []

    pivots = sorted(
        pivots,
        key=lambda x: x["price"],
    )

    clusters = []

    for pivot in pivots:

        if not clusters:

            clusters.append(
                [pivot]
            )

            continue

        cluster = clusters[-1]

        center = (
            sum(
                x["price"]
                for x in cluster
            )
            / len(cluster)
        )

        if (
            abs(
                pivot["price"]
                - center
            )
            <= tolerance
        ):

            cluster.append(
                pivot
            )

        else:

            clusters.append(
                [pivot]
            )

    return clusters


# ============================================================
# CHECK CANDLE TOUCH
# ============================================================

def candle_touches_zone(
    candle,
    zone_low,
    zone_high,
    touch_tolerance,
):

    expanded_low = (
        zone_low
        - touch_tolerance
    )

    expanded_high = (
        zone_high
        + touch_tolerance
    )

    return (
        candle["high"]
        >= expanded_low

        and

        candle["low"]
        <= expanded_high
    )


# ============================================================
# BUILD S/R LEVEL
# ============================================================

def build_level(
    cluster,
    candles,
    atr,
    current_price,
    side,
):

    prices = [
        x["price"]
        for x in cluster
    ]

    center = (
        sum(prices)
        / len(prices)
    )

    raw_low = min(prices)

    raw_high = max(prices)


    # --------------------------------------------------------
    # ZONE WIDTH
    # --------------------------------------------------------

    zone_half_width = min(

        max(
            atr
            * SR_MIN_ZONE_ATR,

            (raw_high - raw_low)
            / 2,
        ),

        atr
        * SR_MAX_ZONE_ATR,
    )

    zone_low = (
        center
        - zone_half_width
    )

    zone_high = (
        center
        + zone_half_width
    )


    # --------------------------------------------------------
    # REACTION COUNT
    # --------------------------------------------------------

    touch_tolerance = (
        atr
        * SR_TOUCH_ATR
    )

    start_idx = max(
        0,
        min(
            x["idx"]
            for x in cluster
        ) - 2,
    )


    raw_touch_count = 0

    separated_reactions = 0

    in_reaction = False


    for candle in candles[start_idx:]:

        touched = (
            candle_touches_zone(
                candle,
                zone_low,
                zone_high,
                touch_tolerance,
            )
        )


        if touched:

            raw_touch_count += 1


        # Count separated reactions,
        # not every consecutive candle.
        if (
            touched
            and not in_reaction
        ):

            separated_reactions += 1


        in_reaction = touched


    # --------------------------------------------------------
    # RECENCY
    # --------------------------------------------------------

    latest = max(
        cluster,
        key=lambda x: x["idx"],
    )

    age_bars = max(
        0,
        len(candles)
        - 1
        - latest["idx"],
    )

    recency = (
        2
        ** (
            -age_bars
            / RECENCY_HALF_LIFE
        )
    )


    # --------------------------------------------------------
    # STRENGTH SCORE
    # --------------------------------------------------------

    pivot_count = len(cluster)

    score = (
        pivot_count * 2.0

        +

        min(
            separated_reactions,
            8,
        ) * 0.75

        +

        recency * 2.0
    )


    # --------------------------------------------------------
    # STRENGTH LABEL
    # --------------------------------------------------------

    if (
        pivot_count >= 3

        and

        separated_reactions >= 3

        and

        recency >= 0.50
    ):

        strength = "STRONG"

    elif (
        pivot_count >= 2

        or

        separated_reactions >= 2
    ):

        strength = "MEDIUM"

    else:

        strength = "WEAK"


    # --------------------------------------------------------
    # DISTANCE
    # --------------------------------------------------------

    if side == "support":

        distance = (
            current_price
            - center
        )

    else:

        distance = (
            center
            - current_price
        )


    return {

        "price": center,

        "zone_low": zone_low,

        "zone_high": zone_high,

        "pivot_confirmations":
            pivot_count,

        "reaction_touches":
            separated_reactions,

        "raw_touches":
            raw_touch_count,

        "time":
            latest["time"],

        "age_bars":
            age_bars,

        "recency":
            recency,

        "score":
            score,

        "strength":
            strength,

        "distance":
            distance,

        "side":
            side,
    }


# ============================================================
# IDENTIFY SUPPORT / RESISTANCE
# ============================================================

def identify_sr_levels(
    candles,
    current_price,
):

    candles = candles[
        -SR_LOOKBACK:
    ]


    empty_result = {

        "support": [],

        "resistance": [],

        "nearest_support": None,

        "nearest_resistance": None,

        "atr": None,
    }


    if len(candles) < 30:

        return empty_result


    atr = calculate_sr_atr(
        candles
    )


    if (
        atr is None
        or atr <= 0
    ):

        return empty_result


    highs, lows = find_pivots(
        candles
    )


    cluster_tolerance = (
        atr
        * SR_CLUSTER_ATR
    )

    min_distance = (
        atr
        * SR_MIN_DISTANCE_ATR
    )


    # --------------------------------------------------------
    # IMPORTANT:
    # HIGH AND LOW ARE CLUSTERED SEPARATELY
    # --------------------------------------------------------

    high_clusters = cluster_pivots(
        highs,
        cluster_tolerance,
    )

    low_clusters = cluster_pivots(
        lows,
        cluster_tolerance,
    )


    support_levels = []

    resistance_levels = []


    # --------------------------------------------------------
    # SUPPORT
    # --------------------------------------------------------

    for cluster in low_clusters:

        level = build_level(
            cluster,
            candles,
            atr,
            current_price,
            "support",
        )


        if (
            level["price"]
            <
            current_price
            - min_distance
        ):

            support_levels.append(
                level
            )


    # --------------------------------------------------------
    # RESISTANCE
    # --------------------------------------------------------

    for cluster in high_clusters:

        level = build_level(
            cluster,
            candles,
            atr,
            current_price,
            "resistance",
        )


        if (
            level["price"]
            >
            current_price
            + min_distance
        ):

            resistance_levels.append(
                level
            )


    # --------------------------------------------------------
    # SORT BY NEAREST DISTANCE
    # --------------------------------------------------------

    support_levels.sort(
        key=lambda x: (
            x["distance"],
            -x["score"],
        )
    )


    resistance_levels.sort(
        key=lambda x: (
            x["distance"],
            -x["score"],
        )
    )


    support_levels = (
        support_levels[
            :SR_MAX_LEVELS
        ]
    )

    resistance_levels = (
        resistance_levels[
            :SR_MAX_LEVELS
        ]
    )


    return {

        "support":
            support_levels,

        "resistance":
            resistance_levels,

        "nearest_support":
            (
                support_levels[0]
                if support_levels
                else None
            ),

        "nearest_resistance":
            (
                resistance_levels[0]
                if resistance_levels
                else None
            ),

        "atr":
            atr,
    }


# ============================================================
# SIGNAL ENGINE
# ============================================================

def check_signal(
    candles,
    config,
):

    minimum_candles = (

        max(
            ATR_PERIOD,
            EMA_SLOW,
        )

        +

        EMA_SLOPE_LOOKBACK

        +

        2
    )


    if len(candles) < minimum_candles:

        return None


    closes = [
        c["close"]
        for c in candles
    ]


    ema9 = ema_series(
        closes,
        EMA_FAST,
    )

    ema15 = ema_series(
        closes,
        EMA_SLOW,
    )

    atr = atr_series(
        candles,
        ATR_PERIOD,
    )


    i = len(candles) - 1

    p = i - 1

    j = (
        i
        - EMA_SLOPE_LOOKBACK
    )


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


    # --------------------------------------------------------
    # PINBAR
    # --------------------------------------------------------

    pinbar_type = (
        classify_pinbar(
            candle
        )
    )


    if pinbar_type is None:

        return None


    # --------------------------------------------------------
    # EMA15 TOUCH
    # --------------------------------------------------------

    if not touches_ema(
        candle,
        ema15[i],
        atr_now,
        0.15,
    ):

        return None


    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    trend_up = (

        ema9[i]
        > ema15[i]

        and

        ema15[i]
        > ema15[j]

        and

        ema9[i]
        > ema9[j]
    )


    trend_down = (

        ema9[i]
        < ema15[i]

        and

        ema15[i]
        < ema15[j]

        and

        ema9[i]
        < ema9[j]
    )


    buffer_distance = (
        SL_BUFFER_ATR
        * atr_now
    )


    # ========================================================
    # BUY
    # ========================================================

    if (
        pinbar_type == "bullish"
        and trend_up
    ):

        if (
            previous["close"]
            <= ema15[p]
        ):

            return None


        entry = candle["close"]

        sl = (
            candle["low"]
            - buffer_distance
        )


        risk = (
            entry
            - sl
        )


        if risk <= 0:

            return None


        risk_atr = (
            risk
            / atr_now
        )


        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):

            return None


        tp = (
            entry
            + RISK_REWARD
            * risk
        )


        side = "BUY"


    # ========================================================
    # SELL
    # ========================================================

    elif (
        pinbar_type == "bearish"
        and trend_down
    ):

        if (
            previous["close"]
            >= ema15[p]
        ):

            return None


        entry = candle["close"]

        sl = (
            candle["high"]
            + buffer_distance
        )


        risk = (
            sl
            - entry
        )


        if risk <= 0:

            return None


        risk_atr = (
            risk
            / atr_now
        )


        if not (
            config["min_sl_atr"]
            <= risk_atr
            <= config["max_sl_atr"]
        ):

            return None


        tp = (
            entry
            - RISK_REWARD
            * risk
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

        "side":
            side,

        "entry":
            entry,

        "sl":
            sl,

        "tp":
            tp,

        "risk":
            risk,

        "risk_atr":
            risk_atr,

        "time":
            candle["time"],

        "close_dt":
            close_dt,

        "candle":
            candle,

        "ema9":
            ema9[i],

        "ema15":
            ema15[i],

        "atr":
            atr_now,
    }


# ============================================================
# FORMAT HELPERS
# ============================================================

def fmt(
    value,
    decimals=3,
):

    return (
        f"{value:.{decimals}f}"
    )


def strength_icon(
    strength,
):

    return {

        "STRONG":
            "🟢",

        "MEDIUM":
            "🟡",

        "WEAK":
            "⚪",

    }.get(
        strength,
        "⚪",
    )


def level_text(
    level,
    decimals,
):

    icon = strength_icon(
        level["strength"]
    )

    return (

        f"{icon} "

        f"{fmt(level['price'], decimals)} "

        f"[{level['strength']}]"

        f" | pivots "
        f"{level['pivot_confirmations']}"

        f" | reactions "
        f"{level['reaction_touches']}"

        f" | dist "
        f"{fmt(level['distance'], decimals)}"
    )


# ============================================================
# S/R TELEGRAM FORMAT
# ============================================================

def format_sr_message(
    sr,
    current_price,
    decimals,
):

    lines = [

        "\n📊 "
        "<b>30M SUPPORT / RESISTANCE</b>"
    ]


    support = (
        sr["nearest_support"]
    )

    resistance = (
        sr["nearest_resistance"]
    )


    # --------------------------------------------------------
    # SUPPORT
    # --------------------------------------------------------

    if support:

        lines.extend([

            (
                f"🟢 <b>Support:</b> "
                f"{fmt(support['price'], decimals)} "
                f"[{support['strength']}]"
            ),

            (
                f"Zone: "
                f"{fmt(support['zone_low'], decimals)}"
                f" - "
                f"{fmt(support['zone_high'], decimals)}"
            ),

            (
                f"Pivot confirmations: "
                f"{support['pivot_confirmations']}"
            ),

            (
                f"Reaction touches: "
                f"{support['reaction_touches']}"
            ),

            (
                f"Distance: "
                f"{fmt(support['distance'], decimals)}"
            ),
        ])

    else:

        lines.append(
            "🟢 Support: পাওয়া যায়নি"
        )


    # --------------------------------------------------------
    # RESISTANCE
    # --------------------------------------------------------

    if resistance:

        lines.extend([

            (
                f"\n🔴 <b>Resistance:</b> "
                f"{fmt(resistance['price'], decimals)} "
                f"[{resistance['strength']}]"
            ),

            (
                f"Zone: "
                f"{fmt(resistance['zone_low'], decimals)}"
                f" - "
                f"{fmt(resistance['zone_high'], decimals)}"
            ),

            (
                f"Pivot confirmations: "
                f"{resistance['pivot_confirmations']}"
            ),

            (
                f"Reaction touches: "
                f"{resistance['reaction_touches']}"
            ),

            (
                f"Distance: "
                f"{fmt(resistance['distance'], decimals)}"
            ),
        ])

    else:

        lines.append(
            "\n🔴 Resistance: পাওয়া যায়নি"
        )


    # --------------------------------------------------------
    # OTHER SUPPORTS
    # --------------------------------------------------------

    if len(sr["support"]) > 1:

        lines.append(
            "\n<b>Other Supports</b>"
        )

        for level in (
            sr["support"][1:]
        ):

            lines.append(
                "• "
                + level_text(
                    level,
                    decimals,
                )
            )


    # --------------------------------------------------------
    # OTHER RESISTANCES
    # --------------------------------------------------------

    if len(sr["resistance"]) > 1:

        lines.append(
            "\n<b>Other Resistances</b>"
        )

        for level in (
            sr["resistance"][1:]
        ):

            lines.append(
                "• "
                + level_text(
                    level,
                    decimals,
                )
            )


    lines.append(
        "\n<i>"
        "30M levels use closed candles only."
        "</i>"
    )


    return "\n".join(lines)


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(
    message,
):

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN "
            "or TELEGRAM_CHAT_ID"
        )


    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        f"/sendMessage"
    )


    response = SESSION.post(

        url,

        data={

            "chat_id":
                TELEGRAM_CHAT_ID,

            "text":
                message,

            "parse_mode":
                "HTML",

            "disable_web_page_preview":
                True,
        },

        timeout=20,
    )


    response.raise_for_status()

    result = response.json()


    if not result.get("ok"):

        raise RuntimeError(
            "Telegram rejected message: "
            f"{result}"
        )


    return True


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return {}


    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)


    except (
        json.JSONDecodeError,
        OSError,
    ):

        return {}


# ============================================================
# SAVE STATE
# ============================================================

def save_state(
    state,
):

    temp_file = (
        STATE_FILE.with_suffix(
            ".tmp"
        )
    )


    with temp_file.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False,
        )


    temp_file.replace(
        STATE_FILE
    )


# ============================================================
# HEARTBEAT CHECK
# ============================================================

def should_send_heartbeat(
    state,
):

    last = state.get(
        "_last_heartbeat"
    )


    if not last:

        return True


    try:

        last_time = (
            dt.datetime.fromisoformat(
                last
            )
        )

    except ValueError:

        return True


    if last_time.tzinfo is None:

        last_time = (
            last_time.replace(
                tzinfo=UTC
            )
        )


    elapsed = (

        now_utc()
        - last_time

    ).total_seconds() / 60


    return (
        elapsed
        >= HEARTBEAT_INTERVAL_MINUTES
    )


# ============================================================
# BUILD HEARTBEAT
# ============================================================

def build_heartbeat(
    market_data,
):

    stamp = now_utc().strftime(
        "%Y-%m-%d %H:%M UTC"
    )


    lines = [

        "✅ "
        "<b>বট চালু আছে — V4.0</b>",

        f"সময়: {stamp}",
    ]


    for name, data in (
        market_data.items()
    ):

        lines.append(
            f"\n<b>{name}</b>"
        )


        if data is None:

            lines.append(
                "ডেটা আনতে ব্যর্থ"
            )

            continue


        decimals = (
            data["decimals"]
        )

        price = (
            data["price"]
        )

        sr = (
            data["sr"]
        )


        lines.append(
            f"Price: "
            f"{fmt(price, decimals)}"
        )


        support = (
            sr["nearest_support"]
        )

        resistance = (
            sr["nearest_resistance"]
        )


        if support:

            lines.append(

                f"🟢 Support: "
                f"{fmt(support['price'], decimals)} "
                f"[{support['strength']}] "

                f"({support['pivot_confirmations']}"
                f" pivots / "

                f"{support['reaction_touches']}"
                f" reactions)"
            )

        else:

            lines.append(
                "🟢 Support: পাওয়া যায়নি"
            )


        if resistance:

            lines.append(

                f"🔴 Resistance: "
                f"{fmt(resistance['price'], decimals)} "
                f"[{resistance['strength']}] "

                f"({resistance['pivot_confirmations']}"
                f" pivots / "

                f"{resistance['reaction_touches']}"
                f" reactions)"
            )

        else:

            lines.append(
                "🔴 Resistance: পাওয়া যায়নি"
            )


    return "\n".join(lines)


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    name,
    signal,
    sr,
    live_price,
    decimals,
):

    side = signal["side"]


    if side == "BUY":

        title = (
            "🟢 <b>BUY SIGNAL</b>"
        )

    else:

        title = (
            "🔴 <b>SELL SIGNAL</b>"
        )


    lines = [

        f"🚨 <b>{name}</b>",

        title,

        "",

        (
            f"Entry: "
            f"<b>{fmt(signal['entry'], decimals)}</b>"
        ),

        (
            f"SL: "
            f"<b>{fmt(signal['sl'], decimals)}</b>"
        ),

        (
            f"TP: "
            f"<b>{fmt(signal['tp'], decimals)}</b>"
        ),

        (
            f"Risk: "
            f"{fmt(signal['risk'], decimals)}"
        ),

        (
            f"Risk/ATR: "
            f"{signal['risk_atr']:.2f}"
        ),

        (
            f"RR: "
            f"1:{RISK_REWARD:.1f}"
        ),

        "",

        (
            f"Live price: "
            f"{fmt(live_price, decimals)}"
        ),

        (
            f"EMA9: "
            f"{fmt(signal['ema9'], decimals)}"
        ),

        (
            f"EMA15: "
            f"{fmt(signal['ema15'], decimals)}"
        ),

        (
            f"ATR: "
            f"{fmt(signal['atr'], decimals)}"
        ),

        (
            f"Pinbar: "
            f"{signal['side']}"
        ),

        (
            f"Candle: "
            f"{signal['time']} UTC"
        ),
    ]


    support = (
        sr["nearest_support"]
    )

    resistance = (
        sr["nearest_resistance"]
    )


    lines.append("")


    if support:

        lines.append(

            f"🟢 Support: "
            f"{fmt(support['price'], decimals)} "
            f"[{support['strength']}]"
        )


    if resistance:

        lines.append(

            f"🔴 Resistance: "
            f"{fmt(resistance['price'], decimals)} "
            f"[{resistance['strength']}]"
        )


    lines.append(

        "\n<i>"
        "Signal uses closed 15M candle."
        "</i>"
    )


    return "\n".join(lines)


# ============================================================
# ONE COMPLETE SCAN
# ============================================================

def scan_once(
    state,
):

    market_data = {}


    for config in SYMBOLS:

        name = (
            config["name"]
        )

        decimals = (
            config["price_decimals"]
        )


        try:

            # =================================================
            # 15M DATA
            # =================================================

            raw_15m = get_candles(

                config["td_symbol"],

                INTERVAL,

                CANDLES_NEEDED,
            )


            candles_15m = (
                drop_unclosed(
                    raw_15m,
                    INTERVAL_MINUTES,
                )
            )


            if len(candles_15m) < 30:

                raise RuntimeError(
                    "Not enough closed "
                    "15M candles"
                )


            # =================================================
            # 30M DATA
            # =================================================

            raw_30m = get_candles(

                config["td_symbol"],

                SR_INTERVAL,

                SR_LOOKBACK + 20,
            )


            candles_30m = (
                drop_unclosed(
                    raw_30m,
                    SR_INTERVAL_MINUTES,
                )
            )


            # =================================================
            # LIVE PRICE
            # =================================================

            try:

                live_price = (
                    get_live_price(
                        config[
                            "td_symbol"
                        ]
                    )
                )

            except Exception as live_error:

                print(

                    f"[{name}] "
                    f"Live price failed: "
                    f"{live_error}"
                )


                # Fallback
                live_price = (
                    candles_15m[-1]["close"]
                )


            # =================================================
            # S/R
            # =================================================

            sr = identify_sr_levels(

                candles_30m,

                live_price,
            )


            market_data[name] = {

                "price":
                    live_price,

                "sr":
                    sr,

                "decimals":
                    decimals,
            }


            print(

                f"[{name}] Live: "
                f"{fmt(live_price, decimals)}"
            )


            # =================================================
            # PRINT SUPPORT
            # =================================================

            if sr["nearest_support"]:

                s = (
                    sr["nearest_support"]
                )


                print(

                    f"[{name}] S: "

                    f"{fmt(s['price'], decimals)} "

                    f"[{s['strength']}] "

                    f"pivots="
                    f"{s['pivot_confirmations']} "

                    f"reactions="
                    f"{s['reaction_touches']}"
                )


            # =================================================
            # PRINT RESISTANCE
            # =================================================

            if sr["nearest_resistance"]:

                r = (
                    sr["nearest_resistance"]
                )


                print(

                    f"[{name}] R: "

                    f"{fmt(r['price'], decimals)} "

                    f"[{r['strength']}] "

                    f"pivots="
                    f"{r['pivot_confirmations']} "

                    f"reactions="
                    f"{r['reaction_touches']}"
                )


            # =================================================
            # SIGNAL
            # =================================================

            signal = check_signal(

                candles_15m,

                config,
            )


            if not signal:

                print(
                    f"[{name}] No signal"
                )

                continue


            # =================================================
            # SIGNAL DELAY
            # =================================================

            delay_min = (

                now_utc()
                - signal["close_dt"]

            ).total_seconds() / 60


            if delay_min < 0:

                print(

                    f"[{name}] "
                    f"Candle close is "
                    f"in future"
                )

                continue


            if (
                delay_min
                > MAX_SIGNAL_DELAY_MIN
            ):

                print(

                    f"[{name}] "
                    f"Stale signal skipped "
                    f"({delay_min:.1f} min)"
                )

                continue


            # =================================================
            # DUPLICATE PROTECTION
            # =================================================

            signal_key = (

                f"{name}|"

                f"{signal['time']}|"

                f"{signal['side']}"
            )


            sent_signals = (
                state.setdefault(
                    "sent_signals",
                    []
                )
            )


            if signal_key in sent_signals:

                print(

                    f"[{name}] "
                    f"Signal already sent: "
                    f"{signal_key}"
                )

                continue


            # =================================================
            # SEND SIGNAL
            # =================================================

            message = (
                build_signal_message(

                    name,

                    signal,

                    sr,

                    live_price,

                    decimals,
                )
            )


            send_telegram(
                message
            )


            sent_signals.append(
                signal_key
            )


            # Keep state small
            state["sent_signals"] = (
                sent_signals[-100:]
            )


            print(

                f"[{name}] "
                f"ALERT SENT: "
                f"{signal['side']} "
                f"{signal['time']}"
            )


        except Exception as exc:

            print(

                f"[{name}] ERROR: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )


            market_data[name] = None


    # =========================================================
    # HEARTBEAT
    # =========================================================

    if should_send_heartbeat(
        state
    ):

        try:

            heartbeat = (
                build_heartbeat(
                    market_data
                )
            )


            send_telegram(
                heartbeat
            )


            state[
                "_last_heartbeat"
            ] = (
                now_utc()
                .isoformat()
            )


            print(
                "Heartbeat sent."
            )


        except Exception as exc:

            print(

                f"[HEARTBEAT ERROR] "
                f"{type(exc).__name__}: "
                f"{exc}"
            )


    # =========================================================
    # SAVE STATE
    # =========================================================

    save_state(
        state
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    # --------------------------------------------------------
    # CHECK API KEY
    # --------------------------------------------------------

    if not TWELVE_DATA_API_KEY:

        raise RuntimeError(

            "Set "
            "TWELVE_DATA_API_KEY "
            "environment variable."
        )


    # --------------------------------------------------------
    # CHECK TELEGRAM TOKEN
    # --------------------------------------------------------

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(

            "Set "
            "TELEGRAM_BOT_TOKEN "
            "environment variable."
        )


    # --------------------------------------------------------
    # CHECK TELEGRAM CHAT ID
    # --------------------------------------------------------

    if not TELEGRAM_CHAT_ID:

        raise RuntimeError(

            "Set "
            "TELEGRAM_CHAT_ID "
            "environment variable."
        )


    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    print(
        "=" * 60
    )

    print(
        "EMA 9/15 + PINBAR "
        "+ 30M S/R SCANNER V4.0"
    )

    print(
        "Scanner started."
    )

    print(
        "=" * 60
    )


    state = (
        load_state()
    )


    # ========================================================
    # CONTINUOUS LOOP
    # ========================================================

    while True:

        started = time.time()


        try:

            scan_once(
                state
            )


        except Exception as exc:

            print(

                f"[MAIN ERROR] "
                f"{type(exc).__name__}: "
                f"{exc}"
            )


        elapsed = (
            time.time()
            - started
        )


        sleep_for = max(

            5,

            POLL_SECONDS
            - elapsed
        )


        print(

            f"Next scan in "
            f"{sleep_for:.0f} seconds..."
        )


        try:

            time.sleep(
                sleep_for
            )


        except KeyboardInterrupt:

            print(
                "Scanner stopped."
            )

            break


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
