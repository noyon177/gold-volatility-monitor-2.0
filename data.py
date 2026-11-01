"""Twelve Data download / live fetch (5-minute candles, UTC).

  python data.py download --symbol XAU/USD --out data/xauusd_5m.csv --days 730
CSV format: datetime,open,high,low,close (UTC). Your own MT5 export in the same format also works.
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests

URL = "https://api.twelvedata.com/time_series"
FMT = "%Y-%m-%d %H:%M:%S"


def _to_df(values):
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.set_index("datetime")[["open", "high", "low", "close"]].sort_index()


def fetch(symbol, n=3000, end_date=None, key=None, retries=3):
    key = key or os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not set")
    p = {"symbol": symbol, "interval": "5min", "outputsize": n, "timezone": "UTC",
         "apikey": key, "order": "DESC"}
    if end_date:
        p["end_date"] = end_date
    for _ in range(retries):
        r = requests.get(URL, params=p, timeout=60).json()
        if r.get("code") == 429:
            time.sleep(60)
            continue
        if "values" not in r:
            raise RuntimeError(f"Twelve Data: {r.get('message', r)}")
        return _to_df(r["values"])
    raise RuntimeError("Twelve Data rate limit")


def load_csv(path):
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.set_index("datetime")[["open", "high", "low", "close"]].sort_index()


def download(symbol, out, days, sleep=8.0):
    have = load_csv(out) if os.path.exists(out) else pd.DataFrame()
    target = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    frames, end, calls = [], None, 0
    while True:
        df = fetch(symbol, 5000, end)
        calls += 1
        if len(df) < 2:
            break
        frames.append(df)
        oldest = df.index.min()
        print(f"request {calls}: {len(df)} candles, oldest {oldest}", flush=True)
        if oldest <= target:
            break
        end = (oldest - pd.Timedelta(minutes=5)).strftime(FMT)
        time.sleep(sleep)
    all_ = pd.concat(([have] if len(have) else []) + frames)
    all_ = all_[~all_.index.duplicated(keep="last")].sort_index()
    all_ = all_[all_.index >= target]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    all_.reset_index().rename(columns={"index": "datetime"}).to_csv(out, index=False)
    print(f"saved {len(all_)} candles -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download"])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--sleep", type=float, default=8.0)
    a = ap.parse_args()
    try:
        download(a.symbol, a.out, a.days, a.sleep)
    except RuntimeError as e:
        sys.exit(str(e))
