"""Indicators, sessions, market analysis and candidate setups.
The SAME code is used by research.py (history) and bot.py (live), so training and live see identical inputs.
All values at a row use only candles that were already CLOSED at that row's time (index = bar close time)."""
import numpy as np
import pandas as pd

import config as C

SETUP_NAMES = {1: "ট্রেন্ড-পুলব্যাক", 2: "সেশন ব্রেকআউট"}

FEATURES = [
    "setup", "side", "session", "msince", "hour", "weekday",
    "h1_gap", "h1_slope", "ema_gap", "dist_ema", "er", "rsi", "atr_pct", "atr_bp",
    "body", "range", "mom8", "mom32", "room_pd", "back_pd",
    "or_range", "or_pos", "from_open", "is_btc", "cost_r",
]


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def efficiency(c, n=20):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum()


def _resample(df, rule, min_count):
    g = df.resample(rule, label="left", closed="left")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    cnt = g["close"].count()
    return out[cnt >= min_count].dropna()


# ------------------------------------------------------------------ sessions
def session_info(t):
    """t: tz-aware UTC DatetimeIndex of bar CLOSE times.
    Returns (session 0/1/2, minutes since session open, session key). 1 = London, 2 = New York."""
    n = len(t)
    sess = np.zeros(n, dtype=int)
    ms = np.full(n, np.nan)
    key = np.array([""] * n, dtype=object)
    for code, (tz, start, end) in ((1, C.LONDON), (2, C.NEWYORK)):   # New York overrides London in the overlap
        loc = t.tz_convert(tz)
        mins = np.asarray(loc.hour) * 60 + np.asarray(loc.minute)
        ok = (mins > start) & (mins <= end) & (np.asarray(loc.dayofweek) < 5)
        sess = np.where(ok, code, sess)
        ms = np.where(ok, mins - start, ms)
        key = np.where(ok, np.asarray(loc.strftime("%Y-%m-%d"), dtype=object) + f"|{code}", key)
    return sess, ms, key


def session_now(ts):
    s, m, _ = session_info(pd.DatetimeIndex([ts]))
    return int(s[0]), float(m[0])


# ------------------------------------------------------------------- prepare
def prepare(df5, symbol=None):
    """5M candles -> 15M table indexed by bar close time, with every indicator and session level."""
    m = _resample(df5, "15min", 3)
    h = _resample(df5, "1h", 10)
    if len(m) < 400 or len(h) < 80:
        return pd.DataFrame()
    m["start"] = m.index
    m.index = m.index + pd.Timedelta(minutes=C.TF_MIN)
    m.index.name = "t"
    m["ema20"], m["ema50"], m["atr"] = ema(m["close"], 20), ema(m["close"], 50), atr(m)
    m["rsi"], m["er"] = rsi(m["close"]), efficiency(m["close"])
    m["atr_pct"] = m["atr"].rolling(200).rank(pct=True)
    m["close8"], m["close32"] = m["close"].shift(8), m["close"].shift(32)

    h["ema20"], h["ema50"], h["atr"] = ema(h["close"], 20), ema(h["close"], 50), atr(h)
    hh = pd.DataFrame({
        "tc": h.index + pd.Timedelta(hours=1),                    # a 1H bar is known only when it closes
        "h1_gap": (h["ema20"] - h["ema50"]) / h["atr"],
        "h1_slope": (h["ema50"] - h["ema50"].shift(5)) / h["atr"],
    }).sort_values("tc")
    mm = pd.merge_asof(m.reset_index().sort_values("t"), hh, left_on="t", right_on="tc",
                       direction="backward", tolerance=pd.Timedelta(hours=3)).drop(columns="tc").set_index("t")

    mm["day"] = mm["start"].dt.floor("D")
    d = mm.groupby("day").agg(dh=("high", "max"), dl=("low", "min")).shift(1)
    mm["pdh"], mm["pdl"] = mm["day"].map(d["dh"]), mm["day"].map(d["dl"])

    sess, ms, key = session_info(mm.index)
    mm["sess"], mm["msince"], mm["skey"] = sess, ms, key
    inor = mm["msince"] <= C.OR_MIN
    mm["or_high"] = mm["high"].where(inor).groupby(mm["skey"]).transform("max")
    mm["or_low"] = mm["low"].where(inor).groupby(mm["skey"]).transform("min")
    mm["sess_open"] = mm["open"].where(mm["msince"] == C.TF_MIN).groupby(mm["skey"]).transform("max")
    mm.loc[inor, ["or_high", "or_low"]] = np.nan          # the range is unknown until it is complete
    mm["i"] = np.arange(len(mm))
    return mm


# ----------------------------------------------------------------- features
def _features(s, symbol):
    side = s["side"].astype(float)
    a = s["atr"]
    f = pd.DataFrame(index=s.index)
    f["setup"], f["side"], f["session"], f["msince"] = s["setup"], s["side"], s["sess"], s["msince"]
    f["hour"], f["weekday"] = s.index.hour, s.index.dayofweek
    f["h1_gap"], f["h1_slope"] = side * s["h1_gap"], side * s["h1_slope"]
    f["ema_gap"] = side * (s["ema20"] - s["ema50"]) / a
    f["dist_ema"] = side * (s["close"] - s["ema20"]) / a
    f["er"], f["rsi"] = s["er"], np.where(side > 0, s["rsi"], 100 - s["rsi"])
    f["atr_pct"], f["atr_bp"] = s["atr_pct"], a / s["close"] * 1e4
    f["body"], f["range"] = side * (s["close"] - s["open"]) / a, (s["high"] - s["low"]) / a
    f["mom8"], f["mom32"] = side * (s["close"] - s["close8"]) / a, side * (s["close"] - s["close32"]) / a
    f["room_pd"] = np.where(side > 0, s["pdh"] - s["close"], s["close"] - s["pdl"]) / a
    f["back_pd"] = np.where(side > 0, s["close"] - s["pdl"], s["pdh"] - s["close"]) / a
    f["or_range"] = (s["or_high"] - s["or_low"]) / a
    f["or_pos"] = side * (s["close"] - (s["or_high"] + s["or_low"]) / 2) / a
    f["from_open"] = side * (s["close"] - s["sess_open"]) / a
    f["is_btc"] = 1.0 if symbol == "BTCUSD" else 0.0
    f["cost_r"] = C.SYMBOLS[symbol]["spread"] / (C.SL_ATR * a)
    return f


# --------------------------------------------------------------- candidates
def candidates(P, symbol, cooldown=True):
    """Rule-based setups. Two simple ideas, only inside London / New York sessions:
       1) trend pullback: 1H trend + 15M reclaim of EMA20
       2) session breakout: first close beyond the 30-minute opening range
    Returns one row per candidate (index = signal bar close time)."""
    if P.empty:
        return pd.DataFrame()
    c, o = P["close"], P["open"]
    warm = P["i"] >= 250
    in_s = (P["sess"] > 0) & warm & P["atr"].notna()
    up = (P["h1_gap"] >= C.H1_GAP_MIN) & (P["ema20"] > P["ema50"])
    dn = (P["h1_gap"] <= -C.H1_GAP_MIN) & (P["ema20"] < P["ema50"])
    pb_b = in_s & up & (c.shift(1) < P["ema20"].shift(1)) & (c > P["ema20"]) & (c > o)
    pb_s = in_s & dn & (c.shift(1) > P["ema20"].shift(1)) & (c < P["ema20"]) & (c < o)
    after = in_s & (P["msince"] > C.OR_MIN) & (P["msince"] <= C.BREAKOUT_MINUTES)
    bo_b = after & (c > P["or_high"]) & (c.shift(1) <= P["or_high"]) & ((c - P["or_high"]) < P["atr"])
    bo_s = after & (c < P["or_low"]) & (c.shift(1) >= P["or_low"]) & ((P["or_low"] - c) < P["atr"])

    parts = []
    for mask, setup, side in ((pb_b, 1, 1), (pb_s, 1, -1), (bo_b, 2, 1), (bo_s, 2, -1)):
        s = P[mask]
        if setup == 2:
            s = s.groupby("skey").head(1)          # first breakout of the session only
        if len(s):
            parts.append(s.assign(setup=setup, side=side))
    if not parts:
        return pd.DataFrame()
    s = pd.concat(parts).sort_index(kind="stable")
    f = _features(s, symbol)
    out = pd.concat([s[["i", "atr", "close"]], f], axis=1)
    out["symbol"] = symbol
    out = out[out["cost_r"] <= C.MAX_COST_R]
    if cooldown:
        keep, last = [], {}
        gap = pd.Timedelta(minutes=C.TF_MIN * C.COOLDOWN_BARS)
        for t, sd in zip(out.index, out["side"]):
            lt = last.get(sd)
            ok = lt is None or (t - lt) >= gap
            keep.append(ok)
            if ok:
                last[sd] = t
        out = out[keep]
    return out


def random_cands(P, n, seed):
    """Random entries inside sessions - the benchmark any real rule has to beat."""
    rng = np.random.default_rng(seed)
    ok = P[(P["sess"] > 0) & P["atr"].notna() & (P["i"] >= 250)]
    pick = ok.sample(n=min(n, len(ok)), random_state=seed)
    pick = pick.assign(side=rng.choice([-1, 1], len(pick)))
    return pick[["i", "atr", "side"]].sort_index()


# ----------------------------------------------------------------- analysis
def analyze(P):
    r = P.iloc[-1]
    g = r["h1_gap"]
    trend = "UP" if g >= C.H1_GAP_MIN else "DOWN" if g <= -C.H1_GAP_MIN else "RANGE"
    return {"close": r["close"], "trend": trend, "h1_gap": g, "er": r["er"], "atr_pct": r["atr_pct"],
            "pdh": r["pdh"], "pdl": r["pdl"], "or_high": r["or_high"], "or_low": r["or_low"],
            "sess_open": r["sess_open"], "sess": int(r["sess"]), "msince": r["msince"], "skey": r["skey"]}
