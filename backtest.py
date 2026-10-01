
import os
import time
import requests
import pandas as pd
import datetime as dt
from pathlib import Path

# ============================================================
# EMA 9/15 + PINBAR BACKTEST V2
# Matched with Original Signal Bot
#
# FIX 1: Exact SMA-seeded EMA calculation
# FIX 2: Entry at next candle OPEN
# FIX 3: Recalculate SL, TP and risk after entry
# FIX 4: Correct Maximum Drawdown calculation
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
RISK_REWARD = 2.0

TOUCH_ATR = 0.15
MIN_SL_ATR = 1.0
MAX_SL_ATR = 2.5

OUTPUT = Path("backtest_results_v2")
OUTPUT.mkdir(exist_ok=True)

UTC = dt.timezone.utc
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "EMA-Pinbar-Backtest/2.0"
})


# ============================================================
# DOWNLOAD HISTORICAL DATA
# ============================================================

def download_data(symbol, days):

    if not API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY environment variable missing"
        )

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
                f"API error for {symbol}: {data}"
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
        raise RuntimeError(
            f"No data received for {symbol}"
        )

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

    df = df.sort_values(
        "dt"
    ).reset_index(drop=True)

    # Remove incomplete candles
    now = pd.Timestamp.now(tz="UTC")

    df = df[
        df["dt"] + pd.Timedelta(
            minutes=INTERVAL_MINUTES
        ) <= now
    ].reset_index(drop=True)

    return df


# ============================================================
# EXACT EMA CALCULATION FROM ORIGINAL BOT
# ============================================================

def ema_series(values, period):

    result = [None] * len(values)

    if len(values) < period:
        return result

    k = 2 / (period + 1)

    # SMA seed — same as original signal bot
    result[period - 1] = sum(
        values[:period]
    ) / period

    for i in range(period, len(values)):

        result[i] = (
            values[i] * k
            + result[i - 1] * (1 - k)
        )

    return result


# ============================================================
# EXACT WILDER ATR FROM ORIGINAL BOT
# ============================================================

def atr_series(df, period):

    if df.empty:
        return []

    candles = df.to_dict("records")

    true_ranges = []

    for i, candle in enumerate(candles):

        if i == 0:
            tr = candle["high"] - candle["low"]

        else:
            previous_close = candles[i - 1]["close"]

            tr = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close)
            )

        true_ranges.append(tr)

    result = [None] * len(candles)

    if len(candles) < period:
        return result

    result[period - 1] = sum(
        true_ranges[:period]
    ) / period

    for i in range(period, len(candles)):

        result[i] = (
            result[i - 1] * (period - 1)
            + true_ranges[i]
        ) / period

    return result


# ============================================================
# ADD INDICATORS
# ============================================================

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
# PINBAR DETECTION — SAME AS ORIGINAL BOT
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

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    close_position = (
        c - l
    ) / candle_range

    # Bullish rejection
    if (
        lower_wick >= PINBAR_WICK_RATIO * body_for_ratio
        and upper_wick <= PINBAR_NOSE_MAX_RATIO * lower_wick
        and close_position >= 1 - CLOSE_POSITION_LIMIT
    ):
        return "BUY"

    # Bearish rejection
    if (
        upper_wick >= PINBAR_WICK_RATIO * body_for_ratio
        and lower_wick <= PINBAR_NOSE_MAX_RATIO * upper_wick
        and close_position <= CLOSE_POSITION_LIMIT
    ):
        return "SELL"

    return None


# ============================================================
# SIGNAL DETECTION
# ============================================================

def get_signal(df, i):

    row = df.iloc[i]
    prev = df.iloc[i - 1]
    slope = df.iloc[i - EMA_SLOPE_LOOKBACK]

    if pd.isna(row["atr"]) or row["atr"] <= 0:
        return None

    pinbar = classify_pinbar(row)

    if pinbar is None:
        return None

    atr = row["atr"]

    # EMA15 touch
    ema_touch = (
        row["low"] <= row["ema15"] + TOUCH_ATR * atr
        and row["high"] >= row["ema15"] - TOUCH_ATR * atr
    )

    if not ema_touch:
        return None

    # Trend filters — same as original bot
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

    signal_close = row["close"]
    buffer = SL_BUFFER_ATR * atr

    if pinbar == "BUY" and trend_up:

        if prev["close"] <= prev["ema15"]:
            return None

        sl = row["low"] - buffer

        original_risk = signal_close - sl

        if original_risk <= 0:
            return None

        original_risk_atr = original_risk / atr

        if not MIN_SL_ATR <= original_risk_atr <= MAX_SL_ATR:
            return None

    elif pinbar == "SELL" and trend_down:

        if prev["close"] >= prev["ema15"]:
            return None

        sl = row["high"] + buffer

        original_risk = sl - signal_close

        if original_risk <= 0:
            return None

        original_risk_atr = original_risk / atr

        if not MIN_SL_ATR <= original_risk_atr <= MAX_SL_ATR:
            return None

    else:
        return None

    return {
        "side": pinbar,
        "signal_close": signal_close,
        "sl": sl,
        "atr": atr,
        "signal_time": row["dt"],
        "ema9": row["ema9"],
        "ema15": row["ema15"],
        "original_risk_atr": original_risk_atr,
    }


# ============================================================
# EXECUTION — NEXT CANDLE OPEN
# ============================================================

def prepare_trade(signal, entry):

    side = signal["side"]
    sl = signal["sl"]
    atr = signal["atr"]

    if side == "BUY":
        risk = entry - sl

    else:
        risk = sl - entry

    if risk <= 0:
        return None

    risk_atr = risk / atr

    # Reject trades where execution price makes risk invalid
    if not MIN_SL_ATR <= risk_atr <= MAX_SL_ATR:
        return None

    if side == "BUY":
        tp = entry + RISK_REWARD * risk

    else:
        tp = entry - RISK_REWARD * risk

    return {
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
    }


# ============================================================
# TRADE SIMULATION
# ============================================================

def simulate_trade(df, trade, start_index):

    side = trade["side"]
    sl = trade["sl"]
    tp = trade["tp"]

    for j in range(start_index, len(df)):

        candle = df.iloc[j]

        if side == "BUY":

            hit_sl = candle["low"] <= sl
            hit_tp = candle["high"] >= tp

        else:

            hit_sl = candle["high"] >= sl
            hit_tp = candle["low"] <= tp

        # Conservative assumption:
        # If SL and TP both hit in one candle, SL first.
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
                "R": RISK_REWARD,
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

def run_backtest(df, symbol):

    trades = []

    warmup = max(
        ATR_PERIOD,
        EMA_SLOW
    ) + EMA_SLOPE_LOOKBACK + 2

    i = warmup

    while i < len(df) - 1:

        signal = get_signal(df, i)

        if signal is None:
            i += 1
            continue

        # Entry occurs at NEXT candle OPEN
        entry_index = i + 1
        entry_candle = df.iloc[entry_index]

        entry = entry_candle["open"]

        trade_setup = prepare_trade(
            signal,
            entry
        )

        if trade_setup is None:
            i += 1
            continue

        outcome = simulate_trade(
            df,
            trade_setup,
            entry_index
        )

        trade = {
            "symbol": symbol,
            "side": trade_setup["side"],
            "signal_time": signal["signal_time"],
            "entry_time": entry_candle["dt"],
            "signal_close": signal["signal_close"],
            "entry": trade_setup["entry"],
            "sl": trade_setup["sl"],
            "tp": trade_setup["tp"],
            "risk": trade_setup["risk"],
            "risk_atr": trade_setup["risk_atr"],
            "original_risk_atr": signal["original_risk_atr"],
            "ema9": signal["ema9"],
            "ema15": signal["ema15"],
            "atr": signal["atr"],
            "result": outcome["result"],
            "exit": outcome["exit"],
            "exit_time": outcome["exit_time"],
            "R": outcome["R"],
        }

        trades.append(trade)

        # One active trade at a time
        i = outcome["exit_index"] + 1

    return pd.DataFrame(trades)


# ============================================================
# PERFORMANCE REPORT
# ============================================================

def report(df, symbol):

    if df.empty:
        print(f"\n{symbol}: No trades found.")
        return

    closed = df[
        df["result"].isin(["WIN", "LOSS"])
    ].copy()

    if closed.empty:
        print(f"\n{symbol}: No closed trades.")
        return

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

    # Correct equity curve:
    # Start from 0R initial equity
    equity = [
        0.0
    ] + closed["R"].cumsum().tolist()

    equity = pd.Series(equity)

    running_peak = equity.cummax()
    drawdown = running_peak - equity

    max_dd = drawdown.max()

    consecutive_losses = 0
    max_consecutive_losses = 0

    for result in closed["result"]:

        if result == "LOSS":

            consecutive_losses += 1

            max_consecutive_losses = max(
                max_consecutive_losses,
                consecutive_losses
            )

        else:
            consecutive_losses = 0

    print("\n" + "=" * 50)
    print(f"BACKTEST V2 REPORT — {symbol}")
    print("=" * 50)

    print(f"Total Closed Trades : {total}")
    print(f"Wins                : {wins}")
    print(f"Losses              : {losses}")
    print(f"Win Rate            : {win_rate:.2f}%")
    print(f"Profit Factor       : {profit_factor:.2f}")
    print(f"Net Result          : {closed['R'].sum():.2f} R")
    print(f"Max Drawdown        : {max_dd:.2f} R")
    print(f"Max Consecutive Loss: {max_consecutive_losses}")

    print("=" * 50)


# ============================================================
# MAIN
# ============================================================

def main():

    all_trades = []

    for name, td_symbol in SYMBOLS.items():

        print(f"\nDownloading {name} data...")

        try:
            candles = download_data(
                td_symbol,
                DAYS
            )

            print(
                f"Received {len(candles)} candles"
            )

            if len(candles) < 100:
                print("Not enough candles. Skipping.")
                continue

            candles = add_indicators(candles)

            candles.to_csv(
                OUTPUT / f"{name}_candles.csv",
                index=False
            )

            trades = run_backtest(
                candles,
                name
            )

            trades.to_csv(
                OUTPUT / f"{name}_trades.csv",
                index=False
            )

            report(
                trades,
                name
            )

            if not trades.empty:
                all_trades.append(trades)

        except Exception as error:
            print(f"{name} ERROR: {error}")

    if all_trades:

        combined = pd.concat(
            all_trades,
            ignore_index=True
        )

        combined.to_csv(
            OUTPUT / "ALL_TRADES.csv",
            index=False
        )

        print("\nAll trade records saved.")

    else:
        print("\nNo trades generated.")

    print(
        f"\nReports folder: {OUTPUT.resolve()}"
    )


if __name__ == "__main__":
    main()
