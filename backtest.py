import os
import time
import requests
import pandas as pd
import datetime as dt
from pathlib import Path

# ==========================================
# EMA 9/15 + PINBAR BACKTEST ENGINE
# ==========================================

API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

SYMBOLS = {
    "XAUUSD": "XAU/USD",
    "BTCUSD": "BTC/USD",
}

INTERVAL = "15min"
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

OUTPUT = Path("backtest_results")
OUTPUT.mkdir(exist_ok=True)

UTC = dt.timezone.utc
SESSION = requests.Session()

# ==========================================
# DOWNLOAD HISTORICAL DATA
# ==========================================

def download_data(symbol, days):

    if not API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY environment variable missing"
        )

    end = dt.datetime.now(UTC).replace(
        second=0, microsecond=0
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

        # Respect API rate limits
        time.sleep(1)

    df = pd.DataFrame(all_rows)

    if df.empty:
        raise RuntimeError(f"No data received for {symbol}")

    df["dt"] = pd.to_datetime(
        df["datetime"],
        utc=True
    )

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(
        subset=["dt", "open", "high", "low", "close"]
    )

    df = df.drop_duplicates(subset=["dt"])
    df = df.sort_values("dt").reset_index(drop=True)

    # Remove incomplete candles
    now = pd.Timestamp.now(tz="UTC")

    df = df[
        df["dt"] + pd.Timedelta(minutes=15) <= now
    ].reset_index(drop=True)

    return df


# ==========================================
# INDICATORS
# ==========================================

def add_indicators(df):

    close = df["close"]

    df["ema9"] = close.ewm(
        span=EMA_FAST,
        adjust=False,
        min_periods=EMA_FAST
    ).mean()

    df["ema15"] = close.ewm(
        span=EMA_SLOW,
        adjust=False,
        min_periods=EMA_SLOW
    ).mean()

    previous_close = close.shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs()
    ], axis=1).max(axis=1)

    # Wilder ATR
    atr = [None] * len(df)

    if len(df) >= ATR_PERIOD:

        values = tr.tolist()

        atr[ATR_PERIOD - 1] = sum(
            values[:ATR_PERIOD]
        ) / ATR_PERIOD

        for i in range(ATR_PERIOD, len(df)):
            atr[i] = (
                atr[i - 1] * (ATR_PERIOD - 1)
                + values[i]
            ) / ATR_PERIOD

    df["atr"] = atr

    return df


# ==========================================
# PINBAR DETECTION
# ==========================================

def classify_pinbar(row):

    o = row["open"]
    h = row["high"]
    l = row["low"]
    c = row["close"]

    candle_range = h - l

    if candle_range <= 0:
        return None

    body = abs(c - o)
    body_ratio = max(body, candle_range * 0.01)

    upper = h - max(o, c)
    lower = min(o, c) - l

    close_position = (c - l) / candle_range

    if (
        lower >= PINBAR_WICK_RATIO * body_ratio
        and upper <= PINBAR_NOSE_MAX_RATIO * lower
        and close_position >= 1 - CLOSE_POSITION_LIMIT
    ):
        return "BUY"

    if (
        upper >= PINBAR_WICK_RATIO * body_ratio
        and lower <= PINBAR_NOSE_MAX_RATIO * upper
        and close_position <= CLOSE_POSITION_LIMIT
    ):
        return "SELL"

    return None


# ==========================================
# SIGNAL DETECTION
# ==========================================

def get_signal(df, i):

    row = df.iloc[i]
    prev = df.iloc[i - 1]
    slope = df.iloc[i - EMA_SLOPE_LOOKBACK]

    if pd.isna(row["atr"]) or row["atr"] <= 0:
        return None

    pinbar = classify_pinbar(row)

    if not pinbar:
        return None

    atr = row["atr"]

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

    entry = row["close"]
    buffer = SL_BUFFER_ATR * atr

    if pinbar == "BUY" and trend_up:

        if prev["close"] <= prev["ema15"]:
            return None

        sl = row["low"] - buffer
        risk = entry - sl

        if risk <= 0:
            return None

        risk_atr = risk / atr

        if not MIN_SL_ATR <= risk_atr <= MAX_SL_ATR:
            return None

        tp = entry + RISK_REWARD * risk

    elif pinbar == "SELL" and trend_down:

        if prev["close"] >= prev["ema15"]:
            return None

        sl = row["high"] + buffer
        risk = sl - entry

        if risk <= 0:
            return None

        risk_atr = risk / atr

        if not MIN_SL_ATR <= risk_atr <= MAX_SL_ATR:
            return None

        tp = entry - RISK_REWARD * risk

    else:
        return None

    return {
        "side": pinbar,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "risk_atr": risk_atr,
        "signal_time": row["dt"],
        "ema9": row["ema9"],
        "ema15": row["ema15"],
        "atr": atr,
    }


# ==========================================
# TRADE SIMULATION
# ==========================================

def simulate_trade(df, signal, start_index):

    side = signal["side"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    risk = signal["risk"]

    for j in range(start_index, len(df)):

        candle = df.iloc[j]

        if side == "BUY":

            hit_sl = candle["low"] <= sl
            hit_tp = candle["high"] >= tp

        else:

            hit_sl = candle["high"] >= sl
            hit_tp = candle["low"] <= tp

        # Conservative assumption:
        # If both hit in same candle, count SL first.
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


# ==========================================
# BACKTEST ENGINE
# ==========================================

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

        outcome = simulate_trade(
            df,
            signal,
            i + 1
        )

        trade = {
            "symbol": symbol,
            "side": signal["side"],
            "signal_time": signal["signal_time"],
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp": signal["tp"],
            "risk": signal["risk"],
            "risk_atr": signal["risk_atr"],
            "ema9": signal["ema9"],
            "ema15": signal["ema15"],
            "atr": signal["atr"],
            "result": outcome["result"],
            "exit": outcome["exit"],
            "exit_time": outcome["exit_time"],
            "R": outcome["R"],
        }

        trades.append(trade)

        # Do not open another trade while this one is active.
        i = outcome["exit_index"] + 1

    return pd.DataFrame(trades)


# ==========================================
# PERFORMANCE REPORT
# ==========================================

def report(df, symbol):

    if df.empty:
        print(f"\n{symbol}: No trades found.")
        return

    closed = df[df["result"].isin(["WIN", "LOSS"])]

    wins = (closed["result"] == "WIN").sum()
    losses = (closed["result"] == "LOSS").sum()

    total = len(closed)

    win_rate = (
        wins / total * 100
        if total else 0
    )

    gross_profit = closed.loc[
        closed["R"] > 0, "R"
    ].sum()

    gross_loss = abs(
        closed.loc[closed["R"] < 0, "R"].sum()
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss else float("inf")
    )

    equity = closed["R"].cumsum()

    if len(equity):
        running_peak = equity.cummax()
        drawdown = running_peak - equity
        max_dd = drawdown.max()
    else:
        max_dd = 0

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

    print("\n" + "=" * 45)
    print(f"BACKTEST REPORT — {symbol}")
    print("=" * 45)

    print(f"Total Closed Trades : {total}")
    print(f"Wins                : {wins}")
    print(f"Losses              : {losses}")
    print(f"Win Rate            : {win_rate:.2f}%")
    print(f"Profit Factor       : {profit_factor:.2f}")
    print(f"Net Result          : {closed['R'].sum():.2f} R")
    print(f"Max Drawdown        : {max_dd:.2f} R")
    print(f"Max Consecutive Loss: {max_consecutive_losses}")

    print("=" * 45)


# ==========================================
# MAIN
# ==========================================

def main():

    all_trades = []

    for name, td_symbol in SYMBOLS.items():

        print(f"\nDownloading {name} data...")

        try:
            candles = download_data(td_symbol, DAYS)

            print(f"Received {len(candles)} candles")

            if len(candles) < 100:
                print("Not enough candles. Skipping.")
                continue

            candles = add_indicators(candles)

            candles.to_csv(
                OUTPUT / f"{name}_candles.csv",
                index=False
            )

            trades = run_backtest(candles, name)

            trades.to_csv(
                OUTPUT / f"{name}_trades.csv",
                index=False
            )

            report(trades, name)

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

    print(f"\nReports folder: {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
