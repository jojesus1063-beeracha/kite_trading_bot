from flask import Flask, render_template_string
from monitor_route import MONITOR_PAGE

passed, failed = 0, 0

def check(name, condition):
    global passed, failed
    if condition:
        print("PASS: " + name)
        passed += 1
    else:
        print("FAIL: " + name)
        failed += 1

app = Flask(__name__)

SAMPLE_POSITION = {
    'symbol': 'RELIANCE', 'side': 'BUY', 'quantity': 5, 'entry_price': 1294.72,
    'current_price': 1307.8, 'time_in_trade_minutes': 878.3,
    'gross_unrealized_pnl': 65.39, 'net_unrealized_pnl': 58.48, 'profit_pct': 1.01,
    'mfe_pct': 1.01, 'mae_pct': 1.01, 'stop_price': 1268.57, 'target_price': 1347.03,
    'distance_to_stop_pct': 3.0, 'distance_to_target_pct': 3.0, 'reward_risk': 1.0,
    'status': 'ACTIVE',
}
SAMPLE_PORTFOLIO = {'total_open_positions': 1, 'buy_positions': 1, 'sell_positions': 0,
                    'total_exposure': 6539.0, 'gross_unrealized_pnl': 65.39, 'net_unrealized_pnl': 58.48,
                    'available_cash': 473.25, 'margin_utilization_pct': 0.52,
                    'largest_winning_position': 'RELIANCE', 'largest_losing_position': None,
                    'portfolio_reward_risk': 1.0}
SAMPLE_HEALTH = {'trading_mode': 'LIVE', 'api_connection': 'Authenticated', 'entry_scheduler': 'Continuous',
                 'market_alignment': 'Enabled', 'adx_filter': 'Enabled', 'watchlist_size': 108,
                 'open_positions': 1, 'bot_uptime_seconds': 120, 'memory_usage_mb': 97.5,
                 'git_commit_hash': '23d953f'}

with app.app_context():
    # --- Normal render with real-shaped data ---
    html = render_template_string(
        MONITOR_PAGE, updated='2026-07-31 23:45:00', positions=[SAMPLE_POSITION],
        portfolio=SAMPLE_PORTFOLIO,
        session={'todays_trades': 0, 'win_rate_pct': 0, 'net_realized_profit': 0,
                 'brokerage_and_charges': 0, 'expectancy': 0, 'current_consecutive_wins': 0,
                 'current_consecutive_losses': 0, 'max_drawdown_today': 0, 'largest_winner': 0, 'largest_loser': 0},
        health=SAMPLE_HEALTH, profit_factor_display='4.80',
        watchlist_snapshot=None, freshness={"status": "NO_REPORT_AVAILABLE", "reason": "none", "report_session_date": None, "expected_session_date": None}, summary_cards={"largest_mover": None, "strongest_close": None, "weakest_close": None, "average_range_pct": None, "median_range_pct": None, "most_common_high_hour": None, "most_common_low_hour": None, "high_retest_count": 0, "low_retest_count": 0}, watchlist_symbols_json="[]",
    )
    check("Normal render succeeds with no Jinja errors", len(html) > 0)
    check("Renders the real position's symbol", "RELIANCE" in html)
    check("Renders the ACTIVE status badge", "ACTIVE" in html)

    # --- Empty portfolio ---
    html_empty = render_template_string(
        MONITOR_PAGE, updated='N/A', positions=[], portfolio={}, session={}, health={},
        profit_factor_display='N/A',
        watchlist_snapshot=None, freshness={"status": "NO_REPORT_AVAILABLE", "reason": "none", "report_session_date": None, "expected_session_date": None}, summary_cards={"largest_mover": None, "strongest_close": None, "weakest_close": None, "average_range_pct": None, "median_range_pct": None, "most_common_high_hour": None, "most_common_low_hour": None, "high_retest_count": 0, "low_retest_count": 0}, watchlist_symbols_json="[]",
    )
    check("Empty portfolio renders without crashing", len(html_empty) > 0)
    check("Empty portfolio shows the 'no positions' message", "No open positions" in html_empty)

    # --- Profit factor infinity edge case (the real bug found and fixed) ---
    html_inf = render_template_string(
        MONITOR_PAGE, updated='N/A', positions=[], portfolio={}, session={}, health={},
        profit_factor_display='inf',
        watchlist_snapshot=None, freshness={"status": "NO_REPORT_AVAILABLE", "reason": "none", "report_session_date": None, "expected_session_date": None}, summary_cards={"largest_mover": None, "strongest_close": None, "weakest_close": None, "average_range_pct": None, "median_range_pct": None, "most_common_high_hour": None, "most_common_low_hour": None, "high_retest_count": 0, "low_retest_count": 0}, watchlist_symbols_json="[]",
    )
    check("Infinite profit factor renders as 'inf' without crashing (regression test for the real bug found)",
          "inf" in html_inf and len(html_inf) > 0)

    # --- Stale price status renders correctly ---
    stale_position = dict(SAMPLE_POSITION)
    stale_position['status'] = 'PRICE_STALE'
    html_stale = render_template_string(
        MONITOR_PAGE, updated='N/A', positions=[stale_position], portfolio=SAMPLE_PORTFOLIO,
        session={}, health=SAMPLE_HEALTH, profit_factor_display='N/A',
        watchlist_snapshot=None, freshness={"status": "NO_REPORT_AVAILABLE", "reason": "none", "report_session_date": None, "expected_session_date": None}, summary_cards={"largest_mover": None, "strongest_close": None, "weakest_close": None, "average_range_pct": None, "median_range_pct": None, "most_common_high_hour": None, "most_common_low_hour": None, "high_retest_count": 0, "low_retest_count": 0}, watchlist_symbols_json="[]",
    )
    check("PRICE_STALE status renders with the stale badge class", "badge-stale" in html_stale)

    # --- Multiple positions render correctly (BUY and SELL mixed) ---
    sell_position = dict(SAMPLE_POSITION)
    sell_position['symbol'] = 'TCS'
    sell_position['side'] = 'SELL'
    html_multi = render_template_string(
        MONITOR_PAGE, updated='N/A', positions=[SAMPLE_POSITION, sell_position],
        portfolio=SAMPLE_PORTFOLIO, session={}, health=SAMPLE_HEALTH, profit_factor_display='N/A',
        watchlist_snapshot=None, freshness={"status": "NO_REPORT_AVAILABLE", "reason": "none", "report_session_date": None, "expected_session_date": None}, summary_cards={"largest_mover": None, "strongest_close": None, "weakest_close": None, "average_range_pct": None, "median_range_pct": None, "most_common_high_hour": None, "most_common_low_hour": None, "high_retest_count": 0, "low_retest_count": 0}, watchlist_symbols_json="[]",
    )
    check("Multiple mixed BUY/SELL positions all render", "RELIANCE" in html_multi and "TCS" in html_multi)

print("")
print("Results: " + str(passed) + " passed, " + str(failed) + " failed")
