from daily_report import match_trade_to_signal, _group_stats, build_filter_effectiveness, build_trade_reasons

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

# --- match_trade_to_signal ---
trade = {"symbol": "TEST", "entry": 100.0}
signals = [
    {"symbol": "TEST", "entry_price": 100.2},
    {"symbol": "OTHER", "entry_price": 100.0},
]
match = match_trade_to_signal(trade, signals)
check("Matches by symbol + close entry price (within 0.5)", match is not None and match["entry_price"] == 100.2)

no_match_trade = {"symbol": "NOMATCH", "entry": 50.0}
check("Returns None when no candidate matches", match_trade_to_signal(no_match_trade, signals) is None)

# --- _group_stats ---
rows = [
    {"net_pnl": 5.0}, {"net_pnl": -2.0}, {"net_pnl": 3.0}, {"net_pnl": -1.0},
]
grouped = _group_stats(rows, lambda r: "all", min_group_size=3)
check("Group stats: correct count", grouped["all"]["count"] == 4)
check("Group stats: correct win rate (2/4=50%)", grouped["all"]["win_rate_pct"] == 50.0)
check("Group stats: correct total pnl", grouped["all"]["total_pnl"] == 5.0)
check("Group stats: not flagged low-sample when count >= min_group_size", grouped["all"]["low_sample"] == False)

small_rows = [{"net_pnl": 1.0}, {"net_pnl": -1.0}]
grouped_small = _group_stats(small_rows, lambda r: "tiny", min_group_size=5)
check("Group stats: correctly flags low sample when below threshold", grouped_small["tiny"]["low_sample"] == True)

# --- build_filter_effectiveness on empty date ---
empty_result = build_filter_effectiveness("1999-01-01")
check("No trades for a date returns trade_count=0 with a clear reason", empty_result["trade_count"] == 0)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
