from watchlist_section import WATCHLIST_SECTION

MONITOR_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Live Trading Monitor</title>
    <style>
        body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background: #0f1117; color: #e5e7eb; margin: 0; padding: 20px; }
        h1 { font-size: 20px; margin-bottom: 4px; }
        .subtitle { color: #9ca3af; font-size: 13px; margin-bottom: 20px; }
        .section { background: #1a1d29; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        .section h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: #9ca3af; margin: 0 0 12px 0; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; padding: 6px 10px; color: #9ca3af; font-weight: 500; border-bottom: 1px solid #2d3142; white-space: nowrap; }
        td { padding: 6px 10px; border-bottom: 1px solid #232633; white-space: nowrap; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
        .metric { background: #14161f; border-radius: 6px; padding: 10px 12px; }
        .metric .label { font-size: 11px; color: #9ca3af; text-transform: uppercase; }
        .metric .value { font-size: 18px; font-weight: 600; margin-top: 2px; }
        .green { color: #22c55e; } .red { color: #ef4444; } .yellow { color: #eab308; }
        .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
        .dot-green { background: #22c55e; } .dot-red { background: #ef4444; } .dot-yellow { background: #eab308; }
        .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .badge-active { background: #1e3a2e; color: #22c55e; }
        .badge-near { background: #3a3419; color: #eab308; }
        .badge-hit { background: #3a2419; color: #f97316; }
        .badge-stale { background: #3a1919; color: #ef4444; }
        .empty { color: #6b7280; font-style: italic; padding: 8px; }
        a { color: #60a5fa; }
    </style>
</head>
<body>
    <h1>Live Trading Monitor</h1>
    <div class="subtitle">Last updated: {{ updated }} | <a href="/">Settings</a></div>

    <div class="section">
        <h2>System Status</h2>
        <div class="grid">
            <div class="metric"><div class="label">Trading Mode</div><div class="value {{ 'red' if health.get('trading_mode') == 'LIVE' else 'green' }}">{{ health.get('trading_mode', 'N/A') }}</div></div>
            <div class="metric"><div class="label">API Connection</div><div class="value {{ 'green' if health.get('api_connection') == 'Authenticated' else 'red' }}"><span class="dot {{ 'dot-green' if health.get('api_connection') == 'Authenticated' else 'dot-red' }}"></span>{{ health.get('api_connection', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Entry Scheduler</div><div class="value">{{ health.get('entry_scheduler', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Market Alignment</div><div class="value">{{ health.get('market_alignment', 'N/A') }}</div></div>
            <div class="metric"><div class="label">ADX Filter</div><div class="value">{{ health.get('adx_filter', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Watchlist Size</div><div class="value">{{ health.get('watchlist_size', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Open Positions</div><div class="value">{{ health.get('open_positions', 'N/A') }}</div></div>
            <div class="metric"><div class="label">Bot Uptime</div><div class="value">{{ "%.0f"|format(health.get('bot_uptime_seconds', 0) / 60) }} min</div></div>
            <div class="metric"><div class="label">Memory</div><div class="value">{{ "%.0f"|format(health.get('memory_usage_mb', 0) or 0) }} MB</div></div>
            <div class="metric"><div class="label">Git Commit</div><div class="value">{{ health.get('git_commit_hash', 'N/A') }}</div></div>
        </div>
    </div>

    <div class="section">
        <h2>Portfolio Summary</h2>
        <div class="grid">
            <div class="metric"><div class="label">Open Positions</div><div class="value">{{ portfolio.get('total_open_positions', 0) }} ({{ portfolio.get('buy_positions', 0) }} BUY / {{ portfolio.get('sell_positions', 0) }} SELL)</div></div>
            <div class="metric"><div class="label">Total Exposure</div><div class="value">Rs{{ "%.2f"|format(portfolio.get('total_exposure', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Gross Unrealized P&L</div><div class="value {{ 'green' if (portfolio.get('gross_unrealized_pnl') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(portfolio.get('gross_unrealized_pnl', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Net Unrealized P&L</div><div class="value {{ 'green' if (portfolio.get('net_unrealized_pnl') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(portfolio.get('net_unrealized_pnl', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Available Cash</div><div class="value">Rs{{ "%.2f"|format(portfolio.get('available_cash', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Margin Utilization</div><div class="value">{{ "%.1f"|format(portfolio.get('margin_utilization_pct', 0) or 0) }}%</div></div>
            <div class="metric"><div class="label">Largest Winner</div><div class="value green">{{ portfolio.get('largest_winning_position') or '-' }}</div></div>
            <div class="metric"><div class="label">Largest Loser</div><div class="value red">{{ portfolio.get('largest_losing_position') or '-' }}</div></div>
            <div class="metric"><div class="label">Portfolio Live Reward:Risk</div><div class="value">{{ "%.2f"|format(portfolio.get('portfolio_reward_risk', 0) or 0) }}</div></div>
        </div>
    </div>

    <div class="section">
        <h2>Live Positions</h2>
        {% if positions %}
        <table>
            <tr>
                <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th>
                <th>Time in Trade</th><th>Gross P&L</th><th>Net P&L</th><th>Profit %</th>
                <th>MFE %</th><th>MAE %</th><th>Hard Stop</th><th>Strategy Stop</th><th>Active Target</th>
                <th>Dist. Stop %</th><th>Dist. Target %</th><th>Entry R:R</th><th>Live R:R</th><th>Status</th>
            </tr>
            {% for p in positions %}
            <tr>
                <td>{{ p.get('symbol') }}</td>
                <td>{{ p.get('side') }}</td>
                <td>{{ p.get('quantity') }}</td>
                <td>{{ "%.2f"|format(p.get('entry_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('current_price', 0) or 0) if p.get('current_price') is not none else 'N/A' }}</td>
                <td>{{ "%.0f"|format(p.get('time_in_trade_minutes', 0) or 0) }}m</td>
                <td class="{{ 'green' if (p.get('gross_unrealized_pnl') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('gross_unrealized_pnl', 0) or 0) }}</td>
                <td class="{{ 'green' if (p.get('net_unrealized_pnl') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('net_unrealized_pnl', 0) or 0) }}</td>
                <td class="{{ 'green' if (p.get('profit_pct') or 0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('profit_pct', 0) or 0) }}%</td>
                <td class="green">{{ "%.2f"|format(p.get('mfe_pct', 0) or 0) }}%</td>
                <td class="red">{{ "%.2f"|format(p.get('mae_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('stop_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('strategy_stop_price', 0) or 0) if p.get('strategy_stop_price') is not none else '-' }}</td>
                <td>{{ "%.2f"|format(p.get('target_price', 0) or 0) }}</td>
                <td>{{ "%.2f"|format(p.get('distance_to_stop_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('distance_to_target_pct', 0) or 0) }}%</td>
                <td>{{ "%.2f"|format(p.get('entry_reward_risk', 0) or 0) if p.get('entry_reward_risk') is not none else '-' }}</td>
                <td>{{ "%.2f"|format(p.get('remaining_reward_risk', 0) or 0) if p.get('remaining_reward_risk') is not none else '-' }}</td>
                <td>
                    {% set st = p.get('status', 'ACTIVE') %}
                    <span class="badge {{ 'badge-stale' if 'STALE' in st else ('badge-hit' if 'HIT' in st else ('badge-near' if 'NEAR' in st else 'badge-active')) }}">{{ st }}</span>
                </td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div class="empty">No open positions.</div>
        {% endif %}
    </div>

    <div class="section">
        <h2>Today's Session</h2>
        <div class="grid">
            <div class="metric"><div class="label">Trades Today</div><div class="value">{{ session.get('todays_trades', 0) }}</div></div>
            <div class="metric"><div class="label">Win Rate</div><div class="value">{{ "%.1f"|format(session.get('win_rate_pct', 0) or 0) }}%</div></div>
            <div class="metric"><div class="label">Net Realized P&L</div><div class="value {{ 'green' if (session.get('net_realized_profit') or 0) >= 0 else 'red' }}">Rs{{ "%.2f"|format(session.get('net_realized_profit', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Brokerage & Charges</div><div class="value">Rs{{ "%.2f"|format(session.get('brokerage_and_charges', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Profit Factor</div><div class="value">{{ profit_factor_display }}</div></div>
            <div class="metric"><div class="label">Expectancy</div><div class="value">Rs{{ "%.2f"|format(session.get('expectancy', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Consecutive Wins</div><div class="value green">{{ session.get('current_consecutive_wins', 0) }}</div></div>
            <div class="metric"><div class="label">Consecutive Losses</div><div class="value red">{{ session.get('current_consecutive_losses', 0) }}</div></div>
            <div class="metric"><div class="label">Max Drawdown Today</div><div class="value red">Rs{{ "%.2f"|format(session.get('max_drawdown_today', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Largest Winner</div><div class="value green">Rs{{ "%.2f"|format(session.get('largest_winner', 0) or 0) }}</div></div>
            <div class="metric"><div class="label">Largest Loser</div><div class="value red">Rs{{ "%.2f"|format(session.get('largest_loser', 0) or 0) }}</div></div>
        </div>
    </div>
""" + WATCHLIST_SECTION + """
</body>
</html>
"""
