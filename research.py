"""Honest research + learning step.

  python research.py --data data/xauusd_5m.csv:XAUUSD data/btcusd_5m.csv:BTCUSD

1. builds every candidate trade from history (London / New York sessions only) and simulates it
2. compares with a RANDOM-ENTRY benchmark (same exits, same costs)
3. walk-forward learning (expanding window) picks the probability threshold
4. the LAST 25% of time is a hold-out that is never used for training or tuning
5. model.joblib is written ONLY if the hold-out passes every gate in config.py
   (otherwise the bot stays in observe mode: analysis + shadow journal, no trade signals)
"""
import argparse
import datetime as dt
import os

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

import config as C
import data
import simulate as SIM
import strategy as S

OUT = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.append(s)


def stats(r):
    r = np.asarray(r, float)
    n = len(r)
    if n == 0:
        return dict(n=0, avg=0.0, tot=0.0, win=0.0, t=0.0)
    sd = r.std(ddof=1) if n > 1 else 0.0
    return dict(n=n, avg=r.mean(), tot=r.sum(), win=(r > 0).mean(), t=r.mean() / (sd / np.sqrt(n)) if sd > 0 else 0.0)


def fmt(s):
    return f"n={s['n']:<5} avgR={s['avg']:+.3f} totalR={s['tot']:+7.1f} win={s['win'] * 100:3.0f}% t={s['t']:+.2f}"


def boot_ci(r, n=2000):
    r = np.asarray(r, float)
    if len(r) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(1)
    m = [rng.choice(r, len(r)).mean() for _ in range(n)]
    return tuple(np.percentile(m, [2.5, 97.5]))


def passes(s):
    return s["n"] >= C.MIN_KEPT and s["avg"] >= C.MIN_AVG_R and s["t"] >= C.MIN_T


def make_model():
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150,
                                          min_samples_leaf=30, l2_regularization=1.0, random_state=7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", required=True, help="path:SYMBOL, e.g. data/xauusd_5m.csv:XAUUSD")
    ap.add_argument("--out", default=C.MODEL_PATH)
    ap.add_argument("--report", default="report.txt")
    ap.add_argument("--fail-closed", action="store_true", help="delete an existing model if validation fails")
    ap.add_argument("--save-unvalidated", action="store_true", help="testing only: bot will NOT send signals with it")
    args = ap.parse_args()

    parts, rand_avgs = [], []
    for item in args.data:
        path, sym = item.rsplit(":", 1)
        P = S.prepare(data.load_csv(path), sym)
        if P.empty:
            say(f"{sym}: not enough data"); continue
        cand = S.candidates(P, sym)
        lab = SIM.label(P, cand, C.SYMBOLS[sym]["spread"]) if len(cand) else cand
        say(f"{sym}: {len(P)} bars {P.index[0]:%Y-%m-%d} -> {P.index[-1]:%Y-%m-%d} | candidates {len(lab)}")
        parts.append(lab)
        for seed in range(5):
            rl = SIM.label(P, S.random_cands(P, max(len(lab), 50), seed), C.SYMBOLS[sym]["spread"])
            rand_avgs.append((len(rl), rl["r"].mean()))
    D = pd.concat([p for p in parts if len(p)]).sort_index(kind="stable")
    say(f"\nTotal {len(D)} trades {D.index[0]:%Y-%m-%d} -> {D.index[-1]:%Y-%m-%d}")
    if len(D) < 150:
        say("Too few trades for a trustworthy test. Use more history (730+ days)."); finish(args, False); return

    rand_avg = float(np.average([a for _, a in rand_avgs], weights=[n for n, _ in rand_avgs]))
    say(f"\nRandom-entry benchmark (same exits and costs): avgR={rand_avg:+.3f}")
    say("Rules, take every signal:", fmt(stats(D["r"])), f"| gross avgR={D['r_gross'].mean():+.3f}")
    for (sym, setup), g in D.groupby(["symbol", "setup"]):
        say(f"  {sym} {S.SETUP_NAMES[int(setup)]:<14}", fmt(stats(g["r"])))
    say("  results:", D["result"].value_counts().to_dict())

    cut = D.index[int(len(D) * 0.75)]
    dev = D[D.index < cut - pd.Timedelta(hours=6)]
    hold = D[D.index >= cut]
    say(f"\nDevelopment {len(dev)} trades (up to {cut:%Y-%m-%d}) | hold-out {len(hold)} trades (never touched)")
    say("  dev take-all    ", fmt(stats(dev["r"])))
    say("  hold take-all   ", fmt(stats(hold["r"])))

    X = D[S.FEATURES].to_numpy(float)
    y = (D["r"] > 0).astype(int).to_numpy()
    r = D["r"].to_numpy(float)
    dev_idx = np.where(D.index < cut - pd.Timedelta(hours=6))[0]
    hold_idx = np.where(D.index >= cut)[0]

    K = 5
    bnd = [len(dev_idx) * k // (K + 1) for k in range(1, K + 2)]
    oos_p, oos_r = [], []
    for k in range(K):
        tr, te = dev_idx[:bnd[k]], dev_idx[bnd[k]:bnd[k + 1]]
        if len(tr) < 100 or len(te) == 0 or len(set(y[tr])) < 2:
            continue
        m = make_model().fit(X[tr], y[tr])
        oos_p.append(m.predict_proba(X[te])[:, 1]); oos_r.append(r[te])
    best_thr, best_t = None, -1e9
    if oos_p:
        oos_p, oos_r = np.concatenate(oos_p), np.concatenate(oos_r)
        say(f"\nWalk-forward out-of-sample (learning on the past only): {fmt(stats(oos_r))}")
        for q in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            thr = float(np.quantile(oos_p, q))
            s = stats(oos_r[oos_p >= thr])
            ok = s["n"] >= max(30, 0.15 * len(oos_r))
            say(f"  keep top {int((1 - q) * 100):>2}% (p>={thr:.2f}) {fmt(s)} {'' if ok else '(too few)'}")
            if ok and s["t"] > best_t:
                best_thr, best_t = thr, s["t"]

    hold_all = stats(r[hold_idx])
    rules_ok = passes(hold_all) and stats(r[dev_idx])["avg"] > 0 and hold_all["avg"] > rand_avg + C.MIN_AVG_R
    ml_ok, filt = False, None
    if best_thr is not None and len(hold_idx):
        fm = make_model().fit(X[dev_idx], y[dev_idx])
        keep = fm.predict_proba(X[hold_idx])[:, 1] >= best_thr
        filt = stats(r[hold_idx][keep])
        lo, hi = boot_ci(r[hold_idx][keep])
        say(f"\nHOLD-OUT: rules only  {fmt(hold_all)}")
        say(f"HOLD-OUT: ML filtered {fmt(filt)}  95% CI [{lo:+.3f}, {hi:+.3f}]")
        ml_ok = passes(filt) and filt["avg"] > hold_all["avg"] + 0.03 and filt["avg"] > rand_avg + C.MIN_AVG_R

    say(f"\nGates: avgR >= {C.MIN_AVG_R}, t >= {C.MIN_T}, n >= {C.MIN_KEPT}, and better than random ({rand_avg:+.3f}).")
    if rules_ok:
        mode = "rules"
    elif ml_ok:
        mode = "ml"
    else:
        mode = None
    if mode:
        say(f"VALIDATION: PASS ({'rules alone' if mode == 'rules' else 'rules + ML filter'}). Promising, not proven: paper-trade first.")
    else:
        say("VALIDATION: FAIL. On unseen data there is no proven edge. The bot stays in OBSERVE mode (analysis only, no trade signals).")

    if mode or args.save_unvalidated:
        final = make_model().fit(X, y)
        joblib.dump({"model": final, "features": S.FEATURES, "threshold": best_thr if best_thr is not None else 0.0,
                     "use_ml": mode == "ml", "validated": bool(mode), "sklearn": sklearn.__version__,
                     "created": dt.datetime.now(dt.timezone.utc).isoformat(), "n_trades": len(D),
                     "holdout": {"rules": hold_all, "ml": filt}}, args.out)
        say(f"Saved -> {args.out} (validated={bool(mode)})")
    finish(args, bool(mode))


def finish(args, ok):
    if not ok and args.fail_closed and os.path.exists(args.out) and not args.save_unvalidated:
        os.remove(args.out)
        say("Existing model removed (fail-closed).")
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
