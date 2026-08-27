from watchlist_section import WATCHLIST_SECTION
from pipeline_dashboard_section import PIPELINE_MONITOR_SECTION

MONITOR_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark">
    <title>Live Trading Monitor</title>
    <style>
        :root { --bg:#080b12; --surface:#111722; --surface-2:#151d2a; --border:#263244; --text:#eef2f8; --muted:#91a0b5; --green:#34d399; --red:#fb7185; --yellow:#fbbf24; --blue:#60a5fa; }
        * { box-sizing: border-box; }
        html { scroll-behavior: smooth; scroll-padding-top: 76px; }
        body { font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at 50% -15%, #142036 0, var(--bg) 34rem); color: var(--text); margin: 0; padding: 0 22px 42px; font-variant-numeric: tabular-nums; -webkit-font-smoothing: antialiased; }
        .shell { width: min(100%, 1680px); margin: 0 auto; }
        .page-head { display:flex; justify-content:space-between; align-items:flex-end; gap:18px; padding:24px 2px 16px; }
        h1 { font-size: clamp(22px, 3vw, 30px); letter-spacing:-.035em; margin:0 0 5px; }
        .subtitle { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
        .page-head .subtitle { margin:0; }
        .live-mark { display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; }
        .quick-nav { position:sticky; top:0; z-index:20; display:flex; gap:6px; overflow-x:auto; padding:9px 0; margin-bottom:14px; background:rgba(8,11,18,.9); backdrop-filter:blur(14px); scrollbar-width:none; }
        .quick-nav::-webkit-scrollbar { display:none; }
        .quick-nav a { flex:none; color:var(--muted); border:1px solid var(--border); background:rgba(17,23,34,.92); border-radius:999px; padding:7px 12px; text-decoration:none; font-size:12px; font-weight:650; }
        .quick-nav a:hover { color:var(--text); border-color:#3b82f6; }
        .section { background:linear-gradient(180deg,rgba(20,28,41,.98),rgba(14,20,30,.98)); border:1px solid var(--border); border-radius:14px; padding:18px; margin-bottom:14px; box-shadow:0 12px 30px rgba(0,0,0,.18); content-visibility:auto; contain-intrinsic-size:auto 340px; }
        .section h2 { font-size:12px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); margin:0 0 14px; }
        .table-wrap { width:100%; overflow:auto; border:1px solid var(--border); border-radius:10px; scrollbar-color:#344258 transparent; }
        table { width:100%; border-collapse:separate; border-spacing:0; font-size:12px; }
        th { position:sticky; top:0; z-index:2; text-align:left; padding:9px 10px; color:var(--muted); background:#151d2a; font-weight:650; border-bottom:1px solid var(--border); white-space:nowrap; }
        td { padding:9px 10px; border-bottom:1px solid rgba(38,50,68,.65); white-space:nowrap; }
        tr:last-child td { border-bottom:0; }
        tbody tr:hover td { background:rgba(96,165,250,.055); }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:9px; }
        .metric { min-width:0; background:rgba(8,12,19,.52); border:1px solid rgba(38,50,68,.78); border-radius:10px; padding:11px 12px; }
        .metric .label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.055em; }
        .metric .value { overflow-wrap:anywhere; font-size:17px; font-weight:680; line-height:1.25; margin-top:4px; }
        .green { color:var(--green); } .red { color:var(--red); } .yellow { color:var(--yellow); }
        .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
        .dot-green { background: #22c55e; } .dot-red { background: #ef4444; } .dot-yellow { background: #eab308; }
        .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .badge-active { background: #1e3a2e; color: #22c55e; }
        .badge-near { background: #3a3419; color: #eab308; }
        .badge-hit { background: #3a2419; color: #f97316; }
        .badge-stale { background: #3a1919; color: #ef4444; }
        .empty { color: #6b7280; font-style: italic; padding: 8px; }
        a { color:var(--blue); }
        .sr-note { color:var(--muted); font-size:11px; margin:8px 2px 0; }
        @media (max-width:700px) {
            body { padding:0 10px 26px; }
            .page-head { align-items:flex-start; flex-direction:column; padding-top:17px; }
            .section { padding:13px; border-radius:12px; }
            .grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:7px; }
            .metric { padding:9px; }
            .metric .value { font-size:15px; }
        }
        @media (prefers-reduced-motion:reduce) { html { scroll-behavior:auto; } }
    </style>
</head>
<body>
  <div class="shell">
    <header class="page-head">
      <div><h1>Live Trading Monitor</h1><div class="subtitle">Last data update · {{ updated }}</div></div>
      <div class="live-mark"><span class="dot {{ 'dot-green' if health.get('api_connection') == 'Authenticated' else 'dot-red' }}"></span>{{ health.get('trading_mode','N/A') }} · {{ health.get('api_connection','N/A') }}</div>
    </header>
    <nav class="quick-nav" aria-label="Dashboard sections">
      <a href="#system-status">System</a><a href="#portfolio-summary">Portfolio</a><a href="#pipeline-dashboard">Pipeline</a><a href="#live-positions">Positions</a><a href="#today-session">Session</a><a href="#fno-options">F&O</a><a href="#watchlist-analysis">Watchlist</a><a href="/">Settings</a>
    </nav>

    <div class="section" id="system-status">
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

    <div class="section" id="portfolio-summary">
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

""" + PIPELINE_MONITOR_SECTION + """

    <div class="section" id="live-positions">
        <h2>Live Positions</h2>
        {% if positions %}
        <div class="table-wrap"><table>
            <tr>
                <th>Symbol</th><th>Raw</th><th>Market</th><th>Policy</th><th>Final</th><th>Qty</th><th>Entry</th><th>Current</th>
                <th>Time in Trade</th><th>Gross P&L</th><th>Net P&L</th><th>Profit %</th>
                <th>MFE %</th><th>MAE %</th><th>Hard Stop</th><th>Strategy Stop</th><th>Active Target</th>
                <th>Dist. Stop %</th><th>Dist. Target %</th><th>Entry R:R</th><th>Live R:R</th><th>Status</th>
            </tr>
            {% for p in positions %}
            <tr>
                <td>{{ p.get('symbol') }}</td>
                <td>{{ p.get('raw_direction') or '-' }}</td>
                <td>{{ p.get('policy_market_trend') or '-' }}</td>
                <td>{{ p.get('policy_decision') or '-' }}</td>
                <td>{{ p.get('final_direction') or p.get('side') }}</td>
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
        </table></div><div class="sr-note">Scroll horizontally to inspect every position field.</div>
        {% else %}
        <div class="empty">No open positions.</div>
        {% endif %}
    </div>

    <div class="section" id="today-session">
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

    <div class="section" id="fno-options">

        <h2>F&O Options · Professional Momentum</h2>

        <div id="fno-live">
            <div class="empty">
                Loading F&O monitor...
            </div>
        </div>

    </div>

    <script>
    (function () {
        const root = document.getElementById("fno-live");
        if (!root) return;

        function n(v, d) {
            const x = Number(v);
            return Number.isFinite(x) ? x.toFixed(d) : "-";
        }

        function esc(v) {
            return String(v ?? "-")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;");
        }

        function render(f) {
            if (!f) {
                root.innerHTML =
                    '<div class="empty">F&O data unavailable.</div>';
                return;
            }

            const serviceClass =
                f.service_active ? "green" : "red";

            const feed =
                f.pressure_present > 0
                ? '<span class="green">DATA PRESENT</span>'
                : f.evaluations > 0
                  ? '<span class="red">DATA MISSING</span>'
                  : '<span class="yellow">WAITING</span>';

            let html = `
            <div class="grid">

              <div class="metric">
                <div class="label">Service</div>
                <div class="value ${serviceClass}">
                  <span class="dot ${f.service_active ? "dot-green" : "dot-red"}"></span>
                  ${f.service_active ? "RUNNING" : "STOPPED"}
                </div>
              </div>

              <div class="metric">
                <div class="label">Mode</div>
                <div class="value green">${esc(f.mode)}</div>
              </div>

              <div class="metric">
                <div class="label">Strategy</div>
                <div class="value">${esc(f.strategy)}</div>
              </div>

              <div class="metric">
                <div class="label">Universe</div>
                <div class="value">${esc(f.universe)}</div>
              </div>

              <div class="metric">
                <div class="label">Capital</div>
                <div class="value">Rs${n(f.capital,2)}</div>
              </div>

              <div class="metric">
                <div class="label">Evaluations</div>
                <div class="value">${esc(f.evaluations)}</div>
              </div>

              <div class="metric">
                <div class="label">Signals</div>
                <div class="value">${esc(f.signals)}</div>
              </div>

              <div class="metric">
                <div class="label">Open Positions</div>
                <div class="value">${(f.positions || []).length}</div>
              </div>

              <div class="metric">
                <div class="label">Trades Today</div>
                <div class="value">${(f.trades || []).length}</div>
              </div>

              <div class="metric">
                <div class="label">Wins / Losses</div>
                <div class="value">
                  <span class="green">${esc(f.wins)}</span>
                  /
                  <span class="red">${esc(f.losses)}</span>
                </div>
              </div>

              <div class="metric">
                <div class="label">Paper P&L</div>
                <div class="value ${(Number(f.pnl || 0) >= 0) ? "green" : "red"}">
                  Rs${n(f.pnl,2)}
                </div>
              </div>

              <div class="metric">
                <div class="label">Audit Freshness</div>
                <div class="value ${(Number(f.audit_age_seconds || 9999) < 30) ? "green" : "yellow"}">
                  ${f.audit_age_seconds == null ? "N/A" : esc(f.audit_age_seconds) + " sec"}
                </div>
              </div>

              <div class="metric">
                <div class="label">Pressure Present</div>
                <div class="value green">${esc(f.pressure_present)}</div>
              </div>

              <div class="metric">
                <div class="label">Pressure Missing</div>
                <div class="value ${f.pressure_missing ? "red" : "green"}">
                  ${esc(f.pressure_missing)}
                </div>
              </div>

              <div class="metric">
                <div class="label">Pressure Feed</div>
                <div class="value">${feed}</div>
              </div>

            </div>
            `;


            html += `
              <div style="height:18px"></div>
              <h2>Live Rejection Funnel</h2>
            `;

            if ((f.rejections || []).length) {

                html += `
                <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Reason</th>
                      <th>Count</th>
                      <th>%</th>
                    </tr>
                  </thead>
                  <tbody>
                `;

                for (const r of f.rejections) {
                    html += `
                    <tr>
                      <td>${esc(r.reason)}</td>
                      <td>${esc(r.count)}</td>
                      <td>${n(r.pct,2)}%</td>
                    </tr>
                    `;
                }

                html += "</tbody></table></div>";

            } else {
                html += '<div class="empty">No evaluations yet.</div>';
            }


            html += `
              <div style="height:18px"></div>
              <h2>Latest Professional Evaluations</h2>
            `;

            if ((f.latest || []).length) {

                html += `
                <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Symbol</th>
                      <th>Direction</th>
                      <th>Spot 30s %</th>
                      <th>CE 30s %</th>
                      <th>PE 30s %</th>
                      <th>Volume Δ</th>
                      <th>OI</th>
                      <th>Pressure</th>
                      <th>Decision</th>
                    </tr>
                  </thead>
                  <tbody>
                `;

                for (const x of f.latest) {

                    const pressure =
                        x.selected_pressure == null
                        ? '<span class="red">MISSING</span>'
                        : esc(x.selected_pressure);

                    html += `
                    <tr>
                      <td>${esc(x.time)}</td>
                      <td>${esc(x.symbol)}</td>
                      <td>${esc(x.direction)}</td>
                      <td>${n(x.underlying_roc,3)}</td>
                      <td>${n(x.ce_roc,3)}</td>
                      <td>${n(x.pe_roc,3)}</td>
                      <td>${esc(x.volume_delta)}</td>
                      <td>${esc(x.oi)}</td>
                      <td>${pressure}</td>
                      <td>${esc(x.reason)}</td>
                    </tr>
                    `;
                }

                html += "</tbody></table></div>";

            } else {
                html += '<div class="empty">No F&O evaluations available.</div>';
            }

            root.innerHTML = html;
        }


        async function refreshFno() {
            try {
                const response = await fetch(
                    "/api/monitor-data",
                    {
                        cache: "no-store",
                        credentials: "same-origin"
                    }
                );

                if (!response.ok) return;

                const data = await response.json();

                render(data.fno);

            } catch (_) {
                // Preserve last good display.
            }
        }

        refreshFno();

        const timer = setInterval(
            refreshFno,
            5000
        );

        window.addEventListener(
            "pagehide",
            () => clearInterval(timer),
            { once: true }
        );
    })();
    </script>

""" + WATCHLIST_SECTION + """
  </div>
</body>
</html>
"""
