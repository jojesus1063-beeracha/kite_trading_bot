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

# --- Reward:Risk (remaining, from current price) ---
rr = compute_reward_risk("BUY", entry=100, current=105, stop=95, target=115)
check("BUY reward remaining: 115-105=10", rr["reward_remaining"] == 10)
check("BUY risk remaining: 105-95=10", rr["risk_remaining"] == 10)
check("BUY reward:risk ratio: 10/10=1.0", rr["reward_risk"] == 1.0)
rr_zero_risk = compute_reward_risk("BUY", entry=100, current=95, stop=95, target=115)
check("Reward:risk is None when risk_remaining is 0 (fail-safe, no div-by-zero)", rr_zero_risk["reward_risk"] is None)

# --- Spread ---
s = compute_spread(bid=99.5, ask=100.0)
check("Spread: 100.0-99.5=0.5", abs(s["spread"] - 0.5) < 0.001)
check("Spread %: 0.5/99.5*100", abs(s["spread_pct"] - 0.5025) < 0.001)
s_none = compute_spread(bid=None, ask=100.0)
check("Missing bid/ask returns None spread (fail-safe)", s_none["spread"] is None)

# --- MFE/MAE ---
pos = {}
update_mfe_mae(pos, 2.0)
check("First update: MFE=MAE=2.0", pos["mfe_pct"] == 2.0 and pos["mae_pct"] == 2.0)
update_mfe_mae(pos, 5.0)
check("New high: MFE updates to 5.0, MAE stays 2.0", pos["mfe_pct"] == 5.0 and pos["mae_pct"] == 2.0)
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
