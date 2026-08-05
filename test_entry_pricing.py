from entry_pricing import (
    best_price_and_depth, compute_spread_pct, compute_marketable_limit_price,
    evaluate_entry_price, check_tick_freshness,
)

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1


class FakeCfg:
    WS_ENTRY_TICK_MAX_AGE_SECONDS = 2.0
    WS_MAX_SPREAD_PCT = 0.5
    WS_MAX_SLIPPAGE_PCT = 0.15
    MAX_ADVERSE_MOVE_PCT = None
    MAX_ABSOLUTE_DRIFT_PCT = None


class FakeGapTracker:
    def __init__(self, age_seconds):
        self._age = age_seconds

    def seconds_since_last_tick(self, symbol, now=None):
        return self._age


depth_normal = {
    "buy": [{"price": 100.00, "quantity": 500, "orders": 3}],
    "sell": [{"price": 100.10, "quantity": 400, "orders": 2}],
}

# -- best_price_and_depth -------------------------------------------------

p, q = best_price_and_depth(depth_normal, "BUY")
check("BUY uses best ASK price", p == 100.10)
check("BUY depth qty comes from ask side", q == 400)

p, q = best_price_and_depth(depth_normal, "SELL")
check("SELL uses best BID price", p == 100.00)
check("SELL depth qty comes from bid side", q == 500)

p, q = best_price_and_depth({}, "BUY")
check("Empty depth returns (None, None)", p is None and q is None)

# -- compute_spread_pct ----------------------------------------------------

spread = compute_spread_pct(depth_normal)
expected_spread = (100.10 - 100.00) / 100.05 * 100
check("Spread percentage computed correctly", abs(spread - expected_spread) < 1e-9)

check("Missing one side of book returns None spread",
      compute_spread_pct({"buy": depth_normal["buy"]}) is None)

# -- compute_marketable_limit_price ----------------------------------------

buy_limit = compute_marketable_limit_price("BUY", 100.0, 0.15)
check("BUY limit price is capped ABOVE best ask", buy_limit == round(100.0 * 1.0015, 2))

sell_limit = compute_marketable_limit_price("SELL", 100.0, 0.15)
check("SELL limit price is capped BELOW best bid", sell_limit == round(100.0 * 0.9985, 2))

# -- check_tick_freshness ---------------------------------------------------

check("Fresh tick (0.5s old) passes freshness check",
      check_tick_freshness(FakeGapTracker(0.5), "SYM", 2.0) is None)
check("Stale tick (5s old) fails freshness check",
      check_tick_freshness(FakeGapTracker(5.0), "SYM", 2.0) is not None)
check("No tick ever seen fails freshness check",
      check_tick_freshness(FakeGapTracker(None), "SYM", 2.0) is not None)

# -- evaluate_entry_price: full pipeline ------------------------------------

cfg = FakeCfg()

result = evaluate_entry_price("SYM", "BUY", signal_price=100.05, depth=depth_normal,
                               gap_tracker=FakeGapTracker(0.5), cfg=cfg)
check("Full pipeline approves a clean BUY", result.approved)
check("Approved result carries a limit_price", result.limit_price is not None)
check("BUY limit_price is at or above best ask", result.limit_price >= 100.10)

result_stale = evaluate_entry_price("SYM", "BUY", signal_price=100.05, depth=depth_normal,
                                     gap_tracker=FakeGapTracker(10.0), cfg=cfg)
check("Stale tick rejects before even reading depth", not result_stale.approved)
check("Stale rejection reason mentions staleness", "old" in result_stale.reason)

wide_spread_depth = {
    "buy": [{"price": 95.0, "quantity": 100, "orders": 1}],
    "sell": [{"price": 105.0, "quantity": 100, "orders": 1}],
}
result_wide = evaluate_entry_price("SYM", "BUY", signal_price=100.0, depth=wide_spread_depth,
                                    gap_tracker=FakeGapTracker(0.5), cfg=cfg)
check("Wide spread beyond WS_MAX_SPREAD_PCT rejects", not result_wide.approved)

result_empty_depth = evaluate_entry_price("SYM", "BUY", signal_price=100.0, depth={},
                                           gap_tracker=FakeGapTracker(0.5), cfg=cfg)
check("Empty depth rejects with a clear reason", not result_empty_depth.approved and "depth" in result_empty_depth.reason)

# adverse-move check, when explicitly configured
cfg_with_adverse = FakeCfg()
cfg_with_adverse.MAX_ADVERSE_MOVE_PCT = 0.05
result_adverse = evaluate_entry_price("SYM", "BUY", signal_price=100.0, depth=depth_normal,
                                       gap_tracker=FakeGapTracker(0.5), cfg=cfg_with_adverse)
check("Adverse move beyond configured MAX_ADVERSE_MOVE_PCT rejects when explicitly set",
      not result_adverse.approved)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
