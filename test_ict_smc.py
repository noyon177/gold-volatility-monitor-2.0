"""Offline tests (no network): python -m unittest test_ict_smc -v"""
import datetime as dt
import random
import unittest
from pathlib import Path
from unittest import mock

import ict_smc_market_reader_v3 as bot

UTC = dt.timezone.utc


def mk(t, o, h, l, c):
    return {"time": t.strftime("%Y-%m-%d %H:%M:%S"), "dt": t, "open": o, "high": h, "low": l, "close": c}


def make_trade(side="BUY", mode="LIMIT", entry=100.0, risk=2.0, rr=3.0):
    sgn = 1 if side == "BUY" else -1
    start = dt.datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    plan = {
        "time": "2026-10-05 09:45:00", "side": side, "mode": mode, "entry": entry,
        "sl": entry - sgn * risk, "tp1": entry + sgn * risk, "tp": entry + sgn * risk * rr,
        "rr": rr, "risk": risk, "score": 10, "close_dt": start,
    }
    sym = bot.Symbol("TEST", "T/T", digits=2)
    return bot.new_trade(plan, sym, start), start


def candles5(start, rows):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(mk(start + dt.timedelta(minutes=5 * i), o, h, l, c))
    return out


class TradeLifecycle(unittest.TestCase):
    def test_limit_fill_tp1_then_breakeven(self):
        tr, s = make_trade("BUY", "LIMIT")
        cs = candles5(s, [(101, 101.5, 100.5, 101),      # not touched
                          (101, 101, 99.9, 100.5),       # fill at 100
                          (100.5, 102.1, 100.4, 102),    # TP1 (102) hit
                          (102, 102.2, 99.8, 100)])      # back to entry -> BE
        ev = [e["type"] for e in bot.advance_trade(tr, cs, 5)]
        self.assertEqual(ev, ["FILLED", "TP1", "BE"])
        self.assertAlmostEqual(tr["r"], 0.5)             # 50% at 1R + 50% at 0R

    def test_full_tp(self):
        tr, s = make_trade("BUY", "MARKET")
        cs = candles5(s, [(100, 102.5, 99.5, 102), (102, 106.5, 101.9, 106)])
        ev = [e["type"] for e in bot.advance_trade(tr, cs, 5)]
        self.assertEqual(ev, ["TP1", "TP"])
        self.assertAlmostEqual(tr["r"], 0.5 * 1 + 0.5 * 3)

    def test_sl_and_sell_side(self):
        tr, s = make_trade("SELL", "MARKET")
        cs = candles5(s, [(100, 102.1, 99.5, 101)])
        bot.advance_trade(tr, cs, 5)
        self.assertEqual(tr["result"], "SL")
        self.assertAlmostEqual(tr["r"], -1.0)

    def test_same_candle_sl_wins(self):
        tr, s = make_trade("BUY", "MARKET")
        cs = candles5(s, [(100, 110, 90, 100)])
        bot.advance_trade(tr, cs, 5)
        self.assertEqual(tr["result"], "SL")

    def test_pending_missed_and_expired(self):
        tr, s = make_trade("BUY", "LIMIT")
        ev = bot.advance_trade(tr, candles5(s, [(101, 102.5, 100.5, 102)]), 5)
        self.assertEqual(ev[-1]["reason"], "MISSED")
        tr2, s = make_trade("BUY", "LIMIT")
        ev = bot.advance_trade(tr2, [], 5, now=s + dt.timedelta(minutes=bot.LIMIT_EXPIRE_MIN + 1))
        self.assertEqual(ev[-1]["reason"], "EXPIRED")

    def test_limit_fill_candle_checks_sl_only(self):
        tr, s = make_trade("BUY", "LIMIT")
        # fill and a big high on the same candle must NOT count as TP
        bot.advance_trade(tr, candles5(s, [(104, 110, 99.9, 101)]), 5)
        self.assertEqual(tr["status"], "OPEN")


class PlanAndSizing(unittest.TestCase):
    def _signal(self, side="BUY", **kw):
        s = {"side": side, "atr": 1.0, "ob": None, "fvg": None,
             "sr": {"resistance": [], "support": []}, "key_levels": [], "eq": {}}
        s.update(kw)
        return s

    def _candles(self, price=100.0):
        t = dt.datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
        return [mk(t + dt.timedelta(minutes=15 * i), price, price + 0.5, price - 1.0, price) for i in range(10)]

    def test_limit_entry_and_structural_tp(self):
        sig = self._signal(ob={"side": "BULLISH", "low": 98.0, "high": 99.6},
                           key_levels=[{"name": "PDH", "price": 106.0, "kind": "high"}])
        plan, why = bot.make_trade_plan(sig, self._candles())
        self.assertIsNone(why)
        self.assertEqual(plan["mode"], "LIMIT")
        self.assertAlmostEqual(plan["entry"], 98.8)
        self.assertIn("PDH", plan["tp_label"])
        self.assertGreaterEqual(plan["rr"], bot.MIN_RR)

    def test_close_target_is_rejected_and_open_air_uses_fixed_rr(self):
        near = self._signal(key_levels=[{"name": "PDH", "price": 101.5, "kind": "high"}])
        plan, why = bot.make_trade_plan(near, self._candles())
        self.assertIsNone(plan)
        self.assertIn("target", why)
        plan, why = bot.make_trade_plan(self._signal(), self._candles())
        self.assertAlmostEqual(plan["rr"], bot.RISK_REWARD)

    def test_far_target_is_capped(self):
        sig = self._signal(key_levels=[{"name": "PDH", "price": 150.0, "kind": "high"}])
        plan, _ = bot.make_trade_plan(sig, self._candles())
        self.assertAlmostEqual(plan["rr"], bot.MAX_RR)

    def test_position_size(self):
        gold = bot.Symbol("XAUUSD", "XAU/USD", contract_size=100.0)
        with mock.patch.object(bot, "ACCOUNT_BALANCE", 1000.0), mock.patch.object(bot, "RISK_PERCENT", 1.0):
            pos = bot.position_size(gold, 5.0)       # $10 risk / (5 * 100) = 0.02 lot
            self.assertAlmostEqual(pos["lots"], 0.02)
            self.assertIsNotNone(bot.position_size(gold, 500.0)["warn"])


class TimeAndLevels(unittest.TestCase):
    def test_market_open_gold(self):
        gold = bot.Symbol("XAUUSD", "XAU/USD", market="FX")
        btc = bot.Symbol("BTCUSD", "BTC/USD", market="CRYPTO")
        sat = dt.datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
        wed = dt.datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        self.assertFalse(bot.market_open(gold, sat))
        self.assertTrue(bot.market_open(gold, wed))
        self.assertTrue(bot.market_open(btc, sat))
        # Friday 17:30 NY (EDT = UTC-4) -> 21:30 UTC closed
        self.assertFalse(bot.market_open(gold, dt.datetime(2026, 10, 9, 21, 30, tzinfo=UTC)))

    def test_killzone_follows_new_york_dst(self):
        summer = dt.datetime(2026, 7, 1, 11, 30, tzinfo=UTC)   # 07:30 EDT
        winter = dt.datetime(2026, 12, 1, 11, 30, tzinfo=UTC)  # 06:30 EST
        self.assertEqual(bot.session_name(summer), "New York KZ")
        self.assertEqual(bot.session_name(winter), "Off-hours")

    def test_key_levels(self):
        start = dt.datetime(2026, 10, 4, 22, 0, tzinfo=UTC)    # Sunday 18:00 NY = week open
        cs, p = [], 100.0
        for i in range(48 * 4):
            t = start + dt.timedelta(minutes=30 * i)
            cs.append(mk(t, p, p + 1, p - 1, p + 0.1))
            p += 0.1
        now = cs[-1]["dt"] + dt.timedelta(minutes=30)
        lv = {k["name"]: k for k in bot.key_levels(cs, now)}
        for name in ("PDH", "PDL", "DAILY_OPEN", "WEEKLY_OPEN", "ASIA_H", "LONDON_L"):
            self.assertIn(name, lv, name)
        self.assertGreater(lv["PDH"]["price"], lv["PDL"]["price"])
        self.assertAlmostEqual(lv["WEEKLY_OPEN"]["price"], 100.0)

    def test_news_block(self):
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        ev = [{"time": (now + dt.timedelta(minutes=20)).isoformat(), "title": "CPI"}]
        self.assertIsNotNone(bot.news_block(now, ev))
        self.assertIsNone(bot.news_block(now + dt.timedelta(hours=2), ev))


class SweepAndRetry(unittest.TestCase):
    def test_key_level_sweep_detected(self):
        t0 = dt.datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
        cs = [mk(t0 + dt.timedelta(minutes=15 * i), 100, 100.6, 99.5, 100.1) for i in range(60)]
        cs[-2] = mk(cs[-2]["dt"], 100, 100.2, 97.9, 99.4)     # pokes under 98.5, closes back above
        cs[-1] = mk(cs[-1]["dt"], 99.4, 100.2, 99.2, 100.0)
        sw = bot.detect_liquidity_sweep(cs, [{"name": "PDL", "price": 98.5, "kind": "low"}])
        self.assertEqual(sw["side"], "SELL_SIDE")
        self.assertTrue(sw["key"])

    def test_retry_backoff(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise bot.RetryableError("boom", wait=0)
            return "ok"

        with mock.patch.object(bot.time, "sleep"):
            self.assertEqual(bot.with_retry(flaky, tries=4), "ok")
        self.assertEqual(calls["n"], 3)


def synthetic(days=14, seed=7, start_price=2000.0):
    rnd = random.Random(seed)
    t = dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    out, p, vol = [], start_price, 0.8
    for _ in range(days * 288):
        vol = max(0.3, min(2.0, vol + rnd.gauss(0, 0.05)))
        o = p
        c = o + rnd.gauss(0.0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol * 0.4))
        l = min(o, c) - abs(rnd.gauss(0, vol * 0.4))
        out.append(mk(t, o, h, l, c))
        p = c
        t += dt.timedelta(minutes=5)
    return out


def resample(c5, minutes):
    out, bucket = [], []
    n = minutes // 5
    for c in c5:
        if c["dt"].timestamp() // (minutes * 60) != (bucket[0]["dt"].timestamp() // (minutes * 60) if bucket else None):
            if len(bucket) == n:
                out.append(mk(bucket[0]["dt"], bucket[0]["open"], max(x["high"] for x in bucket),
                              min(x["low"] for x in bucket), bucket[-1]["close"]))
            bucket = []
        bucket.append(c)
    return out


class BacktestSmoke(unittest.TestCase):
    def test_backtest_runs_and_reports(self):
        c5 = synthetic()
        d15, d30, d4 = resample(c5, 15), resample(c5, 30), resample(c5, 240)
        sym = bot.Symbol("SYN", "SYN/USD", digits=2, market="CRYPTO")
        trades = bot.backtest_symbol(sym, d4, d30, d15, c5, days=6)
        for t in trades:
            self.assertIn(t["result"], ("TP", "SL", "BE", "TIMEOUT", "CANCELLED"))
        text = bot.backtest_report("SYN", trades)
        self.assertIn("BACKTEST SYN", text)
        print("\n" + text)

    def test_analyze_data_smoke_and_chart(self):
        c5 = synthetic(days=16, seed=3)
        d15, d30, d4 = resample(c5, 15), resample(c5, 30), resample(c5, 240)
        now = d15[-1]["dt"] + dt.timedelta(minutes=15)
        sym = bot.Symbol("SYN", "SYN/USD", digits=2, market="CRYPTO")
        res = bot.analyze_data(sym, d4[-180:], d30[-400:], d15[-220:], c5[-180:], now)
        self.assertIn("bias", res)
        plan, _ = bot.make_trade_plan(
            {"side": "BUY", "atr": 1.5, "ob": None, "fvg": None, "sr": {"resistance": [], "support": []},
             "key_levels": res["key_levels"], "eq": res.get("eq"), "score": 10, "context": 4, "trigger": 4,
             "confirm": 2, "reasons": ["x"], "conflicts": [], "pd": {"zone": "DISCOUNT"}, "sweep": None,
             "m15_structure": {"bos": None, "choch": None}}, d15[-220:])
        if plan:
            png = bot.build_chart(sym, plan, d15[-220:])
            if png is not None:
                self.assertTrue(png.startswith(b"\x89PNG"))


class LogAndStats(unittest.TestCase):
    def test_csv_roundtrip_and_stats(self):
        p = Path("test_log.csv")
        p.unlink(missing_ok=True)
        try:
            tr, s = make_trade("BUY", "MARKET")
            bot.advance_trade(tr, candles5(s, [(100, 102.5, 99.5, 102), (102, 106.5, 101.9, 106)]), 5)
            bot.log_trade(tr, p)
            rows = bot.read_log(p)
            self.assertEqual(rows[0]["result"], "TP")
            self.assertIn("Total", bot.stats_text("t", rows))
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
