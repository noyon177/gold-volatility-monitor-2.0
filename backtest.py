
import os
import time
import requests
import pandas as pd
import datetime as dt
from pathlib import Path

# ============================================================
# EMA 9/15 + PINBAR BACKTEST V3
# Trend Strength + Market Structure + RR Comparison
# ============================================================

API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

SYMBOLS = {
    "XAUUSD": "XAU/USD",
    "BTCUSD": "BTC/USD",
}

INTERVAL = "15min"
INTERVAL_MINUTES = 15
DAYS = 90

EMA_FAST = 9
EMA_SLOW = 15
ATR_PERIOD = 14
EMA_SLOPE_LOOKBACK = 3

PINBAR_WICK_RATIO = 2.0
PINBAR_NOSE_MAX_RATIO = 0.40
CLOSE_POSITION_LIMIT = 0.30

SL_BUFFER_ATR = 0.10
TOUCH_ATR = 0.15

MIN_SL_ATR = 1.0
MAX_SL_ATR = 2.5

# New trend strength filters
MIN_EMA_SEPARATION_ATR = 0.10
MIN_EMA_SLOPE_ATR = 0.05

# Market structure windows
STRUCTURE_LOOKBACK = 5

# Test configurations
TESTS = [
    {
        "name": "BASELINE_RR1.5",
        "trend_filter": False,
        "structure_filter": False,
        "rr": 1.5,
    },
    {
        "name": "BASELINE_RR2.0",
        "trend_filter": False,
        "structure_filter": False,
        "rr": 2.0,
    },
    {
        "name": "TREND_FILTER_RR1.5",
        "trend_filter": True,
        "structure_filter": False,
        "rr": 1.5,
    },
    {
        "name": "TREND_FILTER_RR2.0",
        "trend_filter": True,
        "structure_filter": False,
        "rr": 2.0,
    },
    {
        "name": "STRUCTURE_RR1.5",
        "trend_filter": False,
        "structure_filter": True,
        "rr": 1.5,
    },
    {
        "name": "COMBINED_RR1.5",
        "trend_filter": True,
        "structure_filter": True,
        "rr": 1.5,
    },
    {
        "name": "COMBINED_RR2.0",
        "trend_filter": True,
        "structure_filter": True,
        "rr": 2.0,
    },
]

OUTPUT = Path("backtest_results_v3")
OUTPUT.mkdir(exist_ok=True)

UTC = dt.timezone.utc
SESSION = requests.Session()


# ============================================================
# DOWNLOAD HISTORICAL DATA
# ============================================================

def download_data(symbol, days):

    if not API_KEY:
        raise RuntimeError("Missing TWELVE_DATA_API_KEY")

    end = dt.datetime.now(UTC).replace(
        second=0,
        microsecond=0
    )

    start = end - dt.timedelta(days=days)

    all_rows = []
    cursor = start

    while cursor < end:

        chunk_end = min(
            cursor + dt.timedelta(days=25),
            end
        )

        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "start_date": cursor.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "UTC",
            "outputsize": 5000,
            "apikey": API_KEY,
        }

        response = SESSION.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=30
        )

        response.raise_for_status()
        data = response.json()

        if "values" not in data:
            raise RuntimeError(
                f"API error: {data}"
            )

        all_rows.extend(data["values"])

        print(
            f"{symbol}: downloaded through "
            f"{chunk_end.strftime('%Y-%m-%d')}"
        )

        cursor = chunk_end
        time.sleep(1)

    df = pd.DataFrame(all_rows)

    if df.empty:
        raise RuntimeError("No historical data received")

    df["dt"] = pd.to_datetime(
        df["datetime"],
        utc=True
    )

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna(
        subset=["dt", "open", "high", "low", "close"]
    )

    df = df.drop_duplicates(
        subset=["dt"]
    )

    df = df.sort_values("dt").reset_index(drop=True)

    now = pd.Timestamp.now(tz="UTC")

    df = df[
        df["dt"] + pd.Timedelta(minutes=INTERVAL_MINUTES)
        <= now
    ].reset_index(drop=True)

    return df


# ============================================================
# INDICATORS - MATCH LIVE SCANNER CALCULATION
# ============================================================

def ema_series(values, period):

    result = [None] * len(values)

    if len(values) < period:
        return result

    k = 2 / (period + 1)

    result[period - 1] = sum(
        values[:period]
    ) / period

    for i in range(period, len(values)):

        result[i] = (
            values[i] * k
            + result[i - 1] * (1 - k)
        )

    return result


def atr_series(df, period):

    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()

    tr = []

    for i in range(len(df)):

        if i == 0:
            value = highs[i] - lows[i]

        else:
            value = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )

        tr.append(value)

    result = [None] * len(df)

    if len(df) < period:
        return result

    result[period - 1] = sum(
        tr[:period]
    ) / period

    for i in range(period, len(df)):

        result[i] = (
            result[i - 1] * (period - 1)
            + tr[i]
        ) / period

    return result


def add_indicators(df):

    df = df.copy()

    closes = df["close"].tolist()

    df["ema9"] = ema_series(
        closes,
        EMA_FAST
    )

    df["ema15"] = ema_series(
        closes,
        EMA_SLOW
    )

    df["atr"] = atr_series(
        df,
        ATR_PERIOD
    )

    return df


# ============================================================
# PINBAR DETECTION
# ============================================================

def classify_pinbar(row):

    o = row["open"]
    h = row["high"]
    l = row["low"]
    c = row["close"]

    candle_range = h - l

    if candle_range <= 0:
        return None

    body = abs(c - o)

    body_for_ratio = max(
        body,
        candle_range * 0.01
    )

    upper = h - max(o, c)
    lower = min(o, c) - l

    close_position = (c - l) / candle_range

    if (
        lower >= PINBAR_WICK_RATIO * body_for_ratio
        and upper <= PINBAR_NOSE_MAX_RATIO * lower
        and close_position >= 1 - CLOSE_POSITION_LIMIT
    ):
        return "BUY"

    if (
        upper >= PINBAR_WICK_RATIO * body_for_ratio
        and lower <= PINBAR_NOSE_MAX_RATIO * upper
        and close_position <= CLOSE_POSITION_LIMIT
    ):
        return "SELL"

    return None


# ============================================================
# MARKET STRUCTURE FILTER
# ============================================================

def structure_confirmation(df, i, side):

    n = STRUCTURE_LOOKBACK

    if i < 2 * n:
        return False

    # Both windows exclude the current signal candle.
    previous = df.iloc[i - 2*n:i - n]
    recent = df.iloc[i - n:i]

    previous_high = previous["high"].max()
    previous_low = previous["low"].min()

    recent_high = recent["high"].max()
    recent_low = recent["low"].min()

    if side == "BUY":

        return (
            recent_high > previous_high
            and recent_low > previous_low
        )

    if side == "SELL":

        return (
            recent_high < previous_high
            and recent_low < previous_low
        )

    return False


# ============================================================
# SIGNAL ENGINE
# ============================================================

def get_signal(df, i, config):

    row = df.iloc[i]
    prev = df.iloc[i - 1]
    slope = df.iloc[i - EMA_SLOPE_LOOKBACK]

    atr = row["atr"]

    if pd.isna(atr) or atr <= 0:
        return None

    pinbar = classify_pinbar(row)

    if pinbar is None:
        return None

    # EMA15 touch
    ema_touch = (
        row["low"] <= row["ema15"] + TOUCH_ATR * atr
        and row["high"] >= row["ema15"] - TOUCH_ATR * atr
    )

    if not ema_touch:
        return None

    trend_up = (
        row["ema9"] > row["ema15"]
        and row["ema15"] > slope["ema15"]
        and row["ema9"] > slope["ema9"]
    )

    trend_down = (
        row["ema9"] < row["ema15"]
        and row["ema15"] < slope["ema15"]
        and row["ema9"] < slope["ema9"]
    )

    # New trend strength filter
    ema_separation = abs(
        row["ema9"] - row["ema15"]
    ) / atr

    ema_slope = (
        row["ema15"] - slope["ema15"]
    ) / atr

    if config["trend_filter"]:

        if ema_separation < MIN_EMA_SEPARATION_ATR:
            return None

        if pinbar == "BUY":

            if ema_slope < MIN_EMA_SLOPE_ATR:
                return None

        elif pinbar == "SELL":

            if ema_slope > -MIN_EMA_SLOPE_ATR:
                return None

    # New market structure filter
    if config["structure_filter"]:

        if not structure_confirmation(
            df,
            i,
            pinbar
        ):
            return None

    entry = row["close"]
    buffer = SL_BUFFER_ATR * atr

    if pinbar == "BUY" and trend_up:

        if prev["close"] <= prev["ema15"]:
            return None

        sl = row["low"] - buffer
        risk = entry - sl

    elif pinbar == "SELL" and trend_down:

        if prev["close"] >= prev["ema15"]:
            return None

        sl = row["high"] + buffer
        risk = sl - entry

    else:
        return None

    if risk <= 0:
        return None

    risk_atr = risk / atr

    if not MIN_SL_ATR <= risk_atr <= MAX_SL_ATR:
        return None

    rr = config["rr"]

    if pinbar == "BUY":
        tp = entry + rr * risk
    else:
        tp = entry - rr * risk

    return {
        "side": pinbar,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
        "rr": rr,
        "signal_time": row["dt"],
        "ema9": row["ema9"],
        "ema15": row["ema15"],
        "atr": atr,
    }


# ============================================================
# TRADE SIMULATION
# ============================================================

def simulate_trade(df, signal, start_index):

    side = signal["side"]
    sl = signal["sl"]
    tp = signal["tp"]

    for j in range(start_index, len(df)):

        candle = df.iloc[j]

        if side == "BUY":

            hit_sl = candle["low"] <= sl
            hit_tp = candle["high"] >= tp

        else:

            hit_sl = candle["high"] >= sl
            hit_tp = candle["low"] <= tp

        # Conservative same-candle assumption
        if hit_sl:

            return {
                "result": "LOSS",
                "exit": sl,
                "exit_time": candle["dt"],
                "R": -1.0,
                "exit_index": j,
            }

        if hit_tp:

            return {
                "result": "WIN",
                "exit": tp,
                "exit_time": candle["dt"],
                "R": signal["rr"],
                "exit_index": j,
            }

    return {
        "result": "OPEN",
        "exit": None,
        "exit_time": None,
        "R": 0.0,
        "exit_index": len(df) - 1,
    }


# ============================================================
# BACKTEST ENGINE
# ============================================================

def run_backtest(df, symbol, config):

    trades = []

    warmup = max(
        ATR_PERIOD,
        EMA_SLOW,
        STRUCTURE_LOOKBACK * 2
    ) + EMA_SLOPE_LOOKBACK + 2

    i = warmup

    while i < len(df) - 1:

        signal = get_signal(
            df,
            i,
            config
        )

        if signal is None:
            i += 1
            continue

        outcome = simulate_trade(
            df,
            signal,
            i + 1
        )

        trade = {
            "symbol": symbol,
            "test": config["name"],
            "side": signal["side"],
            "signal_time": signal["signal_time"],
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp": signal["tp"],
            "risk": signal["risk"],
            "risk_atr": signal["risk_atr"],
            "rr": signal["rr"],
            "ema9": signal["ema9"],
            "ema15": signal["ema15"],
            "atr": signal["atr"],
            "result": outcome["result"],
            "exit": outcome["exit"],
            "exit_time": outcome["exit_time"],
            "R": outcome["R"],
        }

        trades.append(trade)

        # No overlapping trades within this test
        if outcome["result"] == "OPEN":
            break

        i = outcome["exit_index"] + 1

    return pd.DataFrame(trades)


# ============================================================
# PERFORMANCE REPORT
# ============================================================

def report(trades, symbol, test_name):

    if trades.empty:
        print(f"{symbol} | {test_name}: No trades")
        return None

    closed = trades[
        trades["result"].isin(["WIN", "LOSS"])
    ].copy()

    if closed.empty:
        print(f"{symbol} | {test_name}: No closed trades")
        return None

    wins = (closed["result"] == "WIN").sum()
    losses = (closed["result"] == "LOSS").sum()

    total = len(closed)

    win_rate = wins / total * 100

    gross_profit = closed.loc[
        closed["R"] > 0, "R"
    ].sum()

    gross_loss = abs(
        closed.loc[closed["R"] < 0, "R"].sum()
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf")
    )

    net_result = closed["R"].sum()

    # Include initial equity = 0 in drawdown calculation
    equity = [0.0]

    for value in closed["R"]:
        equity.append(equity[-1] + value)

    peak = equity[0]
    max_dd = 0.0

    for value in equity:

        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    consecutive = 0
    max_consecutive = 0

    for result in closed["result"]:

        if result == "LOSS":
            consecutive += 1
            max_consecutive = max(
                max_consecutive,
                consecutive
            )
        else:
            consecutive = 0

    summary = {
        "symbol": symbol,
        "test": test_name,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3),
        "net_R": round(net_result, 2),
        "max_drawdown_R": round(max_dd, 2),
        "max_consecutive_losses": max_consecutive,
    }

    print("\n" + "=" * 55)
    print(f"BACKTEST V3 — {symbol} — {test_name}")
    print("=" * 55)

    for key, value in summary.items():
        if key not in ("symbol", "test"):
            print(f"{key:28}: {value}")

    print("=" * 55)

    return summary


# ============================================================
# MAIN
# ============================================================

def main():

    all_summaries = []
    all_trades = []

    for name, td_symbol in SYMBOLS.items():

        print(f"\nDownloading {name}...")

        try:
            candles = download_data(
                td_symbol,
                DAYS
            )

            print(f"Received {len(candles)} candles")

            if len(candles) < 200:
                print("Not enough candles")
                continue

            candles = add_indicators(candles)

            candles.to_csv(
                OUTPUT / f"{name}_candles.csv",
                index=False
            )

            for config in TESTS:

                trades = run_backtest(
                    candles,
                    name,
                    config
                )

                trades.to_csv(
                    OUTPUT / f"{name}_{config['name']}.csv",
                    index=False
                )

                summary = report(
                    trades,
                    name,
                    config["name"]
                )

                if summary:
                    all_summaries.append(summary)

                if not trades.empty:
                    all_trades.append(trades)

        except Exception as error:
            print(f"{name} ERROR: {error}")

    if all_summaries:

        summary_df = pd.DataFrame(all_summaries)

        summary_df.to_csv(
            OUTPUT / "V3_COMPARISON.csv",
            index=False
        )

        print("\n========== FINAL COMPARISON ==========")
        print(
            summary_df.to_string(index=False)
        )

    if all_trades:

        combined = pd.concat(
            all_trades,
            ignore_index=True
        )

        combined.to_csv(
            OUTPUT / "ALL_TRADES.csv",
            index=False
        )

    print("\nAll results saved.")
    print(f"Reports folder: {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
