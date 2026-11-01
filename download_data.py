"""
Download historical 5-minute candles from Twelve Data into a CSV.

  python download_data.py --symbol XAU/USD --out xauusd_5m.csv --days 180
  python download_data.py --symbol BTC/USD --out btcusd_5m.csv --days 180

Needs env TWELVE_DATA_API_KEY. Each request returns up to 5000 candles
(~17 days of 5M), so 180 days is ~11 requests. Free plan: 8 requests/minute,
so the script waits between requests. Re-running appends/merges into the CSV.

CSV columns: datetime (UTC), open, high, low, close
You can also use your own CSV (e.g. exported from MT5) in this same format.
"""

import argparse
import csv
import datetime as dt
import os
import sys
import time

import requests

FMT = "%Y-%m-%d %H:%M:%S"


def read_existing(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows[r["datetime"]] = r
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. XAU/USD or BTC/USD")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--sleep", type=float, default=8.0, help="seconds between requests")
    args = ap.parse_args()

    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        sys.exit("Set TWELVE_DATA_API_KEY first.")

    rows = read_existing(args.out)
    target = dt.datetime.utcnow() - dt.timedelta(days=args.days)
    end_date = None
    calls = 0

    while True:
        params = {
            "symbol": args.symbol, "interval": "5min", "outputsize": 5000,
            "timezone": "UTC", "apikey": key, "order": "DESC",
        }
        if end_date:
            params["end_date"] = end_date
        resp = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=60)
        calls += 1
        data = resp.json()
        if data.get("code") == 429:
            print("Rate limit, waiting 60s...")
            time.sleep(60)
            continue
        if "values" not in data:
            print("API message:", data)
            break
        vals = data["values"]
        if len(vals) < 2:
            break
        for v in vals:
            rows[v["datetime"]] = {
                "datetime": v["datetime"], "open": v["open"], "high": v["high"],
                "low": v["low"], "close": v["close"],
            }
        oldest = min(v["datetime"] for v in vals)
        print(f"request {calls}: got {len(vals)} candles, oldest {oldest}")
        if dt.datetime.strptime(oldest, FMT) <= target:
            break
        if end_date == oldest:
            break
        end_date = oldest
        time.sleep(args.sleep)

    ordered = [rows[k] for k in sorted(rows)]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["datetime", "open", "high", "low", "close"])
        w.writeheader()
        w.writerows(ordered)
    print(f"saved {len(ordered)} candles -> {args.out}")


if __name__ == "__main__":
    main()
