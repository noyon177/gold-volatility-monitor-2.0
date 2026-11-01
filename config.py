"""All settings in one place. Every value can be overridden with an environment variable."""
import os


def _f(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


SYMBOLS = {
    "XAUUSD": {"td": "XAU/USD", "spread": _f("XAU_SPREAD", 0.30)},   # price units, use your broker's typical spread
    "BTCUSD": {"td": "BTC/USD", "spread": _f("BTC_SPREAD", 15.0)},
}

# Sessions use LOCAL clock time, so daylight-saving changes are handled automatically.
# (timezone, start minute of day, end minute of day)
LONDON = ("Europe/London", 8 * 60, 16 * 60 + 30)
NEWYORK = ("America/New_York", 9 * 60 + 30, 16 * 60)
OR_MIN = 30              # opening range = first 30 minutes of each session
BREAKOUT_MINUTES = 180   # breakouts only count in the first 3 hours of a session

# Trade model (one simple, fixed exit: no partial profits, no break-even games)
TF_MIN = 15
SL_ATR = _f("SL_ATR", 1.5)       # stop distance in 15M ATR
RR = _f("RR", 2.0)               # take-profit = RR x risk
MAX_BARS = 24                    # time stop: 24 x 15M = 6 hours
COOLDOWN_BARS = 8                # min gap between two signals of the same side (2 hours)
H1_GAP_MIN = 0.25                # 1H trend strength needed for pullback setups (in 1H ATR)
MAX_COST_R = _f("MAX_COST_R", 0.20)   # skip if spread > this fraction of the risk
EXTRA_COST_R = _f("EXTRA_COST_R", 0.0)

# Validation gates: a model/rule set is deployed ONLY if it passes all of them on unseen data
MIN_KEPT = 30
MIN_AVG_R = 0.05
MIN_T = 1.64

MODEL_PATH = os.environ.get("MODEL_PATH", "model.joblib")
