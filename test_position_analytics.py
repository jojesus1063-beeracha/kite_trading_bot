import time
from position_analytics import (
    compute_gross_pnl, compute_profit_pct, compute_distances,
    compute_reward_risk, compute_spread, update_mfe_mae, classify_status
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

# --- Gross PnL ---
check("BUY gross PnL: (110-100)*10 = 100", compute_gross_pnl("BUY", 100, 110, 10) == 100)
check("BUY gross PnL negative when price drops", compute_gross_pnl("BUY", 100, 95, 10) == -50)
check("SELL gross PnL: (100-90)*10 = 100", compute_gross_pnl("SELL", 100, 90, 10) == 100)
check("SELL gross PnL negative when price rises", compute_gross_pnl("SELL", 100, 105, 10) == -50)

# --- Profit % ---
check("BUY profit %: 10% up", abs(compute_profit_pct("BUY", 100, 110) - 10.0) < 0.001)
check("SELL profit %: 10% down = +10% for short", abs(compute_profit_pct("SELL", 100, 90) - 10.0) < 0.001)
check("Zero entry price returns 0 (fail-safe)", compute_profit_pct("BUY", 0, 100) == 0.0)

# --- Distances ---
d = compute_distances("BUY", current=105, stop=95, target=115)
check("BUY distance to stop (INR): 105-95=10", d["distance_to_stop_inr"] == 10)
check("BUY distance to target (INR): 115-105=10", d["distance_to_target_inr"] == 10)
d_sell = compute_distances("SELL", current=95, stop=105, target=85)
check("SELL distance to stop (INR): 105-95=10", d_sell["distance_to_stop_inr"] == 10)
check("SELL distance to target (INR): 95-85=10", d_sell["distance_to_target_inr"] == 10)

# --- Reward:Risk (entry plan and remaining from current price) ---
rr = compute_reward_risk("BUY", entry=100, current=105, stop=95, target=115)
check("BUY entry reward: 115-100=15", rr["entry_reward"] == 15)
check("BUY entry risk: 100-95=5", rr["entry_risk"] == 5)
check("BUY entry reward:risk ratio: 15/5=3.0", rr["entry_reward_risk"] == 3.0)
check("BUY reward remaining: 115-105=10", rr["reward_remaining"] == 10)
check("BUY risk remaining: 105-95=10", rr["risk_remaining"] == 10)
check("BUY live reward:risk ratio: 10/10=1.0", rr["remaining_reward_risk"] == 1.0)
check("Legacy reward_risk remains the live ratio", rr["reward_risk"] == 1.0)
rr_zero_risk = compute_reward_risk("BUY", entry=100, current=95, stop=95, target=115)
check("Reward:risk is None when risk_remaining is 0 (fail-safe, no div-by-zero)", rr_zero_risk["reward_risk"] is None)

rr_marine = compute_reward_risk("BUY", entry=383.80, current=382.45, stop=380.92, target=385.53)
check("MARINE entry R:R is reported as 0.60, not the misleading live 2.01",
      abs(rr_marine["entry_reward_risk"] - 0.6007) < 0.001)
check("MARINE live remaining R:R is independently reported as about 2.01",
      abs(rr_marine["remaining_reward_risk"] - 2.0131) < 0.001)

# --- Spread ---
s = compute_spread(bid=99.5, ask=100.0)
check("Spread: 100.0-99.5=0.5", abs(s["spread"] - 0.5) < 0.001)
check("Spread %: 0.5/99.5*100", abs(s["spread_pct"] - 0.5025) < 0.001)
s_none = compute_spread(bid=None, ask=100.0)
check("Missing bid/ask returns None spread (fail-safe)", s_none["spread"] is None)

# --- MFE/MAE ---
pos = {}
update_mfe_mae(pos, -1.0)
check("First losing update: MFE remains zero and MAE becomes -1.0",
      pos["mfe_pct"] == 0.0 and pos["mae_pct"] == -1.0)
legacy_pos = {"mfe_pct": -0.14, "mae_pct": -0.36}
update_mfe_mae(legacy_pos, -0.20)
check("Legacy negative MFE is repaired to zero without losing MAE history",
      legacy_pos["mfe_pct"] == 0.0 and legacy_pos["mae_pct"] == -0.36)
pos = {}
update_mfe_mae(pos, 2.0)
check("First winning update: MFE=2.0 and MAE remains zero",
      pos["mfe_pct"] == 2.0 and pos["mae_pct"] == 0.0)
update_mfe_mae(pos, 5.0)
check("New high: MFE updates to 5.0, MAE stays zero", pos["mfe_pct"] == 5.0 and pos["mae_pct"] == 0.0)
update_mfe_mae(pos, -1.5)
check("New low: MAE updates to -1.5, MFE stays 5.0", pos["mfe_pct"] == 5.0 and pos["mae_pct"] == -1.5)
update_mfe_mae(pos, 3.0)
check("Mid-range value: MFE/MAE both correctly unchanged", pos["mfe_pct"] == 5.0 and pos["mae_pct"] == -1.5)

# --- Status classification ---
check("Stale quote takes priority over everything else",
      classify_status("BUY", 105, 95, 115, quote_age_seconds=90, stale_threshold=60) == "PRICE_STALE")
check("BUY: price at/above target -> TARGET_HIT",
      classify_status("BUY", 116, 95, 115) == "TARGET_HIT (Pending Exit)")
check("BUY: price at/below stop -> STOP_HIT",
      classify_status("BUY", 94, 95, 115) == "STOP_HIT (Pending Exit)")
check("BUY: within 0.25% of target -> NEAR TARGET",
      classify_status("BUY", 114.8, 95, 115, near_pct=0.25) == "NEAR TARGET")
check("BUY: within 0.25% of stop -> NEAR STOP",
      classify_status("BUY", 95.2, 95, 115, near_pct=0.25) == "NEAR STOP")
check("BUY: comfortably in the middle -> ACTIVE",
      classify_status("BUY", 105, 95, 115) == "ACTIVE")
check("SELL: price at/below target -> TARGET_HIT",
      classify_status("SELL", 84, 105, 85) == "TARGET_HIT (Pending Exit)")
check("SELL: price at/above stop -> STOP_HIT",
      classify_status("SELL", 106, 105, 85) == "STOP_HIT (Pending Exit)")
check("SELL: comfortably in the middle -> ACTIVE",
      classify_status("SELL", 95, 105, 85) == "ACTIVE")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- fetch_batched_quotes ---
print("\n--- Batched quote fetch ---")
from unittest.mock import MagicMock
from position_analytics import fetch_batched_quotes, build_position_analytics

mock_kite = MagicMock()
mock_kite.quote.return_value = {"NSE:RELIANCE": {"last_price": 1500}}
positions = {"RELIANCE": {"exchange": "NSE"}, "TCS": {"exchange": "NSE"}}
result = fetch_batched_quotes(mock_kite, positions)
check("Batched fetch calls kite.quote() exactly once (not per-symbol)", mock_kite.quote.call_count == 1)
check("Batched fetch passes both instruments in a single call",
      len(mock_kite.quote.call_args[0][0]) == 2)

mock_kite_fail = MagicMock()
mock_kite_fail.quote.side_effect = Exception("network error")
result_fail = fetch_batched_quotes(mock_kite_fail, positions)
check("Batched fetch fails safe (empty dict) on error, does not raise", result_fail == {})

check("Empty positions returns empty dict without calling kite at all",
      fetch_batched_quotes(MagicMock(), {}) == {})

# --- build_position_analytics: normal path ---
print("\n--- build_position_analytics ---")
position = {"direction": "BUY", "entry": 100.0, "qty": 10, "stop": 95.0, "target": 115.0,
            "exchange": "NSE", "entry_time": "2026-07-31 10:00:00"}
quotes = {"NSE:TEST": {"last_price": 105.0, "last_quantity": 50,
                       "depth": {"buy": [{"price": 104.9}], "sell": [{"price": 105.1}]}}}
analytics = build_position_analytics("TEST", position, quotes)
check("Normal path: current_price correctly extracted", analytics["current_price"] == 105.0)
check("Normal path: gross_unrealized_pnl = (105-100)*10 = 50", analytics["gross_unrealized_pnl"] == 50.0)
check("Normal path: profit_pct = 5.0%", abs(analytics["profit_pct"] - 5.0) < 0.001)
check("Normal path: bid/ask correctly extracted", analytics["bid"] == 104.9 and analytics["ask"] == 105.1)
check("Normal path: status is a real classification, not stale", analytics["status"] != "PRICE_STALE")
check("Normal path: mfe_pct set on first call", analytics["mfe_pct"] == 5.0)

# --- build_position_analytics: missing quote, WITH prior data -> retains it ---
prior = {"symbol": "TEST", "current_price": 105.0, "profit_pct": 5.0, "price_updated_at": time.time() - 30}
analytics_stale = build_position_analytics("TEST", position, {}, previous_status_entry=prior)
check("Missing quote with prior data: retains the old current_price (never zeros it)",
      analytics_stale["current_price"] == 105.0)
check("Missing quote with prior data: status becomes PRICE_STALE", analytics_stale["status"] == "PRICE_STALE")
check("Missing quote with prior data: quote_age_seconds correctly grows", analytics_stale["quote_age_seconds"] >= 30)

# --- build_position_analytics: missing quote, NO prior data ---
analytics_no_prior = build_position_analytics("TEST", position, {}, previous_status_entry=None)
check("Missing quote with no prior data: status is PRICE_STALE, current_price is None (not fabricated as 0)",
      analytics_no_prior["status"] == "PRICE_STALE" and analytics_no_prior["current_price"] is None)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- compute_portfolio_summary ---
print("\n--- Portfolio Summary ---")
from position_analytics import compute_portfolio_summary, compute_session_summary

check("Empty portfolio: zero positions, no crash", compute_portfolio_summary([])["total_open_positions"] == 0)

positions_list = [
    {"symbol": "A", "side": "BUY", "quantity": 10, "current_price": 110, "gross_unrealized_pnl": 100,
     "net_unrealized_pnl": 90, "risk_remaining": 5, "reward_remaining": 10, "time_in_trade_minutes": 30},
    {"symbol": "B", "side": "SELL", "quantity": 5, "current_price": 200, "gross_unrealized_pnl": -50,
     "net_unrealized_pnl": -60, "risk_remaining": 8, "reward_remaining": 4, "time_in_trade_minutes": 60},
]
summary = compute_portfolio_summary(positions_list)
check("Correct total open positions (2)", summary["total_open_positions"] == 2)
check("Correct BUY/SELL split (1/1)", summary["buy_positions"] == 1 and summary["sell_positions"] == 1)
check("Correct gross unrealized P&L sum (100-50=50)", summary["gross_unrealized_pnl"] == 50)
check("Correct largest winning position identified (A)", summary["largest_winning_position"] == "A")
check("Correct largest losing position identified (B)", summary["largest_losing_position"] == "B")
check("Correct total portfolio risk (5+8=13)", summary["total_portfolio_risk"] == 13)
check("Correct total portfolio reward (10+4=14)", summary["total_portfolio_reward"] == 14)
check("Correct average holding time ((30+60)/2=45)", summary["average_holding_time_minutes"] == 45)

margins_data = {"equity": {"available": {"live_balance": 5000}, "utilised": {"debits": 2000}, "net": 3000}}
summary_with_margin = compute_portfolio_summary(positions_list, margins=margins_data)
check("Margin data correctly extracted: available_cash", summary_with_margin["available_cash"] == 5000)
check("Margin data correctly extracted: margin_utilization_pct", abs(summary_with_margin["margin_utilization_pct"] - 66.67) < 0.1)

# --- compute_session_summary ---
print("\n--- Session Summary ---")
check("Empty session: zero trades, no crash", compute_session_summary([])["todays_trades"] == 0)

todays_trades = [
    {"pnl": 100, "gross_pnl": 110, "costs": 10},
    {"pnl": -50, "gross_pnl": -45, "costs": 5},
    {"pnl": 80, "gross_pnl": 88, "costs": 8},
    {"pnl": 60, "gross_pnl": 65, "costs": 5},
]
session = compute_session_summary(todays_trades)
check("Correct trade count (4)", session["todays_trades"] == 4)
check("Correct win/loss split (3 wins, 1 loss)", session["winning_trades"] == 3 and session["losing_trades"] == 1)
check("Correct win rate (75%)", session["win_rate_pct"] == 75.0)
check("Correct net realized profit (100-50+80+60=190)", session["net_realized_profit"] == 190)
check("Correct total costs (10+5+8+5=28)", session["brokerage_and_charges"] == 28)
check("Correct current consecutive wins (last 2 trades: +80,+60 = streak of 2)", session["current_consecutive_wins"] == 2)
check("Correct profit factor ((100+80+60)/50 = 4.8)", abs(session["profit_factor"] - 4.8) < 0.01)

# Consecutive losses test: most recent trades are losses
trades_losing_streak = [{"pnl": 100, "gross_pnl": 100, "costs": 0}, {"pnl": -20, "gross_pnl": -20, "costs": 0}, {"pnl": -30, "gross_pnl": -30, "costs": 0}]
session2 = compute_session_summary(trades_losing_streak)
check("Correct current consecutive losses (last 2 trades are losses)", session2["current_consecutive_losses"] == 2)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- compute_health_check ---
print("\n--- Health Check ---")
from position_analytics import compute_health_check
import config as cfg

class FakeCfg:
    PAPER_TRADING = True
    ENABLE_MARKET_ALIGNMENT_FILTER = True
    USE_ADX_FILTER = True
    ENABLE_CANDLE_ALIGNED_POLLING = False
    POSITION_CHECK_SECONDS = 25
    WATCHLIST = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]

mock_kite_healthy = MagicMock()
mock_kite_healthy.margins.return_value = {"equity": {"net": 1000}}

health = compute_health_check(FakeCfg(), mock_kite_healthy, {}, ["A", "B", "C"], time.time() - 3600)
check("Correctly reports PAPER mode", health["trading_mode"] == "PAPER")
check("Correctly reports market alignment enabled", health["market_alignment"] == "Enabled")
check("Correctly reports entry scheduler as Continuous (candle-aligned off)", health["entry_scheduler"] == "Continuous")
check("Correctly reports watchlist size (3)", health["watchlist_size"] == 3)
check("Correctly reports API connection as Authenticated", health["api_connection"] == "Authenticated")
check("Bot uptime is a real positive number (~3600s)", 3590 < health["bot_uptime_seconds"] < 3610)
check("Memory usage successfully reported (psutil installed)", health["memory_usage_mb"] is not None and health["memory_usage_mb"] > 0)

mock_kite_unhealthy = MagicMock()
mock_kite_unhealthy.margins.side_effect = Exception("auth expired")
health_bad_auth = compute_health_check(FakeCfg(), mock_kite_unhealthy, {}, ["A", "B", "C"], time.time())
check("Correctly reports API connection as Disconnected on auth failure", health_bad_auth["api_connection"] == "Disconnected")
check("Auth failure does not crash the rest of the health check", health_bad_auth["watchlist_size"] == 3)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- build_full_analytics_snapshot (orchestration) ---
print("\n--- Full Analytics Snapshot Orchestration ---")
from position_analytics import build_full_analytics_snapshot

mock_kite_full = MagicMock()
mock_kite_full.margins.return_value = {"equity": {"net": 1000, "available": {"live_balance": 500},
                                                     "utilised": {"debits": 500}}}
mock_kite_full.quote.return_value = {
    "NSE:TEST": {"last_price": 105.0, "last_quantity": 10,
                 "depth": {"buy": [{"price": 104.9}], "sell": [{"price": 105.1}]}}
}
open_positions_snap = {"TEST": {"direction": "BUY", "entry": 100.0, "qty": 10, "stop": 95.0,
                                  "target": 115.0, "exchange": "NSE", "entry_time": "2026-07-31 10:00:00"}}

positions_list, portfolio, session, health = build_full_analytics_snapshot(
    mock_kite_full, FakeCfg(), open_positions_snap, ["TEST"], time.time() - 100,
    previous_bot_status=None, todays_trades=[{"pnl": 50, "gross_pnl": 55, "costs": 5}]
)
check("Orchestration: correctly assembles one position", len(positions_list) == 1)
check("Orchestration: portfolio summary reflects the one position", portfolio["total_open_positions"] == 1)
check("Orchestration: session summary reflects the one trade", session["todays_trades"] == 1)
check("Orchestration: health check populated", health["trading_mode"] == "PAPER")

# Fail-safe: quote fetch raises entirely -- should still return a usable (degraded) snapshot
mock_kite_broken = MagicMock()
mock_kite_broken.margins.return_value = {"equity": {"net": 1000}}
mock_kite_broken.quote.side_effect = Exception("total API outage")
positions_list_broken, portfolio_broken, session_broken, health_broken = build_full_analytics_snapshot(
    mock_kite_broken, FakeCfg(), open_positions_snap, ["TEST"], time.time(), todays_trades=[]
)
check("Orchestration fail-safe: total quote outage does not crash the whole snapshot", True)
check("Orchestration fail-safe: position still appears (marked stale), not silently dropped",
      len(positions_list_broken) == 1 and positions_list_broken[0]["status"] == "PRICE_STALE")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- validate_position ---
print("\n--- Position Validation ---")
from position_analytics import validate_position, classify_bot_freshness

valid_pos = {"exchange": "NSE", "direction": "BUY", "qty": 10, "entry": 100.0, "stop": 95.0, "target": 110.0}
check("Fully valid position has no errors", validate_position("TEST", valid_pos) == [])

check("Missing direction is caught", "direction is missing or invalid (must be BUY or SELL)" in
      validate_position("TEST", {**valid_pos, "direction": None}))
check("Invalid direction string is caught", "direction is missing or invalid (must be BUY or SELL)" in
      validate_position("TEST", {**valid_pos, "direction": "HOLD"}))
check("Zero quantity is caught", "quantity must be greater than zero" in
      validate_position("TEST", {**valid_pos, "qty": 0}))
check("Negative entry price is caught", "entry_price must be greater than zero" in
      validate_position("TEST", {**valid_pos, "entry": -5}))
check("Missing stop is caught", "stop_price must be greater than zero" in
      validate_position("TEST", {**valid_pos, "stop": None}))
check("Missing target is caught", "target_price must be greater than zero" in
      validate_position("TEST", {**valid_pos, "target": None}))
check("Missing exchange is caught", "exchange is missing" in
      validate_position("TEST", {**valid_pos, "exchange": None}))
check("Multiple errors all reported at once",
      len(validate_position("TEST", {"exchange": None, "direction": None, "qty": 0, "entry": 0, "stop": 0, "target": 0})) >= 5)

# --- INVALID_DATA integration in build_position_analytics ---
print("\n--- INVALID_DATA Integration ---")
malformed_position = {"exchange": "NSE", "direction": None, "qty": 10, "entry": 0, "stop": 95.0, "target": 110.0}
result = build_position_analytics("BADSYM", malformed_position, {"NSE:BADSYM": {"last_price": 100}})
check("Malformed position (missing direction) does not crash, returns INVALID_DATA",
      result["status"] == "INVALID_DATA")
check("Malformed position never fabricates a gross_unrealized_pnl", "gross_unrealized_pnl" not in result)
check("validation_errors list is present and non-empty", len(result.get("validation_errors", [])) > 0)

# Valid position + quote with a nonsensical current_price (e.g. 0)
weird_quote = {"NSE:TEST2": {"last_price": 0, "depth": {}}}
result2 = build_position_analytics("TEST2", valid_pos, weird_quote)
check("Valid static data but nonsensical current_price (0) also returns INVALID_DATA",
      result2["status"] == "INVALID_DATA")

# --- Portfolio summary excludes INVALID_DATA from financial aggregation ---
print("\n--- Portfolio Summary Excludes Invalid Positions ---")
mixed_list = [
    {"symbol": "GOOD", "side": "BUY", "quantity": 10, "current_price": 110,
     "gross_unrealized_pnl": 100, "net_unrealized_pnl": 90, "risk_remaining": 5, "reward_remaining": 10,
     "status": "ACTIVE"},
    {"symbol": "BAD", "status": "INVALID_DATA", "validation_errors": ["entry_price must be greater than zero"]},
]
summary_mixed = compute_portfolio_summary(mixed_list)
check("Invalid position excluded from total_open_positions count", summary_mixed["total_open_positions"] == 1)
check("Invalid position counted separately", summary_mixed["invalid_position_count"] == 1)
check("Invalid position's absence of gross_unrealized_pnl does not crash or corrupt the sum",
      summary_mixed["gross_unrealized_pnl"] == 100)

# --- Summary-equals-sum-of-rows guarantee ---
print("\n--- Summary Consistency (sum of rows = summary total) ---")
three_positions = [
    {"symbol": "A", "side": "BUY", "quantity": 10, "current_price": 110, "gross_unrealized_pnl": 100,
     "net_unrealized_pnl": 90, "risk_remaining": 5, "reward_remaining": 10, "status": "ACTIVE"},
    {"symbol": "B", "side": "SELL", "quantity": 5, "current_price": 200, "gross_unrealized_pnl": -50,
     "net_unrealized_pnl": -60, "risk_remaining": 8, "reward_remaining": 4, "status": "ACTIVE"},
    {"symbol": "C", "status": "INVALID_DATA", "validation_errors": ["bad data"]},
]
summary3 = compute_portfolio_summary(three_positions)
manual_sum_gross = sum(p.get("gross_unrealized_pnl", 0) for p in three_positions if p.get("status") != "INVALID_DATA")
check("Sum of valid rows' gross P&L exactly equals portfolio summary total",
      summary3["gross_unrealized_pnl"] == manual_sum_gross)
check("Count of valid rows exactly equals portfolio open-position count",
      summary3["total_open_positions"] == 2)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")

# --- classify_bot_freshness ---
print("\n--- Bot Freshness / Offline Detection ---")
from datetime import datetime as _dt
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
_kolkata = ZoneInfo("Asia/Kolkata")

# Weekend (Saturday) during nominal "market hours" -> MARKET_CLOSED, never BOT_OFFLINE
saturday_noon = _dt(2026, 8, 1, 12, 0, tzinfo=_kolkata)  # 2026-08-01 is a Saturday
result_weekend = classify_bot_freshness(
    generated_at=_dt(2026, 8, 1, 8, 0, tzinfo=_kolkata),  # very old, would be "offline" on a weekday
    now=saturday_noon,
)
check("Weekend during nominal market hours -> MARKET_CLOSED (never alarms as offline)",
      result_weekend["status"] == "MARKET_CLOSED")

# Weekday, outside market hours (e.g. 20:00) -> MARKET_CLOSED, even with a very stale timestamp
weekday_evening = _dt(2026, 7, 31, 20, 0, tzinfo=_kolkata)  # 2026-07-31 is a Friday
result_after_hours = classify_bot_freshness(
    generated_at=_dt(2026, 7, 31, 10, 0, tzinfo=_kolkata),
    now=weekday_evening,
)
check("Weekday after market close -> MARKET_CLOSED, not an offline alarm",
      result_after_hours["status"] == "MARKET_CLOSED")

# Weekday, within market hours, status genuinely very old -> BOT_OFFLINE
weekday_midday = _dt(2026, 7, 31, 11, 0, tzinfo=_kolkata)
result_offline = classify_bot_freshness(
    generated_at=_dt(2026, 7, 31, 10, 30, tzinfo=_kolkata),  # 30 min old
    position_monitor_interval_seconds=25, offline_multiplier=2,  # offline threshold = 50s
    now=weekday_midday,
)
check("Within market hours, status 30 min old (way past offline threshold) -> BOT_OFFLINE",
      result_offline["status"] == "BOT_OFFLINE")

# Weekday, within market hours, status moderately old (past stale but not offline)
result_stale = classify_bot_freshness(
    generated_at=weekday_midday.replace(second=0) - __import__("datetime").timedelta(seconds=90),
    position_monitor_interval_seconds=25, offline_multiplier=10, stale_threshold_seconds=60,
    now=weekday_midday,
)
check("Within market hours, moderately stale (past 60s, not past offline threshold) -> STATUS_STALE",
      result_stale["status"] == "STATUS_STALE")

# Weekday, within market hours, fresh status -> LIVE
result_live = classify_bot_freshness(
    generated_at=weekday_midday - __import__("datetime").timedelta(seconds=5),
    position_monitor_interval_seconds=25, offline_multiplier=2, stale_threshold_seconds=60,
    now=weekday_midday,
)
check("Within market hours, freshly updated (5s old) -> LIVE", result_live["status"] == "LIVE")

# No status has ever been recorded
result_none = classify_bot_freshness(generated_at=None, now=weekday_midday)
check("No status ever recorded -> BOT_OFFLINE with a clear reason",
      result_none["status"] == "BOT_OFFLINE" and "ever been recorded" in result_none["reason"])

# Unparseable timestamp fails safe
result_bad = classify_bot_freshness(generated_at="not-a-real-timestamp", now=weekday_midday)
check("Unparseable generated_at fails safe to BOT_OFFLINE, does not crash", result_bad["status"] == "BOT_OFFLINE")

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
