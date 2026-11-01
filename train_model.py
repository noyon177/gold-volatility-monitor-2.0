"""
Train the ML meta-label filter from collect_trades.py output.

  python train_model.py --trades trades_xau.csv trades_btc.csv --out ml_model.joblib

Honest-validation design
  1. Data is sorted by time. The LAST 20% is a hold-out that is never used to
     fit or to choose anything.
  2. Inside the first 80%, an expanding walk-forward gives out-of-sample
     probabilities; the probability threshold is chosen from those only.
  3. A model fitted on the first 80% is then tested once on the hold-out.
  4. The model file is saved ONLY if the filter beats "take every trade" on the
     hold-out and is profitable there (override with --force).
"""

import argparse
import datetime as dt
import sys

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance

from ml_features import FEATURE_COLS


def make_model():
    return HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.05, max_iter=150,
        min_samples_leaf=30, l2_regularization=1.0, random_state=7,
    )


def stats(r):
    r = np.asarray(r, dtype=float)
    if len(r) == 0:
        return dict(n=0, avg=0.0, total=0.0, win=0.0, t=0.0)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    t = r.mean() / (sd / np.sqrt(len(r))) if sd > 0 else 0.0
    return dict(n=len(r), avg=r.mean(), total=r.sum(), win=(r > 0).mean(), t=t)


def fmt(s):
    return f"n={s['n']:<5} avgR={s['avg']:+.3f}  totalR={s['total']:+7.1f}  win={s['win'] * 100:4.0f}%  t={s['t']:+.2f}"


def bootstrap_ci(r, n=2000, seed=1):
    r = np.asarray(r, dtype=float)
    if len(r) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = [rng.choice(r, len(r)).mean() for _ in range(n)]
    return tuple(np.percentile(means, [2.5, 97.5]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", nargs="+", required=True)
    ap.add_argument("--out", default="ml_model.joblib")
    ap.add_argument("--min-trades", type=int, default=300)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--force", action="store_true", help="save even if validation fails")
    args = ap.parse_args()

    df = pd.concat([pd.read_csv(p) for p in args.trades], ignore_index=True)
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    print(f"{n} trades {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
    if n < args.min_trades and not args.force:
        sys.exit(f"Need at least {args.min_trades} trades for a trustworthy model (have {n}). "
                 "Download more history, or use --min-trades to override (not recommended).")

    X = df[FEATURE_COLS].to_numpy(dtype=float)
    y = (df["r"] > 0).astype(int).to_numpy()
    r = df["r"].to_numpy(dtype=float)

    cut = int(n * 0.8)
    dev_idx = np.arange(0, cut)
    hold_idx = np.arange(cut, n)
    print(f"development: {len(dev_idx)} trades | hold-out: {len(hold_idx)} trades")
    print("\nResult by symbol and by score (all trades, for understanding the rules)")
    for sym, g in df.groupby("symbol"):
        print(f"  {sym:<7}", fmt(stats(g["r"].to_numpy())))
    for sc, g in df.groupby("score"):
        print(f"  score {int(sc):<2}", fmt(stats(g["r"].to_numpy())))
    print("\nBaseline (take every signal)")
    print("  dev     ", fmt(stats(r[dev_idx])))
    print("  hold-out", fmt(stats(r[hold_idx])))

    # ---- walk-forward inside the development part ----
    K = args.folds
    bounds = [len(dev_idx) * k // (K + 1) for k in range(1, K + 2)]
    oos_p, oos_r = [], []
    for k in range(K):
        tr = np.arange(0, bounds[k])
        te = np.arange(bounds[k], bounds[k + 1])
        if len(tr) < 100 or len(te) == 0 or len(set(y[tr])) < 2:
            continue
        m = make_model().fit(X[tr], y[tr])
        oos_p.append(m.predict_proba(X[te])[:, 1])
        oos_r.append(r[te])
    if not oos_p:
        sys.exit("Not enough data for walk-forward validation.")
    oos_p = np.concatenate(oos_p)
    oos_r = np.concatenate(oos_r)
    print("\nWalk-forward out-of-sample (development part)")
    print("  all     ", fmt(stats(oos_r)))

    best, best_t = None, -1e9
    print("  threshold | kept trades")
    for thr in np.arange(0.40, 0.74, 0.02):
        keep = oos_p >= thr
        s = stats(oos_r[keep])
        ok = s["n"] >= max(30, 0.25 * len(oos_r))
        print(f"   {thr:.2f}     {fmt(s)} {'' if ok else '(too few)'}")
        if ok and s["t"] > best_t:
            best, best_t = float(round(thr, 2)), s["t"]
    if best is None:
        print("\nNo threshold keeps enough trades. The model finds no usable pattern.")
        best = 0.5
    print(f"\nChosen threshold (from walk-forward only): {best:.2f}")

    # ---- single look at the hold-out ----
    final_dev = make_model().fit(X[dev_idx], y[dev_idx])
    hp = final_dev.predict_proba(X[hold_idx])[:, 1]
    keep = hp >= best
    base = stats(r[hold_idx])
    filt = stats(r[hold_idx][keep])
    lo, hi = bootstrap_ci(r[hold_idx][keep])
    print("\nHOLD-OUT RESULT (never used for training or tuning)")
    print("  take all     ", fmt(base))
    print("  ML filtered  ", fmt(filt))
    print(f"  95% CI of filtered avg R: [{lo:+.3f}, {hi:+.3f}]")

    passed = (
        filt["n"] >= max(20, 0.2 * len(hold_idx))
        and filt["avg"] > 0
        and filt["avg"] > base["avg"] + 0.03
    )
    if passed:
        print("\nVALIDATION: PASS (filter improves the hold-out)")
        if lo <= 0:
            print("  note: the confidence interval still includes 0 - the sample is small. "
                  "Treat as promising, not proven; paper-trade first.")
    else:
        print("\nVALIDATION: FAIL - on unseen data the filter does not give a clear improvement.")

    try:
        imp = permutation_importance(final_dev, X[hold_idx], y[hold_idx], n_repeats=5, random_state=3)
        order = np.argsort(imp.importances_mean)[::-1][:8]
        print("\nMost used features (hold-out permutation importance)")
        for j in order:
            print(f"  {FEATURE_COLS[j]:<18} {imp.importances_mean[j]:+.4f}")
    except Exception as exc:
        print("importance skipped:", exc)

    if passed or args.force:
        final = make_model().fit(X, y)
        joblib.dump({
            "model": final, "features": FEATURE_COLS, "threshold": best,
            "sklearn_version": sklearn.__version__,
            "created": dt.datetime.utcnow().isoformat(),
            "n_trades": n, "holdout": {"base": base, "filtered": filt},
        }, args.out)
        print(f"\nModel saved -> {args.out} (threshold {best:.2f})")
    else:
        print("\nModel NOT saved. Do not deploy a filter that failed validation. "
              "Options: more history, different rules, or keep v7 without ML.")


if __name__ == "__main__":
    main()
