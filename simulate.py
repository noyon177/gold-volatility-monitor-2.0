"""One trade simulator, used for history (labels) and live (resolving journal rows).
Entry = open of the bar after the signal. SL and TP in the same bar -> SL first (pessimistic).
A gap through the stop fills at the open. Spread is charged in R. 6-hour time stop."""
import numpy as np

import config as C

GAP_MIN = 60   # a hole longer than this (weekend, maintenance) closes the trade at the next open


def bars(P):
    st = P["start"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy().astype("datetime64[m]").astype("int64")
    return {"st": st, "o": P["open"].to_numpy(float), "h": P["high"].to_numpy(float),
            "l": P["low"].to_numpy(float), "c": P["close"].to_numpy(float)}


def run_trade(A, j, side, risk, entry=None):
    """Simulate from entry bar j. Returns (status, r_gross, bars_used, exit_index).
    status: SL / TP / TIME / GAP, or OPEN when the future is not complete yet."""
    st, o, h, l, c = A["st"], A["o"], A["h"], A["l"], A["c"]
    n = len(o)
    if j >= n:
        return "OPEN", 0.0, 0, None
    entry = o[j] if entry is None else entry
    sl, tp = entry - side * risk, entry + side * C.RR * risk
    last = min(n, j + C.MAX_BARS)
    for k in range(j, last):
        if k > j and st[k] - st[k - 1] > GAP_MIN:
            return "GAP", side * (o[k] - entry) / risk, k - j + 1, k
        hit_sl = l[k] <= sl if side > 0 else h[k] >= sl
        hit_tp = h[k] >= tp if side > 0 else l[k] <= tp
        if hit_sl:
            px = min(o[k], sl) if side > 0 else max(o[k], sl)
            return "SL", side * (px - entry) / risk, k - j + 1, k
        if hit_tp:
            return "TP", C.RR, k - j + 1, k
    if last - j >= C.MAX_BARS:
        k = last - 1
        return "TIME", side * (c[k] - entry) / risk, C.MAX_BARS, k
    return "OPEN", 0.0, last - j, None


def label(P, cands, spread):
    """Attach the realised net R (after spread) to every candidate whose outcome is known."""
    A = bars(P)
    st, n = A["st"], len(A["st"])
    keep, res, gross, net = [], [], [], []
    for pos, (i, side, a) in enumerate(zip(cands["i"].to_numpy(int), cands["side"].to_numpy(int),
                                           cands["atr"].to_numpy(float))):
        j = i + 1
        if j >= n or st[j] - st[i] != C.TF_MIN:
            continue
        risk = C.SL_ATR * a
        status, r, _, _ = run_trade(A, j, side, risk)
        if status == "OPEN":
            continue
        keep.append(pos)
        res.append(status)
        gross.append(r)
        net.append(r - spread / risk - C.EXTRA_COST_R)
    out = cands.iloc[keep].copy()
    out["result"], out["r_gross"], out["r"] = res, gross, net
    return out
