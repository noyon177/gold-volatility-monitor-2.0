"""
Walk through historical 5M candles with the SAME rule engine as the live bot,
simulate every signal with the SAME trade manager (limit fill, TP1, trail, TP2,
SL, timeout, spread cost) and save one row per trade: features + result in R.

  python collect_trades.py --symbol XAUUSD --csv xauusd_5m.csv --out trades_xau.csv --workers 4

Rules mirrored from live: session filter, weekend close, 1 open trade per symbol.
Not simulated (no history available): news blackout, adaptive/learning filters,
daily loss cap, SL-streak pause.  Rows use only candles known at signal time.
"""

import argparse
import csv
import datetime as dt
import importlib
import multiprocessing as mp
import sys

ENGINE = "ict_smc_market_reader_v8"
bot = importlib.import_module(ENGINE)
from ml_features import FEATURE_COLS, build_features  # noqa: E402

UTC = dt.timezone.utc
G = {}


def load_candles(path):
    by_time = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = dt.datetime.strptime(r["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            except (KeyError, ValueError):
                continue
            if h < max(o, c, lo) or lo > min(o, c, h):
                continue
            by_time[ts] = {"time": r["datetime"][:19], "dt": ts, "open": o, "high": h, "low": lo, "close": c}
    return [by_time[k] for k in sorted(by_time)]


def sim_range(span):
    i0, i1 = span
    data, name, config, cfg, window = G["data"], G["name"], G["config"], G["cfg"], G["window"]
    idx_of = G["idx_of"]
    max_age = bot.TRADE_MAX_AGE_HOURS
    horizon = max_age * 12 + 12
    rows = []
    busy_until = -1
    i = max(i0, window - 1)
    while i < min(i1, len(data) - 1):
        if i <= busy_until:
            i += 1
            continue
        cdt = data[i]["dt"] + dt.timedelta(minutes=5)
        if config.get("weekend_closed") and bot.market_closed_weekend(cdt):
            i += 1
            continue
        if cfg["sessions"] and not bot.in_session(cdt, cfg["sessions"]):
            i += 1
            continue
        if (data[i]["dt"] - data[i - 3]["dt"]) > dt.timedelta(minutes=15):
            i += 1
            continue
        snap = data[i - window + 1:i + 1]
        try:
            res = bot.evaluate(snap, cfg)
        except Exception:
            res = None
        plan = res.get("signal") if res else None
        if not plan:
            i += 1
            continue

        deadline = plan["close_dt"] + dt.timedelta(hours=max_age)
        future = data[i + 1:i + 1 + horizon]
        if not future or future[-1]["dt"] < deadline:
            break  # not enough history left to resolve this trade
        st = {"open_trades": []}
        bot.register_trade(st, name, plan)
        t = st["open_trades"][0]
        _events, closed = bot.advance_trade(t, future, deadline + dt.timedelta(minutes=1))
        end_dt = bot._from_iso(t["checked_until"])
        busy_until = idx_of.get(end_dt, i + 1)
        if closed is not None and closed[0] != "CANCEL":
            feats = build_features(plan, snap, name)
            r = closed[1] - t.get("cost_r", 0.0) - bot.TRADE_COST_R
            row = {"time": plan["time"], "symbol": name, "side": plan["side"],
                   "result": closed[0], "r": round(r, 4)}
            row.update({k: round(v, 6) for k, v in feats.items()})
            rows.append(row)
        i = max(i + 1, busy_until + 1)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, choices=["XAUUSD", "BTCUSD"])
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--window", type=int, default=bot.SCALP_BARS)
    args = ap.parse_args()

    config = next(c for c in bot.SYMBOLS if c["name"] == args.symbol)
    data = load_candles(args.csv)
    if len(data) < args.window + 500:
        sys.exit(f"Only {len(data)} candles - need at least {args.window + 500}.")
    print(f"{len(data)} candles {data[0]['time']} -> {data[-1]['time']}")

    G.update(data=data, name=args.symbol, config=config, cfg=bot.scalp_cfg(config),
             window=args.window, idx_of={c["dt"]: k for k, c in enumerate(data)})

    n = len(data)
    workers = max(1, args.workers)
    if workers > 1 and "fork" not in mp.get_all_start_methods():
        print("fork not available on this OS, using 1 worker")
        workers = 1
    bounds = [args.window - 1 + (n - args.window + 1) * k // workers for k in range(workers + 1)]
    spans = [(bounds[k], bounds[k + 1]) for k in range(workers)]

    if workers == 1:
        parts = [sim_range(spans[0])]
    else:
        with mp.get_context("fork").Pool(workers) as pool:
            parts = pool.map(sim_range, spans)
    rows = [r for p in parts for r in p]
    rows.sort(key=lambda r: r["time"])

    fields = ["time", "symbol", "side", "result", "r"] + FEATURE_COLS
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    if rows:
        tot = sum(r["r"] for r in rows)
        wins = sum(1 for r in rows if r["r"] > 0)
        print(f"{len(rows)} trades | win {wins / len(rows) * 100:.0f}% | total {tot:+.1f}R | avg {tot / len(rows):+.3f}R")
    else:
        print("No trades produced.")
    print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()
