"""
Proves Option B works exactly as intended, using the real
build_confirmed_position() function -- not a reimplementation:
- target_price is preserved as strategy.py's dynamic R:R value
  (entry + risk * RISK_REWARD_MIN), NOT overwritten with a flat
  PROFIT_TARGET_PERCENT.
- stop_price IS still recomputed from the confirmed fill price and
  STOP_LOSS_PERCENT, exactly as before.
"""

from dataclasses import dataclass
from typing import Optional

from entry_protection import build_confirmed_position

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


@dataclass
class FakeSignal:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    target: float
    timestamp: object = None
    reason: str = "test"
    confidence: Optional[str] = None
    market_alignment: Optional[str] = None
    news_sentiment: Optional[str] = None
    news_headline: Optional[str] = None
    news_confidence_score: Optional[float] = None
    price_action_score: Optional[float] = None
    price_action_detail: Optional[dict] = None


class FakeCfg:
    PAPER_TRADING = True
    ENABLE_FIXED_TARGET = True
    STOP_LOSS_PERCENT = 0.45
    PROFIT_TARGET_PERCENT = 0.70  # the OLD flat value -- must NOT end up as the actual target
    RISK_PER_TRADE_PCT = 1.0
    CAPITAL = 100000.0


# -- BUY: entry=100, risk-based target at 3x R:R (risk=0.5 -> target=101.5) --

cfg = FakeCfg()
entry_price = 100.0
risk_based_target = 101.5  # e.g. from RISK_REWARD_MIN=3.0 against a 0.5 stop distance
signal = FakeSignal(
    symbol="TEST", direction="BUY", entry_price=entry_price,
    stop_loss=99.5, target=risk_based_target,
)
entry_result = {"filled_quantity": 10, "average_price": None}  # paper fill -> falls back to signal.entry_price

position = build_confirmed_position(signal, entry_result, "NSE", cfg)

check("BUY: target_price preserved as the dynamic R:R value (101.5), NOT the flat 0.7% (100.7)",
      abs(position["target"] - risk_based_target) < 1e-9)
check("BUY: target_price is NOT the old flat PROFIT_TARGET_PERCENT value (100.7)",
      abs(position["target"] - 100.70) > 0.01)
expected_stop = entry_price * (1 - cfg.STOP_LOSS_PERCENT / 100)  # 99.55
check("BUY: stop_price IS still recomputed from STOP_LOSS_PERCENT against the confirmed fill (99.55), "
      "not left as the original signal.stop_loss (99.5)",
      abs(position["stop"] - expected_stop) < 1e-9)

# -- SELL: mirrored ------------------------------------------------------------

cfg2 = FakeCfg()
entry_price_s = 200.0
risk_based_target_s = 194.0  # e.g. risk=2.0 * RISK_REWARD_MIN=3.0
signal_s = FakeSignal(
    symbol="TEST2", direction="SELL", entry_price=entry_price_s,
    stop_loss=202.0, target=risk_based_target_s,
)
entry_result_s = {"filled_quantity": 5, "average_price": None}
position_s = build_confirmed_position(signal_s, entry_result_s, "NSE", cfg2)

check("SELL: target_price preserved as the dynamic R:R value (194.0)",
      abs(position_s["target"] - risk_based_target_s) < 1e-9)
expected_stop_s = entry_price_s * (1 + cfg2.STOP_LOSS_PERCENT / 100)  # 200.9
check("SELL: stop_price still recomputed from STOP_LOSS_PERCENT (200.9)",
      abs(position_s["stop"] - expected_stop_s) < 1e-9)

# -- ENABLE_FIXED_TARGET=False -> both stop and target come straight from --
# -- the signal, completely unaffected by this change ------------------------

cfg3 = FakeCfg()
cfg3.ENABLE_FIXED_TARGET = False
signal3 = FakeSignal(symbol="TEST3", direction="BUY", entry_price=100.0, stop_loss=98.0, target=106.0)
entry_result3 = {"filled_quantity": 10, "average_price": None}
position3 = build_confirmed_position(signal3, entry_result3, "NSE", cfg3)
check("ENABLE_FIXED_TARGET=False -> stop and target both come straight from the signal, unaffected",
      position3["stop"] == 98.0 and position3["target"] == 106.0)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
