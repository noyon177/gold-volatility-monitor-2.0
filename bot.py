"""Live bot. Runs every 5 minutes (GitHub Actions). Works only inside London / New York sessions.
 - once per session: market analysis (trend, momentum, volatility, key levels, opening range)
 - every closed 15M candle: look for setups, journal every candidate (shadow or real), resolve outcomes
 - trade signals are sent ONLY if model.joblib exists and passed validation (research.py); otherwise OBSERVE mode."""
import os
import sys

import numpy as np
import pandas as pd
import requests

import config as C
import data
import simulate as SIM
import strategy as S

JOURNAL, STATE = "journal.csv", "state.json"
_BASE = ["id", "symbol", "time", "setup", "side", "signaled", "proba", "status", "risk", "entry",
         "result", "r", "exit_time"]
JCOLS = _BASE + [f for f in S.FEATURES if f not in _BASE]
SESS_NAME = {1: "লন্ডন", 2: "নিউ ইয়র্ক"}


def send(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print(text, "\n---"); return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": text}, timeout=30)
    except requests.RequestException as e:
        print("telegram error:", e)


def load_art():
    if not os.path.exists(C.MODEL_PATH):
        return None
    try:
        import joblib
        return joblib.load(C.MODEL_PATH)
    except Exception as e:
        print("model load failed:", e); return None


def gate(art, row):
    """(signal?, probability). No validated model -> never a trade signal."""
    if art is None or not art.get("validated"):
        return False, np.nan
    if not art.get("use_ml"):
        return True, np.nan
    p = float(art["model"].predict_proba(row[S.FEATURES].to_numpy(float).reshape(1, -1))[0, 1])
    return p >= art["threshold"], p


def _min(ts):
    return np.datetime64(ts.tz_convert("UTC").tz_localize(None), "m").astype("int64")


def resolve_open(jr, sym, P, notify):
    A = SIM.bars(P)
    for ix in jr.index[(jr["symbol"] == sym) & (jr["status"] == "OPEN")]:
        row = jr.loc[ix]
        tm = _min(pd.Timestamp(row["time"]))
        j = int(np.searchsorted(A["st"], tm))
        if j >= len(A["st"]):
            continue                                    # entry bar not formed yet
        if A["st"][j] != tm:
            jr.loc[ix, "status"] = "CANCEL"; continue
        entry = float(A["o"][j])
        status, r, _, k = SIM.run_trade(A, j, int(row["side"]), float(row["risk"]))
        jr.loc[ix, "entry"] = entry
        if status == "OPEN":
            continue
        net = r - C.SYMBOLS[sym]["spread"] / float(row["risk"]) - C.EXTRA_COST_R
        jr.loc[ix, ["status", "result", "r", "exit_time"]] = ["CLOSED", status, round(net, 3), P.index[k].isoformat()]
        if int(row["signaled"]) == 1:
            icon = "✅" if net > 0 else "❌"
            notify(f"{icon} {sym} {'BUY' if int(row['side']) > 0 else 'SELL'} বন্ধ: {status} · ফল {net:+.2f}R (স্প্রেড বাদে)")
    return jr


def analysis_text(sym, a, art, jr):
    tr = {"UP": "ঊর্ধ্বমুখী (1H)", "DOWN": "নিম্নমুখী (1H)", "RANGE": "সাইডওয়ে/অস্পষ্ট (1H)"}[a["trend"]]
    er = "ট্রেন্ডিং" if a["er"] >= 0.35 else "চপি/রেঞ্জ" if a["er"] < 0.2 else "মিশ্র"
    vp = a["atr_pct"]
    vol = "উচ্চ" if vp >= 0.8 else "নিম্ন" if vp <= 0.2 else "স্বাভাবিক"
    px = a["close"]
    mid = (a["or_high"] + a["or_low"]) / 2 if pd.notna(a["or_high"]) else np.nan
    bias = "নিরপেক্ষ"
    if pd.notna(mid):
        if a["trend"] == "UP" and px > mid:
            bias = "বাই-সাইডে ঝোঁক (ট্রেন্ড ঊর্ধ্ব + রেঞ্জের ওপরে)"
        elif a["trend"] == "DOWN" and px < mid:
            bias = "সেল-সাইডে ঝোঁক (ট্রেন্ড নিম্ন + রেঞ্জের নিচে)"
        elif a["trend"] != "RANGE":
            bias = "ট্রেন্ড আর দাম মিলছে না, সতর্ক থাকুন"
    mode = ("সক্রিয় (যাচাইকৃত মডেল)" if art is not None and art.get("validated")
            else "পর্যবেক্ষণ মোড (ট্রেড সিগন্যাল বন্ধ, শুধু বিশ্লেষণ)")
    closed = jr[(jr["status"] == "CLOSED") & (jr["symbol"] == sym)]
    shadow = f"\nলাইভ শ্যাডো ফল: {len(closed)} ট্রেড, গড় {closed['r'].astype(float).mean():+.2f}R" if len(closed) >= 10 else ""
    f = lambda v: "-" if pd.isna(v) else f"{v:,.2f}"
    return (f"📊 {sym} · {SESS_NAME[a['sess']]} সেশন বিশ্লেষণ\n"
            f"দাম: {f(px)}\n1H ট্রেন্ড: {tr}\n15M গতি: {er} (ER {a['er']:.2f})\nভোলাটিলিটি: {vol} ({vp * 100:.0f} পার্সেন্টাইল)\n"
            f"গত দিনের হাই/লো: {f(a['pdh'])} / {f(a['pdl'])}\nওপেনিং রেঞ্জ: {f(a['or_low'])} – {f(a['or_high'])}\n"
            f"বায়াস: {bias}\nমোড: {mode}{shadow}")


def signal_text(sym, row, p, risk):
    buy = int(row["side"]) > 0
    px = float(row["close"])
    sl, tp = px - (1 if buy else -1) * risk, px + (1 if buy else -1) * C.RR * risk
    prob = f"\nমডেল সম্ভাব্যতা: {p:.0%}" if pd.notna(p) else ""
    return (f"🔔 {sym} {'BUY' if buy else 'SELL'} · 15M · {S.SETUP_NAMES[int(row['setup'])]}\n"
            f"এন্ট্রি: মার্কেটে ≈ {px:,.2f}\nSL: {sl:,.2f} (ঝুঁকি {risk:,.2f})\nTP: {tp:,.2f} (1:{C.RR:g})\n"
            f"সময়সীমা: {C.MAX_BARS * C.TF_MIN // 60} ঘণ্টা{prob}\nঝুঁকি আপনার ব্যালেন্সের ছোট অংশ রাখুন; এটা গ্যারান্টি নয়।")


def run_once(now, fetch, notify, art, jr, st):
    sess_now, _ = S.session_now(now)
    for sym, cfg in C.SYMBOLS.items():
        has_open = ((jr["symbol"] == sym) & (jr["status"] == "OPEN")).any()
        if sess_now == 0 and not has_open:
            continue                                     # outside sessions: no API call at all
        df5 = fetch(cfg["td"])
        df5 = df5[df5.index + pd.Timedelta(minutes=5) <= now]
        P = S.prepare(df5, sym)
        if P.empty:
            continue
        jr = resolve_open(jr, sym, P, notify)
        last_t = P.index[-1]
        if sess_now == 0 or now - last_t > pd.Timedelta(minutes=25):
            continue
        a = S.analyze(P)
        akey = f"{sym}|{a['skey']}"
        if a["sess"] > 0 and a["msince"] >= C.OR_MIN and akey not in st["analysis"]:
            notify(analysis_text(sym, a, art, jr))
            st["analysis"] = (st["analysis"] + [akey])[-60:]
        cand = S.candidates(P, sym, cooldown=False)
        cur = cand[cand.index == last_t] if len(cand) else cand
        for t, row in cur.iterrows():
            cid = f"{sym}|{t.isoformat()}|{int(row['setup'])}|{int(row['side'])}"
            if (jr["id"] == cid).any():
                continue
            same = jr[(jr["symbol"] == sym) & (jr["side"].astype(float) == row["side"])]
            if len(same) and t - pd.to_datetime(same["time"], utc=True).max() < pd.Timedelta(minutes=C.TF_MIN * C.COOLDOWN_BARS):
                continue
            ok, p = gate(art, row)
            risk = C.SL_ATR * float(row["atr"])
            new = {"id": cid, "symbol": sym, "time": t.isoformat(), "setup": int(row["setup"]), "side": int(row["side"]),
                   "signaled": int(ok), "proba": p, "status": "OPEN", "risk": risk, "entry": np.nan,
                   "result": "", "r": np.nan, "exit_time": ""}
            new.update({k: row[k] for k in S.FEATURES})
            jr = pd.concat([jr, pd.DataFrame([new])], ignore_index=True)
            if ok:
                notify(signal_text(sym, row, p, risk))
    return jr, st


def main():
    import json
    jr = pd.read_csv(JOURNAL) if os.path.exists(JOURNAL) else pd.DataFrame(columns=JCOLS)
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"analysis": []}
    now = pd.Timestamp.now(tz="UTC")
    try:
        jr, st = run_once(now, lambda s: data.fetch(s, 3000), send, load_art(), jr, st)
    except RuntimeError as e:
        print("data error:", e); sys.exit(0)
    jr.to_csv(JOURNAL, index=False)
    json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()
